"""Административные маршруты: ручной поллинг, диалоги, шаги воронки.

Правила слоя доставки:

* **никакой бизнес-логики в роутерах** — любое действие выполняется сервисом,
  который приходит через зависимость (``IngestService``, ``ConversationService``,
  ``SchedulingService``). Роутер только собирает ответ;
* чтение карточки диалога — исключение: это сборка представления из нескольких
  репозиториев одной транзакцией, а не бизнес-решение;
* контакты кандидата маскируются (см. ``api.schemas``);
* все маршруты требуют заголовок ``X-API-Key`` (кроме ``/health*`` и ``/version``).

Роутер подключён с префиксом ``/api/v1`` и зависимостью ``require_api_key``.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from hirebot.api.actions import require_channel, send_manual_message
from hirebot.api.deps import (
    ContainerDep,
    ConversationServiceDep,
    IngestServiceDep,
    SchedulingServiceDep,
    require_api_key,
    resolve_service,
)
from hirebot.api.schemas import (
    ActionResult,
    ApprovedDraftResponse,
    ApproveDraftRequest,
    BookingSchema,
    BookRequest,
    CandidateSchema,
    ConversationCard,
    ConversationListResponse,
    ConversationSummary,
    DraftListResponse,
    DraftSchema,
    ManualMessageAccepted,
    ManualMessageRequest,
    MessageSchema,
    PollResponse,
    RejectDraftRequest,
    ScreeningSchema,
    SlotProposalRequest,
    SlotSchema,
    VacancySchema,
)
from hirebot.container import Container
from hirebot.domain.enums import Channel, ConversationState
from hirebot.domain.errors import NotFound
from hirebot.logging_setup import get_correlation_id, get_logger
from hirebot.worker.results import conversation_state, int_counters

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/v1",
    tags=["admin"],
    dependencies=[Depends(require_api_key)],
)


@router.post(
    "/channels/{channel}/poll",
    response_model=PollResponse,
    summary="Ручной запуск поллинга канала",
    responses={503: {"description": "Канал не сконфигурирован"}},
)
async def poll_channel_route(
    channel: Channel,
    container: ContainerDep,
    ingest: IngestServiceDep,
    limit: Annotated[int, Query(ge=1, le=200, description="Сколько откликов забрать")] = 50,
) -> PollResponse:
    """Опросить площадку и поставить новые отклики в работу.

    Вызывается вручную (из CLI, из админки) в дополнение к cron-поллингу.
    """
    require_channel(container, channel)
    result = await ingest.poll(channel, limit=limit)
    counters = int_counters(result)
    logger.info("manual_poll_done", channel=channel.value, **counters)
    return PollResponse(channel=channel, counters=counters, correlation_id=get_correlation_id())


@router.get(
    "/conversations",
    response_model=ConversationListResponse,
    summary="Список диалогов с фильтром по состоянию",
)
async def list_conversations_route(
    container: ContainerDep,
    states: Annotated[
        list[ConversationState] | None,
        Query(alias="state", description="Фильтр по состоянию (можно несколько)"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ConversationListResponse:
    """Постраничный список диалогов.

    Порт ``ConversationRepository.list_by_state`` не поддерживает ``offset``,
    поэтому запрашиваем ``limit + offset`` и отрезаем лишнее. Это осознанный
    компромисс: альтернатива — менять контракт порта, что дороже.
    """
    requested = states or list(ConversationState)
    async with container.uow_factory() as uow:
        found = await uow.conversations.list_by_state(requested, limit=limit + offset)
    page = list(found)[offset : offset + limit]
    return ConversationListResponse(
        items=[ConversationSummary.from_entity(item) for item in page],
        limit=limit,
        offset=offset,
        count=len(page),
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationCard,
    summary="Карточка диалога",
    responses={404: {"description": "Диалог не найден"}},
)
async def get_conversation_route(
    conversation_id: UUID,
    container: ContainerDep,
    messages_limit: Annotated[int, Query(ge=1, le=200, description="Сколько сообщений вернуть")] = 50,
) -> ConversationCard:
    """Собрать карточку: кандидат, вакансия, скрининг, переписка, бронь."""
    async with container.uow_factory() as uow:
        conversation = await uow.conversations.get(conversation_id)
        if conversation is None:
            msg = f"Диалог {conversation_id} не найден"
            raise NotFound(msg)
        candidate = await uow.candidates.get(conversation.candidate_id)
        vacancy = await uow.vacancies.get(conversation.vacancy_id)
        screening = await uow.screenings.get_latest(conversation.candidate_id, conversation.vacancy_id)
        messages = list(await uow.messages.list_for_conversation(conversation_id, limit=messages_limit))
        booking = await uow.bookings.get_for_conversation(conversation_id)

    # Переписку показываем в хронологическом порядке: репозиторий вправе
    # вернуть её как угодно (например, последние N без сортировки).
    messages.sort(key=lambda message: message.created_at)
    return ConversationCard(
        conversation=ConversationSummary.from_entity(conversation),
        candidate=CandidateSchema.from_entity(candidate) if candidate else None,
        vacancy=VacancySchema.model_validate(vacancy) if vacancy else None,
        screening=ScreeningSchema.from_entity(screening) if screening else None,
        messages=[MessageSchema.from_entity(message) for message in messages],
        booking=BookingSchema.from_entity(booking) if booking else None,
    )


@router.post(
    "/conversations/{conversation_id}/process",
    response_model=ActionResult,
    summary="Прогнать шаг воронки по диалогу",
)
async def process_conversation_route(
    conversation_id: UUID,
    conversation_service: ConversationServiceDep,
) -> ActionResult:
    """Выполнить очередной шаг воронки (резюме -> скрининг -> вопрос/приглашение)."""
    result = await conversation_service.process(conversation_id)
    return ActionResult(
        counters=int_counters(result),
        state=conversation_state(result),
        correlation_id=get_correlation_id(),
    )


@router.post(
    "/conversations/{conversation_id}/slots",
    response_model=list[SlotSchema],
    summary="Предложить слоты собеседования",
)
async def propose_slots_route(
    conversation_id: UUID,
    scheduling: SchedulingServiceDep,
    payload: SlotProposalRequest | None = None,
) -> list[SlotSchema]:
    """Подобрать свободные слоты в календаре и вернуть их кандидату."""
    request = payload or SlotProposalRequest()
    slots = await scheduling.propose_slots(
        conversation_id=conversation_id, days_ahead=request.days_ahead, limit=request.limit
    )
    return [SlotSchema.from_entity(slot) for slot in slots]


@router.post(
    "/conversations/{conversation_id}/book",
    response_model=BookingSchema,
    summary="Забронировать слот по индексу",
    responses={409: {"description": "Слот занят"}},
)
async def book_interview_route(
    conversation_id: UUID,
    scheduling: SchedulingServiceDep,
    payload: BookRequest | None = None,
) -> BookingSchema:
    """Забронировать собеседование по выбранному слоту.

    Предпочтительно передавать ``slot_start`` — конкретное время, которое выбрал
    кандидат. ``slot_index`` оставлен для совместимости и указывает на позицию в
    текущей сетке; при пересчёте сетки он может адресовать другое время, поэтому
    при наличии ``slot_start`` индекс игнорируется.
    """
    request = payload or BookRequest()
    booking = await scheduling.book(
        conversation_id=conversation_id,
        slot_index=None if request.slot_start is not None else request.slot_index,
        slot_start=request.slot_start,
        requested_by=request.requested_by,
    )
    logger.info(
        "interview_booked",
        conversation_id=str(conversation_id),
        slot_start=request.slot_start.isoformat() if request.slot_start else None,
        slot_index=None if request.slot_start is not None else request.slot_index,
    )
    return BookingSchema.from_entity(booking)


# ---------------------------------------------------------------------------
# Очередь подтверждения
# ---------------------------------------------------------------------------


def _approval_service(container: Container) -> Any:
    """``ApprovalService``: черновики сообщений и их одобрение."""
    return resolve_service("hirebot.services.approval", "ApprovalService", container)


@router.get(
    "/approvals",
    response_model=DraftListResponse,
    summary="Очередь подтверждения черновиков",
)
async def list_approvals(
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> DraftListResponse:
    """Черновики, ожидающие решения рекрутера.

    Пока черновик в этой очереди, кандидат ничего не получил: это режим, в
    котором бота безопасно включать на реальных кандидатах.
    """
    service = _approval_service(container)
    pending = await service.list_pending(limit=limit)
    items = [
        DraftSchema(
            draft_id=item.draft_id,
            conversation_id=item.conversation_id,
            candidate_name=item.candidate_name,
            vacancy_title=item.vacancy_title,
            channel=item.channel,
            body=item.body,
            kind=item.kind,
            reason=item.reason,
            model=item.model,
            created_at=item.created_at,
            score=item.score,
        )
        for item in pending
    ]
    return DraftListResponse(total=len(items), items=items)


@router.post(
    "/approvals/{draft_id}/approve",
    response_model=ApprovedDraftResponse,
    summary="Одобрить черновик и отправить сообщение",
    responses={404: {"description": "Черновик не найден"}, 409: {"description": "Уже обработан"}},
)
async def approve_draft_route(
    draft_id: UUID,
    container: ContainerDep,
    payload: ApproveDraftRequest | None = None,
) -> ApprovedDraftResponse:
    """Одобрить черновик: сообщение уходит кандидату, шаг воронки завершается.

    Повторное одобрение безопасно: ключ идемпотентности совпадает с тем, что был
    бы при прямой отправке, поэтому второе сообщение кандидат не получит. Попытка
    одобрить уже обработанный черновик — ``409``, а не молчаливый успех.
    """
    request = payload or ApproveDraftRequest()
    service = _approval_service(container)
    message = await service.approve(draft_id, reviewer=request.reviewer, text=request.text)
    logger.info("draft_approved_api", draft_id=str(draft_id), reviewer=request.reviewer)
    return ApprovedDraftResponse(
        draft_id=draft_id,
        conversation_id=message.conversation_id,
        message_id=message.id,
        external_id=message.external_id,
        state="approved",
    )


@router.post(
    "/approvals/{draft_id}/reject",
    response_model=ActionResult,
    summary="Отклонить черновик",
    responses={404: {"description": "Черновик не найден"}, 409: {"description": "Уже обработан"}},
)
async def reject_draft_route(
    draft_id: UUID,
    container: ContainerDep,
    payload: RejectDraftRequest | None = None,
) -> ActionResult:
    """Отклонить черновик: кандидату ничего не уходит, диалог остаётся человеку."""
    request = payload or RejectDraftRequest()
    service = _approval_service(container)
    await service.reject(draft_id, reviewer=request.reviewer, reason=request.reason)
    return ActionResult(conversation_id=draft_id, action="draft_rejected", state="rejected")


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=ManualMessageAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ручное сообщение рекрутера",
    responses={404: {"description": "Диалог не найден"}},
)
async def send_manual_message_route(
    conversation_id: UUID,
    container: ContainerDep,
    payload: ManualMessageRequest,
) -> ManualMessageAccepted:
    """Положить ручное сообщение рекрутера в outbox.

    Отправка выполняется воркером: HTTP-запрос не должен ждать площадку.
    """
    result = await send_manual_message(
        container,
        conversation_id=conversation_id,
        text=payload.text,
        requested_by=payload.requested_by,
    )
    return ManualMessageAccepted(
        message_id=result.message_id,
        outbox_entry_id=result.outbox_entry_id,
        conversation_id=conversation_id,
        requested_by=payload.requested_by,
    )


__all__ = ["router"]
