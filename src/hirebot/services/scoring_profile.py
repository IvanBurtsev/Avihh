"""Профили скоринга: настройка, публикация версий и режим пилота.

Зачем отдельный сервис.

Критерии и вопросы — это **настройка под вакансию**, которая меняется при
каждой публикации. Если менять их «на месте», то:

* нельзя объяснить прошлое решение — критерии к тому времени уже другие;
* нет возможности сравнить старую и новую настройку;
* нет безопасного способа включить изменения на реальных кандидатах.

Поэтому здесь три правила:

1. **Правки копятся в черновике**, применяются публикацией.
2. **Версии не удаляются**: откат — это публикация другой версии.
3. **Первый запуск — shadow**: система считает и показывает результат, но
   ничего не отправляет. Только после сверки с решениями человека — active.

Профиль кэшируется: он читается на каждого кандидата, а меняется редко.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from hirebot.domain.entities import Vacancy, new_id
from hirebot.domain.enums import ProfileStatus, QuestionKind, ThresholdMode
from hirebot.domain.errors import NotFound, PolicyViolation
from hirebot.domain.scoring import (
    MAX_QUESTIONS,
    ProfileCriterion,
    ProfileQuestion,
    ProfileThresholds,
    ScoringProfile,
)
from hirebot.logging_setup import get_logger
from hirebot.services.base import ServiceBase
from hirebot.services.policy import PolicyGuard

logger = get_logger(__name__)

#: Сколько живёт кэш активного профиля. Профиль меняется при публикации, поэтому
#: минуты достаточно, а инвалидация снимает кэш сразу после публикации.
PROFILE_CACHE_TTL_S = 120.0


def profile_cache_key(vacancy_id: UUID) -> str:
    return f"scoring:profile:{vacancy_id}"


class ScoringProfileService(ServiceBase):
    """Управление версиями профиля скоринга."""

    # ------------------------------------------------------------ чтение

    async def get_live(self, vacancy_id: UUID) -> ScoringProfile | None:
        """Живой профиль вакансии (active или shadow), из кэша при возможности."""
        key = profile_cache_key(vacancy_id)
        cached = await self.container.cache.get_json(key)
        if isinstance(cached, dict):
            try:
                return _profile_from_cache(cached)
            except (KeyError, ValueError, TypeError):
                # Битый кэш не должен ломать обработку: читаем из БД.
                await self.container.cache.delete(key)

        async with self.uow() as uow:
            profile = await uow.profiles.get_live(vacancy_id)
        if profile is not None:
            await self.container.cache.set_json(key, _profile_to_cache(profile), ttl_s=PROFILE_CACHE_TTL_S)
        return profile

    async def resolve_for_vacancy(self, vacancy: Vacancy) -> ScoringProfile | None:
        """Профиль для обработки кандидата с обратной совместимостью.

        Если профиля нет (вакансия создана до появления версий), он собирается
        из ``vacancy.criteria`` на лету и **не сохраняется**: это не решение
        человека, а перенос прежнего поведения. Сохранение произойдёт при первой
        публикации.
        """
        live = await self.get_live(vacancy.id)
        if live is not None:
            return live
        if not vacancy.criteria:
            return None
        return self._profile_from_vacancy(vacancy)

    async def list_versions(self, vacancy_id: UUID, *, limit: int = 50) -> Sequence[ScoringProfile]:
        async with self.uow() as uow:
            return await uow.profiles.list_versions(vacancy_id, limit=limit)

    async def get_draft(self, vacancy_id: UUID) -> ScoringProfile | None:
        """Текущий черновик или None, если правок нет."""
        versions = await self.list_versions(vacancy_id, limit=50)
        return next((item for item in versions if item.is_draft), None)

    # ------------------------------------------------------------ запись

    async def create_draft(
        self,
        *,
        vacancy: Vacancy,
        criteria: Sequence[ProfileCriterion],
        questions: Sequence[ProfileQuestion] = (),
        thresholds: ProfileThresholds | None = None,
        created_by: str = "recruiter",
        comment: str = "",
    ) -> ScoringProfile:
        """Создать черновик следующей версии.

        Проверки (дискриминация, пустой набор, обязательный критерий) выполняются
        до сохранения: черновик с ошибками не должен попасть в базу, иначе его
        случайно опубликуют.
        """
        list_criteria = list(criteria)
        list_questions = list(questions)
        guard = PolicyGuard(self.settings.policy)
        guard.assert_criteria_allowed([item.as_vacancy_dict() for item in list_criteria])
        guard.assert_questions_allowed([item.text for item in list_questions])

        resolved_thresholds = thresholds or ProfileThresholds()
        if len(list_questions) > resolved_thresholds.max_questions:
            msg = (
                f"Вопросов {len(list_questions)}, а лимит {resolved_thresholds.max_questions}: "
                "сократите список или поднимите лимит"
            )
            raise PolicyViolation(msg)

        async with self.uow() as uow:
            latest = await uow.profiles.get_latest_version(vacancy.id)
            next_version = (latest.version + 1) if latest is not None else 1
            profile = ScoringProfile(
                id=new_id(),
                company_id=vacancy.company_id,
                vacancy_id=vacancy.id,
                version=next_version,
                status=ProfileStatus.DRAFT,
                criteria=list_criteria,
                questions=list_questions,
                thresholds=resolved_thresholds,
                created_by=created_by,
                created_at=self.now(),
                comment=comment,
            )
            profile.validate()
            saved = await uow.profiles.add(profile)
            await uow.commit()

        logger.info(
            "scoring_profile_draft_created",
            vacancy_id=str(vacancy.id),
            version=saved.version,
            criteria=len(saved.criteria),
            questions=len(saved.questions),
        )
        return saved

    async def publish(self, profile_id: UUID, *, as_shadow: bool = False) -> ScoringProfile:
        """Опубликовать версию.

        ``as_shadow=True`` — режим пилота: профиль применяется для расчёта, но
        решения не отправляются кандидатам. Это режим по умолчанию для первого
        запуска на реальных людях.
        """
        async with self.uow() as uow:
            profile = await uow.profiles.get(profile_id)
            if profile is None:
                raise NotFound(f"Профиль {profile_id} не найден")
            try:
                profile.validate()
            except ValueError as exc:
                msg = f"Профиль нельзя опубликовать: {exc}"
                raise PolicyViolation(msg) from exc
            saved = await uow.profiles.save(profile)
            await uow.commit()

        async with self.uow() as uow:
            saved = await uow.profiles.publish(profile_id)
            if as_shadow:
                saved.status = ProfileStatus.SHADOW
                saved = await uow.profiles.save(saved)
            await uow.commit()

        await self.container.cache.delete(profile_cache_key(saved.vacancy_id))
        logger.info(
            "scoring_profile_published",
            vacancy_id=str(saved.vacancy_id),
            version=saved.version,
            shadow=as_shadow,
        )
        return saved

    async def switch_to_shadow(self, profile_id: UUID) -> ScoringProfile:
        """Перевести живую версию в shadow: считать, но не отправлять."""
        async with self.uow() as uow:
            profile = await uow.profiles.get(profile_id)
            if profile is None:
                raise NotFound(f"Профиль {profile_id} не найден")
            profile.status = ProfileStatus.SHADOW
            saved = await uow.profiles.save(profile)
            await uow.commit()
        await self.container.cache.delete(profile_cache_key(saved.vacancy_id))
        logger.info("scoring_profile_shadow", profile_id=str(profile_id))
        return saved

    async def rollback(self, vacancy_id: UUID, version: int) -> ScoringProfile:
        """Откатить вакансию на прежнюю версию профиля."""
        async with self.uow() as uow:
            restored = await uow.profiles.activate_version(vacancy_id, version)
            await uow.commit()
        await self.container.cache.delete(profile_cache_key(vacancy_id))
        logger.warning(
            "scoring_profile_rollback",
            vacancy_id=str(vacancy_id),
            version=version,
        )
        return restored

    # ------------------------------------------------------------ внутреннее

    def _profile_from_vacancy(self, vacancy: Vacancy) -> ScoringProfile:
        """Собрать профиль из прежних критериев вакансии.

        Обратная совместимость: вакансии, созданные до появления версий, должны
        продолжать обрабатываться, пока профиль не опубликован явно.
        """
        criteria: list[ProfileCriterion] = []
        for position, item in enumerate(vacancy.criteria):
            key = str(item.get("key") or f"criterion_{position}")
            weight = int(float(str(item.get("weight") or 1)))
            criteria.append(
                ProfileCriterion(
                    key=key,
                    title=str(item.get("title") or key),
                    weight=max(1, min(5, weight)),
                    must_have=bool(item.get("must_have")),
                    evidence_hint=str(item.get("evidence_hint") or ""),
                    expectation=str(item.get("expectation") or item.get("title") or key),
                    source="vacancy",
                    position=position,
                )
            )
        return ScoringProfile(
            id=new_id(),
            company_id=vacancy.company_id,
            vacancy_id=vacancy.id,
            version=0,
            status=ProfileStatus.ACTIVE,
            criteria=criteria,
            questions=[],
            thresholds=ProfileThresholds(),
            created_by="system",
            created_at=self.now(),
            comment="Собран из критериев вакансии (до появления версий)",
        )


def _profile_to_cache(profile: ScoringProfile) -> dict[str, object]:
    """Сериализовать профиль для кэша (только данные, без объектов домена)."""
    return {
        "id": str(profile.id),
        "company_id": str(profile.company_id),
        "vacancy_id": str(profile.vacancy_id),
        "version": profile.version,
        "status": profile.status.value,
        "created_by": profile.created_by,
        "comment": profile.comment,
        "criteria": [
            {
                "key": item.key,
                "title": item.title,
                "weight": item.weight,
                "must_have": item.must_have,
                "evidence_hint": item.evidence_hint,
                "expectation": item.expectation,
                "source": item.source,
                "position": item.position,
            }
            for item in profile.criteria
        ],
        "questions": [
            {
                "text": item.text,
                "criterion_key": item.criterion_key,
                "kind": item.kind.value,
                "informativeness": item.informativeness,
                "is_custom": item.is_custom,
                "position": item.position,
            }
            for item in profile.questions
        ],
        "thresholds": {
            "invite_at": profile.thresholds.invite_at,
            "human_at": profile.thresholds.human_at,
            "reject_at": profile.thresholds.reject_at,
            "mode": profile.thresholds.mode.value,
            "top_k": profile.thresholds.top_k,
            "max_questions": profile.thresholds.max_questions,
            "auto_invite": profile.thresholds.auto_invite,
            "min_coverage_for_auto": profile.thresholds.min_coverage_for_auto,
        },
    }


def _profile_from_cache(payload: dict[str, object]) -> ScoringProfile:
    """Восстановить профиль из кэша, проверив типы.

    Кэш — внешние данные: если его формат изменится или он будет повреждён,
    обработка не должна падать. Здесь ошибки намеренно пробрасываются как
    ``ValueError``/``KeyError`` — вызывающая сторона прочитает профиль из БД.
    """
    thresholds_raw = payload["thresholds"]
    if not isinstance(thresholds_raw, dict):
        msg = "Повреждённый кэш профиля: thresholds"
        raise ValueError(msg)
    criteria_raw = payload.get("criteria") or []
    questions_raw = payload.get("questions") or []
    if not isinstance(criteria_raw, list) or not isinstance(questions_raw, list):
        msg = "Повреждённый кэш профиля: criteria/questions"
        raise ValueError(msg)

    criteria = [
        ProfileCriterion(
            key=str(item["key"]),
            title=str(item["title"]),
            weight=int(item["weight"]),
            must_have=bool(item["must_have"]),
            evidence_hint=str(item.get("evidence_hint") or ""),
            expectation=str(item.get("expectation") or ""),
            source=str(item.get("source") or "catalog"),
            position=int(item.get("position") or 0),
        )
        for item in criteria_raw
        if isinstance(item, dict)
    ]
    questions = [
        ProfileQuestion(
            text=str(item["text"]),
            criterion_key=str(item["criterion_key"]),
            kind=QuestionKind(str(item.get("kind") or QuestionKind.FACT.value)),
            informativeness=int(item.get("informativeness") or 2),
            is_custom=bool(item.get("is_custom")),
            position=int(item.get("position") or 0),
        )
        for item in questions_raw
        if isinstance(item, dict)
    ]
    return ScoringProfile(
        id=UUID(str(payload["id"])),
        company_id=UUID(str(payload["company_id"])),
        vacancy_id=UUID(str(payload["vacancy_id"])),
        version=int(str(payload["version"])),
        status=ProfileStatus(str(payload["status"])),
        criteria=criteria,
        questions=questions,
        thresholds=ProfileThresholds(
            invite_at=float(thresholds_raw["invite_at"]),
            human_at=float(thresholds_raw["human_at"]),
            reject_at=float(thresholds_raw["reject_at"]),
            mode=ThresholdMode(str(thresholds_raw.get("mode") or ThresholdMode.ABSOLUTE.value)),
            top_k=int(thresholds_raw.get("top_k") or 10),
            max_questions=min(MAX_QUESTIONS, int(thresholds_raw.get("max_questions") or MAX_QUESTIONS)),
            auto_invite=bool(thresholds_raw.get("auto_invite")),
            min_coverage_for_auto=float(thresholds_raw.get("min_coverage_for_auto") or 0.7),
        ),
        created_by=str(payload.get("created_by") or "system"),
        created_at=datetime.now(tz=UTC),
        comment=str(payload.get("comment") or ""),
    )


__all__ = [
    "PROFILE_CACHE_TTL_S",
    "ScoringProfileService",
    "profile_cache_key",
]
