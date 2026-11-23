"""Сбор откликов с площадок.

Поллинг, а не вебхуки: у HH перечень событий и подпись в публичной
документации не раскрыты, у Talantix вебхуков нет вовсе.

Ключевое требование (ARCHITECTURE.md, п. 3.1): **отдельная транзакция на
каждый отклик**. Падение на одном кандидате не должно ронять пачку, поэтому
ошибки собираются в ``PollResult.errors``, а обработка продолжается.

Дедупликация двойная:
* канал сам отдаёт только новые/непрочитанные отклики;
* если диалог по ``(channel, external_id)`` уже существует и ушёл дальше
  состояния ``NEW``, отклик считается дублем и повторно в работу не ставится.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from hirebot.container import Container
from hirebot.domain.entities import Candidate, Conversation, OutboxEntry, Resume, Vacancy, new_id
from hirebot.domain.enums import Channel, ConversationState
from hirebot.domain.errors import ChannelNotConfigured, ChannelUnavailable
from hirebot.logging_setup import get_logger
from hirebot.observability.metrics import record_response_ingested
from hirebot.ports.channels import ChannelPort
from hirebot.ports.dto import InboundResponse, VacancyPayload
from hirebot.ports.repositories import UnitOfWork
from hirebot.services.base import DEFAULT_COMPANY_ID, ServiceBase
from hirebot.services.topics import PROCESS_CONVERSATION_TOPIC

logger = get_logger(__name__)


@dataclass(slots=True)
class PollResult:
    """Итог одного цикла опроса площадки."""

    channel: Channel
    found: int = 0
    created: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class IngestOutcome:
    """Итог приёма одного отклика."""

    conversation_id: UUID | None
    created: bool
    reason: str = ""


class IngestService(ServiceBase):
    """Опрос канала и раскладка откликов в воронку."""

    def __init__(self, container: Container) -> None:
        super().__init__(container)

    async def poll(self, channel: Channel, *, limit: int = 50) -> PollResult:
        """Опросить площадку и принять все свежие отклики.

        Никогда не бросает наружу ошибку отдельного отклика: они попадают в
        ``PollResult.errors``. Ошибка самого канала (недоступен/не настроен)
        возвращает пустой результат с текстом ошибки.
        """
        result = PollResult(channel=channel)
        adapter = self._adapter_or_none(channel)
        if adapter is None:
            result.errors.append(f"Канал {channel.value} не сконфигурирован")
            return result

        try:
            responses = list(await adapter.fetch_new_responses(limit=limit))
        except ChannelUnavailable as exc:
            logger.warning("poll_channel_unavailable", channel=channel.value, error=str(exc))
            result.errors.append(f"{channel.value}: {exc}")
            return result

        result.found = len(responses)
        for response in responses:
            try:
                outcome = await self.ingest(response)
            except Exception as exc:
                logger.warning(
                    "ingest_response_failed",
                    channel=channel.value,
                    conversation_external_id=response.conversation_external_id,
                    error=str(exc),
                )
                result.errors.append(f"{response.conversation_external_id}: {exc}")
                continue
            if outcome.created:
                result.created += 1
            else:
                result.duplicates += 1

        logger.info(
            "poll_finished",
            channel=channel.value,
            found=result.found,
            created=result.created,
            duplicates=result.duplicates,
            errors=len(result.errors),
        )
        return result

    async def ingest(self, response: InboundResponse) -> IngestOutcome:
        """Принять один отклик: upsert данных + постановка шага воронки.

        Идемпотентно: повторный вызов с тем же откликом не создаёт второй
        диалог и не запускает обработку заново.
        """
        existing = await self._find_conversation(response)
        if existing is not None:
            return await self._handle_existing(existing)

        vacancy = await self._ensure_vacancy(response)
        async with self.uow() as uow:
            vacancy = await uow.vacancies.upsert(vacancy)
            candidate = await self._ensure_candidate(uow, response, vacancy)
            now = self.now()
            conversation = Conversation(
                id=new_id(),
                company_id=vacancy.company_id,
                candidate_id=candidate.id,
                vacancy_id=vacancy.id,
                channel=response.channel,
                external_id=response.conversation_external_id,
                state=ConversationState.NEW,
                created_at=now,
                updated_at=now,
            )
            conversation = await uow.conversations.upsert(conversation)
            await self._ensure_resume_stub(uow, response, candidate)
            # Задача шага воронки кладётся в outbox в той же транзакции, что и
            # создание диалога: иначе воркер увидит диалог без задачи.
            await uow.outbox.add(self._process_entry(conversation.id))
            await uow.commit()

        await self._wake_worker(conversation.id)
        record_response_ingested(response.channel.value)
        logger.info(
            "response_ingested",
            channel=response.channel.value,
            conversation_id=str(conversation.id),
            vacancy_external_id=response.vacancy_external_id,
        )
        return IngestOutcome(conversation_id=conversation.id, created=True, reason="created")

    # ------------------------------------------------------------------ #
    # Внутреннее
    # ------------------------------------------------------------------ #

    async def _handle_existing(self, existing: Conversation) -> IngestOutcome:
        """Отклик уже видели: решаем, дубль это или незавершённый приём."""
        if existing.state is not ConversationState.NEW:
            logger.info(
                "response_duplicate",
                conversation_id=str(existing.id),
                state=existing.state.value,
            )
            return IngestOutcome(
                conversation_id=existing.id,
                created=False,
                reason=f"duplicate:{existing.state.value}",
            )

        # Диалог создан, но шаг воронки не поставлен (упали между коммитами
        # или предыдущая постановка потерялась) — повторяем постановку.
        async with self.uow() as uow:
            fresh = await uow.conversations.get(existing.id)
            if fresh is None:
                return IngestOutcome(conversation_id=existing.id, created=False, reason="duplicate:gone")
            if fresh.state is not ConversationState.NEW:
                return IngestOutcome(
                    conversation_id=fresh.id,
                    created=False,
                    reason=f"duplicate:{fresh.state.value}",
                )
            await uow.outbox.add(self._process_entry(fresh.id))
            await uow.commit()
        await self._wake_worker(existing.id)
        return IngestOutcome(conversation_id=existing.id, created=False, reason="already_pending")

    async def _find_conversation(self, response: InboundResponse) -> Conversation | None:
        async with self.uow() as uow:
            return await uow.conversations.get_by_external(
                response.channel, response.conversation_external_id
            )

    async def _ensure_vacancy(self, response: InboundResponse) -> Vacancy:
        """Найти вакансию в БД или создать её по данным канала.

        Критерии скоринга настраивает рекрутер уже в нашей системе, поэтому у
        вакансии, впервые увиденной в отклике, критериев нет. Пустой список
        критериев дальше приводит к ``NEEDS_REVIEW``, а не к «оценке на глаз».
        """
        async with self.uow() as uow:
            vacancy = await uow.vacancies.get_by_external(response.channel, response.vacancy_external_id)
        if vacancy is not None:
            return vacancy

        payload = await self._find_vacancy_payload(response)
        now = self.now()
        if payload is None:
            logger.warning(
                "vacancy_payload_missing",
                channel=response.channel.value,
                vacancy_external_id=response.vacancy_external_id,
            )
        return Vacancy(
            id=new_id(),
            channel=response.channel,
            external_id=response.vacancy_external_id,
            title=payload.title if payload is not None else f"Вакансия {response.vacancy_external_id}",
            company_id=DEFAULT_COMPANY_ID,
            description=payload.description if payload is not None else "",
            city=payload.city if payload is not None else None,
            is_active=payload.is_active if payload is not None else True,
            criteria=[],
            created_at=now,
            updated_at=now,
        )

    async def _find_vacancy_payload(self, response: InboundResponse) -> VacancyPayload | None:
        adapter = self._adapter_or_none(response.channel)
        if adapter is None:
            return None
        try:
            payloads = await adapter.list_vacancies()
        except ChannelUnavailable as exc:
            logger.warning("list_vacancies_failed", channel=response.channel.value, error=str(exc))
            return None
        for payload in payloads:
            if payload.external_id == response.vacancy_external_id:
                return payload
        return None

    async def _ensure_candidate(
        self,
        uow: UnitOfWork,
        response: InboundResponse,
        vacancy: Vacancy,
    ) -> Candidate:
        candidate = await uow.candidates.get_by_external(response.channel, response.candidate_external_id)
        if candidate is None:
            candidate = Candidate(
                id=new_id(),
                channel=response.channel,
                external_id=response.candidate_external_id,
                full_name=response.candidate_name,
                company_id=vacancy.company_id,
                created_at=self.now(),
            )
        elif response.candidate_name and candidate.full_name != response.candidate_name:
            # Имя могло уточниться на площадке — держим его актуальным.
            candidate.full_name = response.candidate_name
        return await uow.candidates.upsert(candidate)

    async def _ensure_resume_stub(
        self,
        uow: UnitOfWork,
        response: InboundResponse,
        candidate: Candidate,
    ) -> Resume | None:
        """Сохранить заготовку резюме (без текста).

        Заготовка нужна сервису скрининга: в отклике приходит только
        ``resume_external_id``, и без записи в ``resumes`` он не знал бы, какое
        резюме запрашивать у канала. Сам текст догружается в
        ``ScreeningService`` — платить квоту за просмотр резюме раньше времени
        не нужно.
        """
        if not response.resume_external_id:
            return None
        latest = await uow.resumes.get_latest_for_candidate(candidate.id)
        if latest is not None and latest.external_id == response.resume_external_id:
            return latest
        return await uow.resumes.save(
            Resume(
                id=new_id(),
                candidate_id=candidate.id,
                channel=response.channel,
                external_id=response.resume_external_id,
                source_url=response.resume_url,
                fetched_at=self.now(),
            )
        )

    def _process_entry(self, conversation_id: UUID) -> OutboxEntry:
        now = self.now()
        return OutboxEntry(
            id=new_id(),
            topic=PROCESS_CONVERSATION_TOPIC,
            payload={"conversation_id": str(conversation_id)},
            dedupe_key=f"{PROCESS_CONVERSATION_TOPIC}:{conversation_id}",
            # Время берём из порта часов, а не из системных: иначе запись
            # окажется «в будущем» относительно диспетчера и не будет забрана.
            available_at=now,
            created_at=now,
        )

    async def _wake_worker(self, conversation_id: UUID) -> None:
        """Разбудить воркер задачей ``process_conversation``.

        Outbox гарантирует доставку, а задача в очереди — скорость: в
        ``inline``-режиме шаг выполняется сразу, а не на следующем тике
        диспетчера. Шаг идемпотентен, поэтому «двойное» пробуждение безопасно.
        """
        try:
            await self._container.queue.enqueue(
                PROCESS_CONVERSATION_TOPIC,
                {"conversation_id": str(conversation_id)},
                dedupe_key=f"{PROCESS_CONVERSATION_TOPIC}:{conversation_id}",
            )
        except Exception as exc:
            logger.warning(
                "enqueue_process_failed",
                conversation_id=str(conversation_id),
                error=str(exc),
            )

    def _adapter_or_none(self, channel: Channel) -> ChannelPort | None:
        try:
            return self._container.channel(channel)
        except (ChannelNotConfigured, KeyError):
            logger.info("channel_not_configured", channel=channel.value)
            return None
