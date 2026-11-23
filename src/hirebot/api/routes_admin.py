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
    CriterionConfigSchema,
    DraftListResponse,
    DraftSchema,
    ManualMessageAccepted,
    ManualMessageRequest,
    MessageSchema,
    PollResponse,
    PublishProfileRequest,
    QuestionConfigSchema,
    QueueItemSchema,
    QueueResponse,
    RejectDraftRequest,
    ScoringProfileDraftRequest,
    ScoringProfileSchema,
    ScreeningSchema,
    SlotProposalRequest,
    SlotSchema,
    ThresholdsSchema,
    VacancyCreateRequest,
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
# Вакансии и профиль скоринга
# ---------------------------------------------------------------------------


def _scoring_service(container: Container) -> Any:
    return resolve_service("hirebot.services.scoring_profile", "ScoringProfileService", container)


def _priority_service(container: Container) -> Any:
    return resolve_service("hirebot.services.priority", "PriorityService", container)


def _profile_to_schema(profile: Any) -> ScoringProfileSchema:

    return ScoringProfileSchema(
        id=profile.id,
        vacancy_id=profile.vacancy_id,
        version=profile.version,
        status=profile.status.value,
        criteria=[
            CriterionConfigSchema(
                key=item.key,
                title=item.title,
                weight=item.weight,
                must_have=item.must_have,
                evidence_hint=item.evidence_hint,
                expectation=item.expectation,
                source=item.source,
            )
            for item in profile.criteria
        ],
        questions=[
            QuestionConfigSchema(
                text=item.text,
                criterion_key=item.criterion_key,
                kind=item.kind.value,
                informativeness=item.informativeness,
                is_custom=item.is_custom,
            )
            for item in profile.questions
        ],
        thresholds=ThresholdsSchema(
            invite_at=profile.thresholds.invite_at,
            human_at=profile.thresholds.human_at,
            reject_at=profile.thresholds.reject_at,
            mode=profile.thresholds.mode.value,
            top_k=profile.thresholds.top_k,
            max_questions=profile.thresholds.max_questions,
            auto_invite=profile.thresholds.auto_invite,
            min_coverage_for_auto=profile.thresholds.min_coverage_for_auto,
        ),
        created_by=profile.created_by,
        comment=profile.comment,
        published_at=profile.published_at,
    )


@router.post("/vacancies", response_model=VacancySchema, summary="Создать вакансию")
async def create_vacancy_route(
    container: ContainerDep,
    payload: VacancyCreateRequest,
) -> VacancySchema:
    """Создать вакансию в системе.

    ``external_id`` генерируется: вакансия заводится вручную, а не приходит с
    площадки. Критерии задаются отдельно — через профиль скоринга.
    """
    from uuid import uuid4

    from hirebot.domain.entities import Vacancy, new_id
    from hirebot.services.base import DEFAULT_COMPANY_ID

    channel = Channel(payload.channel) if payload.channel in {"hh", "avito"} else Channel.HH
    company_id = payload.company_id or DEFAULT_COMPANY_ID
    vacancy = Vacancy(
        id=new_id(),
        channel=channel,
        external_id=f"manual-{uuid4().hex[:12]}",
        title=payload.title,
        company_id=company_id,
        description=payload.description,
        city=payload.city,
    )
    async with container.uow_factory() as uow:
        await uow.companies.upsert(
            __import__("hirebot.domain.entities", fromlist=["Company"]).Company(
                id=company_id, name="Компания", slug=f"company-{company_id.hex[:8]}"
            )
        )
        saved = await uow.vacancies.upsert(vacancy)
        await uow.commit()
    logger.info("vacancy_created", vacancy_id=str(saved.id))
    return VacancySchema.model_validate(saved)


@router.get("/vacancies", response_model=list[VacancySchema], summary="Список вакансий")
async def list_vacancies_route(
    container: ContainerDep,
    company_id: UUID | None = None,
) -> list[VacancySchema]:
    from hirebot.services.base import DEFAULT_COMPANY_ID

    async with container.uow_factory() as uow:
        vacancies = await uow.vacancies.list_active(company_id or DEFAULT_COMPANY_ID)
    return [VacancySchema.model_validate(item) for item in vacancies]


@router.get(
    "/vacancies/{vacancy_id}/scoring",
    response_model=ScoringProfileSchema,
    summary="Активный профиль скоринга вакансии",
    responses={404: {"description": "Профиль не задан"}},
)
async def get_scoring_route(vacancy_id: UUID, container: ContainerDep) -> ScoringProfileSchema:
    service = _scoring_service(container)
    profile = await service.get_live(vacancy_id)
    if profile is None:
        draft = await service.get_draft(vacancy_id)
        if draft is None:
            raise NotFound("У вакансии нет профиля скоринга: критерии не настроены")
        profile = draft
    return _profile_to_schema(profile)


