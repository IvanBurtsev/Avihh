"""Ответы кандидата: сохранение и пересчёт соответствия.

Зачем отдельный сервис.

Ответ на уточняющий вопрос — это **новые данные о кандидате**. Если он остаётся
только в переписке, скоринг не меняется, и вся затея с вопросами теряет смысл:
бот спрашивает, кандидат отвечает, а решение принимается по старому резюме.

Здесь три шага:

1. **Оценить ответ** по критерию (модель возвращает балл и цитату).
2. **Сохранить** ответ с привязкой к критерию — чтобы было видно, что изменилось.
3. **Пересчитать** соответствие и сохранить **новый** результат скрининга.

Почему новый результат, а не правка старого: решение по кандидату должно быть
объяснимо. «До ответа было 60%, после — 80%» — это история, а не перезапись.
"""

from __future__ import annotations

from uuid import UUID

from hirebot.domain.entities import QuestionAnswer, ScreeningResult, new_id
from hirebot.domain.errors import LlmError, NotFound
from hirebot.domain.scoring import (
    CriterionScore,
    ScoringProfile,
    apply_must_have_cap,
    compute_breakdown,
)
from hirebot.logging_setup import get_logger
from hirebot.ports.llm import AnswerAssessmentRequest
from hirebot.services.base import ServiceBase
from hirebot.services.scoring_profile import ScoringProfileService

logger = get_logger(__name__)


class AnswerService(ServiceBase):
    """Оценка ответов и пересчёт процента."""

    async def record_answer(
        self,
        *,
        conversation_id: UUID,
        question: str,
        criterion_key: str,
        answer: str,
    ) -> QuestionAnswer | None:
        """Оценить ответ и сохранить его.

        Возвращает ``None``, если оценить не удалось: переписка важнее, чем
        сбой модели, и диалог не должен останавливаться из-за этого.
        """
        async with self.uow() as uow:
            conversation = await uow.conversations.get(conversation_id)
            if conversation is None:
                raise NotFound(f"Диалог {conversation_id} не найден")
            vacancy = await uow.vacancies.get(conversation.vacancy_id)
            resume = await uow.resumes.get_latest_for_candidate(conversation.candidate_id)
        if vacancy is None:
            raise NotFound(f"Вакансия {conversation.vacancy_id} не найдена")

        profile = await ScoringProfileService(self._container).resolve_for_vacancy(vacancy)
        criterion = profile.criterion(criterion_key) if profile is not None else None
        if criterion is None:
            logger.warning(
                "answer_criterion_missing",
                conversation_id=str(conversation_id),
                criterion_key=criterion_key,
            )
            return None

        try:
            assessment = await self._container.llm.assess_answer(
                AnswerAssessmentRequest(
                    criterion_key=criterion.key,
                    criterion_title=criterion.title,
                    expectation=criterion.expectation or criterion.title,
                    evidence_hint=criterion.evidence_hint,
                    question=question,
                    answer=answer,
                    resume_text=resume.raw_text if resume is not None else "",
                )
            )
        except LlmError as exc:
            logger.warning(
                "answer_assessment_failed",
                conversation_id=str(conversation_id),
                error=str(exc),
            )
            return None

        record = QuestionAnswer(
            id=new_id(),
            conversation_id=conversation_id,
            criterion_key=criterion.key,
            question=question,
            answer=answer,
            score=assessment.score,
            is_unclear=assessment.is_unclear,
            contradicts_resume=assessment.contradicts_resume,
            created_at=self.now(),
        )
        async with self.uow() as uow:
            saved = await uow.answers.add(record)
            await uow.commit()

        logger.info(
            "answer_recorded",
            conversation_id=str(conversation_id),
            criterion_key=criterion.key,
            score=assessment.score,
            unclear=assessment.is_unclear,
            contradicts=assessment.contradicts_resume,
        )
        return saved

    async def rescore(
        self,
        *,
        conversation_id: UUID,
        screening: ScreeningResult,
        profile: ScoringProfile,
        answers: list[QuestionAnswer],
    ) -> ScreeningResult:
        """Пересчитать соответствие с учётом ответов и сохранить результат.

        Балл по критерию берётся из ответа: ответ — более свежие данные, чем
        резюме. Если ответ противоречит резюме, балл **не повышается**:
        противоречие разбирает человек.
        """
        if not answers:
            return screening

        scores = {item.key: item for item in _criterion_scores(screening, profile)}
        changed = False
        for answer in answers:
            criterion = profile.criterion(answer.criterion_key)
            if criterion is None:
                continue
            previous = scores.get(criterion.key)
            new_score = answer.score
            if answer.contradicts_resume and previous is not None:
                # Противоречие не улучшает оценку: оставляем меньший из двух.
                new_score = min(previous.score, answer.score)
            scores[criterion.key] = CriterionScore(
                key=criterion.key,
                title=criterion.title,
                weight=criterion.weight,
                score=new_score,
                must_have=criterion.must_have,
                evidence=answer.answer[:200],
                is_gap=new_score < 55.0,
                from_answer=True,
            )
            changed = True

        if not changed:
            return screening

        breakdown = apply_must_have_cap(compute_breakdown(profile.criteria, scores))
        rescored = ScreeningResult(
            id=new_id(),
            resume_id=screening.resume_id,
            vacancy_id=screening.vacancy_id,
            candidate_id=screening.candidate_id,
            total_score=breakdown.percent,
            verdict=screening.verdict,
            criteria=list(screening.criteria),
            summary=screening.summary,
            gaps=list(screening.gaps),
            strengths=list(screening.strengths),
            questions=list(screening.questions),
            model=screening.model,
            prompt_version=screening.prompt_version,
            percent=breakdown.percent,
            coverage=breakdown.coverage,
            blockers=list(breakdown.blockers),
            profile_version=profile.version,
            content_hash=None,
            created_at=self.now(),
        )
        async with self.uow() as uow:
            saved = await uow.screenings.save(rescored)
            await uow.commit()

        logger.info(
            "screening_rescored",
            conversation_id=str(conversation_id),
            old_percent=screening.match_percent,
            new_percent=breakdown.percent,
            answers=len(answers),
        )
        return saved


def _criterion_scores(screening: ScreeningResult, profile: ScoringProfile) -> list[CriterionScore]:
    """Текущие оценки по критериям профиля (с учётом отсутствия данных)."""
    by_key = {item.key: item for item in screening.criteria}
    result: list[CriterionScore] = []
    for criterion in profile.criteria:
        found = by_key.get(criterion.key)
        if found is None:
            result.append(
                CriterionScore(
                    key=criterion.key,
                    title=criterion.title,
                    weight=criterion.weight,
                    score=0.0,
                    must_have=criterion.must_have,
                    evidence="",
                    is_gap=False,
                )
            )
            continue
        result.append(
            CriterionScore(
                key=criterion.key,
                title=criterion.title,
                weight=criterion.weight,
                score=float(found.score),
                must_have=criterion.must_have,
                evidence=found.evidence,
                is_gap=found.is_gap,
            )
        )
    return result


__all__ = ["AnswerService"]