@router.put(
    "/vacancies/{vacancy_id}/scoring/draft",
    response_model=ScoringProfileSchema,
    summary="Сохранить черновик профиля",
)
async def save_scoring_draft_route(
    vacancy_id: UUID,
    container: ContainerDep,
    payload: ScoringProfileDraftRequest,
) -> ScoringProfileSchema:
    """Сохранить черновик новой версии профиля.

    Проверки (дискриминация, обязательный критерий, лимит вопросов) выполняются
    здесь же: черновик с ошибками не должен попасть в базу.
    """
    from hirebot.domain.enums import QuestionKind, ThresholdMode
    from hirebot.domain.scoring import (
        ProfileCriterion,
        ProfileQuestion,
        ProfileThresholds,
    )

    async with container.uow_factory() as uow:
        vacancy = await uow.vacancies.get(vacancy_id)
    if vacancy is None:
        raise NotFound(f"Вакансия {vacancy_id} не найдена")

    thresholds_payload = payload.thresholds
    thresholds = ProfileThresholds(
        invite_at=thresholds_payload.invite_at,
        human_at=thresholds_payload.human_at,
        reject_at=thresholds_payload.reject_at,
        mode=ThresholdMode(thresholds_payload.mode)
        if thresholds_payload.mode in {item.value for item in ThresholdMode}
        else ThresholdMode.ABSOLUTE,
        top_k=thresholds_payload.top_k,
        max_questions=thresholds_payload.max_questions,
        auto_invite=thresholds_payload.auto_invite,
        min_coverage_for_auto=thresholds_payload.min_coverage_for_auto,
    )
    criteria = [
        ProfileCriterion(
            key=item.key,
            title=item.title,
            weight=item.weight,
            must_have=item.must_have,
            evidence_hint=item.evidence_hint,
            expectation=item.expectation,
            source=item.source,
        )
        for item in payload.criteria
    ]
    questions = [
        ProfileQuestion(
            text=item.text,
            criterion_key=item.criterion_key,
            kind=QuestionKind(item.kind)
            if item.kind in {value.value for value in QuestionKind}
            else QuestionKind.FACT,
            informativeness=item.informativeness,
            is_custom=item.is_custom,
        )
        for item in payload.questions
    ]

    saved = await _scoring_service(container).create_draft(
        vacancy=vacancy,
        criteria=criteria,
        questions=questions,
        thresholds=thresholds,
        created_by=payload.created_by,
        comment=payload.comment,
    )
    return _profile_to_schema(saved)


@router.post(
    "/vacancies/{vacancy_id}/scoring/publish",
    response_model=ScoringProfileSchema,
    summary="Опубликовать профиль",
)
async def publish_scoring_route(
    vacancy_id: UUID,
    container: ContainerDep,
    payload: PublishProfileRequest | None = None,
) -> ScoringProfileSchema:
    """Опубликовать черновик.

    По умолчанию — в shadow-режиме: система считает и показывает, но кандидатам
    ничего не отправляется. Это режим первого запуска.
    """
    request = payload or PublishProfileRequest()
    service = _scoring_service(container)
    draft = await service.get_draft(vacancy_id)
    if draft is None:
        raise NotFound("Нет черновика для публикации")
    published = await service.publish(draft.id, as_shadow=request.as_shadow)
    return _profile_to_schema(published)


@router.get(
    "/vacancies/{vacancy_id}/scoring/versions",
    response_model=list[ScoringProfileSchema],
    summary="История версий профиля",
)
async def list_scoring_versions_route(
    vacancy_id: UUID, container: ContainerDep
) -> list[ScoringProfileSchema]:
    versions = await _scoring_service(container).list_versions(vacancy_id)
    return [_profile_to_schema(item) for item in versions]


@router.post(
    "/vacancies/{vacancy_id}/scoring/rollback",
    response_model=ScoringProfileSchema,
    summary="Откатить на прежнюю версию",
)
async def rollback_scoring_route(
    vacancy_id: UUID,
    container: ContainerDep,
    version: int,
) -> ScoringProfileSchema:
    restored = await _scoring_service(container).rollback(vacancy_id, version)
    return _profile_to_schema(restored)


@router.get("/queue", response_model=QueueResponse, summary="Очередь по приоритету")
async def queue_route(
    container: ContainerDep,
    vacancy_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> QueueResponse:
    """Кандидаты, отсортированные по приоритету: кому писать первым."""
    from hirebot.services.base import DEFAULT_COMPANY_ID

    items = await _priority_service(container).queue(
        company_id=DEFAULT_COMPANY_ID, vacancy_id=vacancy_id, limit=limit
    )
    return QueueResponse(
        total=len(items),
        items=[
            QueueItemSchema(
                conversation_id=item.conversation_id,
                candidate_name=item.candidate_name,
                state=item.state.value,
                percent=item.percent,
                coverage=item.coverage,
                priority=item.priority,
                blockers=list(item.blockers),
                age_hours=item.age_hours,
                contributions=item.contributions,
                vacancy_title=item.vacancy_title,
            )
            for item in items
        ],
    )


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
