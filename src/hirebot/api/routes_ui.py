"""HTML-интерфейс рекрутера.

Решения, которые здесь важно понимать:

1. **Серверный рендеринг, формы — обычный POST.** Интерфейс полностью работает
   без JavaScript: каждое действие — форма, ответ — редирект (PRG), результат —
   flash-сообщение. Это надёжнее SPA для инструмента, которым пользуются
   время от времени, и не требует сборки фронтенда.
2. **Никакой бизнес-логики.** Роутеры вызывают те же сервисы, что API и воркер;
   доменные ошибки превращаются в понятное сообщение, а не в 500.
3. **Сессия и CSRF** — см. ``ui_security``. Каждый POST проверяет токен.
4. **Персональные данные не логируются.** В лог идут идентификаторы и действия,
   но не тексты сообщений и не содержимое резюме.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Form, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from hirebot import __version__
from hirebot.api.deps import (
    ContainerDep,
    ConversationServiceDep,
    IngestServiceDep,
    SchedulingServiceDep,
    SettingsDep,
)
from hirebot.api.scoring_forms import (
    build_groups,
    check_thresholds_order,
    parse_scoring_form,
)
from hirebot.api.ui_security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    check_key,
    configured_secret,
    csrf_token,
    is_authenticated,
    is_open_access,
    issue_session,
    safe_next,
    verify_csrf,
)
from hirebot.api.ui_views import (
    build_conversation,
    build_queue,
    draft_view,
    format_moment,
    slots_view,
)
from hirebot.config import Settings
from hirebot.container import Container
from hirebot.criteria import DEFAULT_CATALOG_PATH
from hirebot.domain.enums import Channel, ConversationState
from hirebot.domain.errors import (
    ChannelNotConfigured,
    ChannelUnavailable,
    DomainError,
    NotFound,
    PolicyViolation,
    RateLimited,
    SlotConflict,
)
from hirebot.logging_setup import get_logger

logger = get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter(prefix="/ui", include_in_schema=False)

#: Ограничения ввода: длиннее в интерфейсе писать незачем.
MAX_MESSAGE_CHARS = 2000

#: Сколько диалогов показывать в очереди за один раз.
QUEUE_LIMIT = 100


# ---------------------------------------------------------------------------
# Общие помощники
# ---------------------------------------------------------------------------


def _timezone(settings: Settings) -> str:
    return settings.talantix.default_timezone or "Europe/Moscow"


def _guard(request: Request, settings: Settings) -> RedirectResponse | None:
    """Пропустить к UI или вернуть редирект на вход."""
    if is_authenticated(request, settings):
        return None
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(
        url=f"/ui/login?{urlencode({'next': target})}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _flash_url(path: str, message: str, *, kind: str = "info", **extra: str) -> str:
    params = {"flash": message, "flash_kind": kind, **extra}
    return f"{path}?{urlencode(params)}"


def _render(
    request: Request,
    template: str,
    context: dict[str, Any],
    *,
    settings: Settings,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    """Отрисовать шаблон и выставить CSRF-куку, если её ещё нет."""
    token = csrf_token(request)
    base: dict[str, Any] = {
        "csrf_token": token,
        "version": __version__,
        "open_access": is_open_access(settings),
        "session_user": not is_open_access(settings),
        "flash": request.query_params.get("flash"),
        "flash_kind": request.query_params.get("flash_kind", "info"),
        "state_filter": request.query_params.get("state"),
        "active_nav": "",
        "pending_total": 0,
    }
    base.update(context)
    response = templates.TemplateResponse(request, template, base, status_code=status_code)
    if request.cookies.get(CSRF_COOKIE) != token:
        response.set_cookie(
            CSRF_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            secure=not settings.is_local,
            max_age=12 * 3600,
            path="/",
        )
    return response


def _domain_error_message(exc: DomainError) -> tuple[str, str]:
    """Человеческое сообщение и вид баннера для доменной ошибки.

    Роутер не должен показывать рекрутеру текст исключения: там бывают технические
    детали. Но и «что-то пошло не так» бесполезно — сообщение объясняет, что делать.
    """
    if isinstance(exc, SlotConflict):
        return ("Это время уже занято. Выберите другой слот.", "warn")
    if isinstance(exc, PolicyViolation):
        return (f"Действие запрещено правилами: {exc}", "warn")
    if isinstance(exc, RateLimited):
        pause = f" Повторите через {int(exc.retry_after_s)} с." if exc.retry_after_s else ""
        return (f"Площадка ограничила частоту запросов.{pause}", "warn")
    if isinstance(exc, ChannelNotConfigured):
        return ("Площадка не подключена. Проверьте настройки доступа.", "error")
    if isinstance(exc, ChannelUnavailable):
        return ("Площадка недоступна, попробуйте позже.", "error")
    if isinstance(exc, NotFound):
        return (str(exc), "warn")
    return ("Не удалось выполнить действие. Подробности в журнале сервиса.", "error")


# ---------------------------------------------------------------------------
# Вход и выход
# ---------------------------------------------------------------------------


@router.get("/", include_in_schema=False)
async def ui_root() -> RedirectResponse:
    return RedirectResponse(url="/ui/queue", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    settings: SettingsDep,
    next_url: Annotated[str | None, Query(alias="next")] = None,
) -> Response:
    """Форма входа. В локальном режиме без секрета вход не нужен."""
    if is_authenticated(request, settings):
        return RedirectResponse(url=safe_next(next_url), status_code=status.HTTP_303_SEE_OTHER)
    if configured_secret(settings) is None:
        return _render(
            request,
            "login.html",
            {
                "error": (
                    "Ключ доступа не задан на сервере. Задайте "
                    "HIREBOT_SECURITY__WEBHOOK_SECRET и перезапустите сервис."
                ),
                "next_url": safe_next(next_url),
            },
            settings=settings,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return _render(
        request,
        "login.html",
        {"error": None, "next_url": safe_next(next_url)},
        settings=settings,
    )


@router.post("/login")
async def login_submit(
    request: Request,
    settings: SettingsDep,
    key: Annotated[str, Form()] = "",
    csrf: Annotated[str | None, Form(alias="csrf_token")] = None,
    next_url: Annotated[str | None, Form(alias="next")] = None,
) -> Response:
    """Проверить ключ и выдать подписанную куку сессии."""
    verify_csrf(request, csrf)
    secret = configured_secret(settings)
    if secret is None:
        return RedirectResponse(url="/ui/login", status_code=status.HTTP_303_SEE_OTHER)
    if not check_key(settings, key):
        logger.warning("ui_login_failed")
        return _render(
            request,
            "login.html",
            {"error": "Неверный ключ доступа", "next_url": safe_next(next_url)},
            settings=settings,
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    logger.info("ui_login_ok")
    response = RedirectResponse(url=safe_next(next_url), status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(secret),
        httponly=True,
        samesite="lax",
        secure=not settings.is_local,
        max_age=12 * 3600,
        path="/",
    )
    return response


@router.post("/logout")
async def logout(
    request: Request,
    csrf: Annotated[str | None, Form(alias="csrf_token")] = None,
) -> Response:
    """Выйти: удалить куку сессии. CSRF проверяется и здесь."""
    verify_csrf(request, csrf)
    response = RedirectResponse(url="/ui/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# ---------------------------------------------------------------------------
# Очередь подтверждения
# ---------------------------------------------------------------------------


async def _approval_service(container: Container) -> Any:
    """``ApprovalService`` из контейнера (ленивый импорт, как у остальных сервисов)."""
    from hirebot.services.approval import ApprovalService

    return ApprovalService(container)


@router.get("/approvals", response_class=HTMLResponse)
async def approvals_page(
    request: Request,
    settings: SettingsDep,
    container: ContainerDep,
) -> Response:
    """Черновики, ожидающие решения рекрутера."""
    redirect = _guard(request, settings)
    if redirect is not None:
        return redirect

    service = await _approval_service(container)
    pending = await service.list_pending()
    drafts = [draft_view(item, timezone=_timezone(settings)) for item in pending]
    return _render(
        request,
        "approvals.html",
        {"drafts": drafts, "active_nav": "approvals", "pending_total": len(drafts)},
        settings=settings,
    )


@router.post("/approvals/{draft_id}/approve")
async def approve_draft(
    request: Request,
    draft_id: UUID,
    settings: SettingsDep,
    container: ContainerDep,
    csrf: Annotated[str | None, Form(alias="csrf_token")] = None,
    text: Annotated[str, Form()] = "",
) -> Response:
    """Одобрить черновик и отправить сообщение кандидату.

    Рекрутер может отредактировать текст: тогда отправляется его версия, и
    ответственность за формулировку на нём.
    """
    redirect = _guard(request, settings)
    if redirect is not None:
        return redirect
    verify_csrf(request, csrf)

    body = (text or "").strip()
    if not body:
        return RedirectResponse(
            url=_flash_url("/ui/approvals", "Текст сообщения не может быть пустым", kind="warn"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if len(body) > MAX_MESSAGE_CHARS:
        return RedirectResponse(
            url=_flash_url("/ui/approvals", f"Сообщение длиннее {MAX_MESSAGE_CHARS} символов", kind="warn"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    service = await _approval_service(container)
    try:
        await service.approve(draft_id, reviewer="recruiter", text=body)
    except DomainError as exc:
        message, kind = _domain_error_message(exc)
        return RedirectResponse(
            url=_flash_url("/ui/approvals", message, kind=kind),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(
        url=_flash_url("/ui/approvals", "Сообщение отправлено кандидату", kind="ok"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/approvals/{draft_id}/reject")
async def reject_draft(
    request: Request,
    draft_id: UUID,
    settings: SettingsDep,
    container: ContainerDep,
    csrf: Annotated[str | None, Form(alias="csrf_token")] = None,
    reason: Annotated[str | None, Form()] = None,
) -> Response:
    """Отклонить черновик: кандидату ничего не уходит, диалог остаётся человеку."""
    redirect = _guard(request, settings)
    if redirect is not None:
        return redirect
    verify_csrf(request, csrf)

    service = await _approval_service(container)
    try:
        await service.reject(draft_id, reviewer="recruiter", reason=reason)
    except DomainError as exc:
        message, kind = _domain_error_message(exc)
        return RedirectResponse(
            url=_flash_url("/ui/approvals", message, kind=kind),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(
        url=_flash_url("/ui/approvals", "Черновик отклонён", kind="info"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Вакансии и настройка критериев
# ---------------------------------------------------------------------------


def _scoring_service(container: Container) -> Any:
    from hirebot.services.scoring_profile import ScoringProfileService

    return ScoringProfileService(container)


@router.get("/vacancies/new", response_class=HTMLResponse)
async def vacancy_new_page(
    request: Request,
    settings: SettingsDep,
) -> Response:
    """Форма создания вакансии."""
    redirect = _guard(request, settings)
    if redirect is not None:
        return redirect
    return _render(
        request,
        "vacancy_new.html",
        {
            "form": {},
            "channels": [
                {"value": Channel.HH.value, "label": "hh.ru"},
                {"value": Channel.AVITO.value, "label": "Avito"},
            ],
            "active_nav": "vacancies",
        },
        settings=settings,
    )


@router.post("/vacancies")
async def vacancy_create(
    request: Request,
    settings: SettingsDep,
    container: ContainerDep,
    csrf: Annotated[str | None, Form(alias="csrf_token")] = None,
    title: Annotated[str, Form()] = "",
    city: Annotated[str | None, Form()] = None,
    description: Annotated[str, Form()] = "",
    channel: Annotated[str, Form()] = "hh",
) -> Response:
    """Создать вакансию и сразу перейти к настройке критериев."""
    redirect = _guard(request, settings)
    if redirect is not None:
        return redirect
    verify_csrf(request, csrf)

    clean_title = (title or "").strip()
    if not clean_title:
        return RedirectResponse(
            url=_flash_url("/ui/vacancies/new", "Название вакансии обязательно", kind="warn"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    from uuid import uuid4

    from hirebot.domain.entities import Company, Vacancy, new_id
    from hirebot.services.base import DEFAULT_COMPANY_ID

    try:
        channel_value = Channel(channel)
    except ValueError:
        channel_value = Channel.HH

    async with container.uow_factory() as uow:
        await uow.companies.upsert(
            Company(
                id=DEFAULT_COMPANY_ID,
                name="Компания",
                slug=f"company-{DEFAULT_COMPANY_ID.hex[:8]}",
            )
        )
        vacancy = await uow.vacancies.upsert(
            Vacancy(
                id=new_id(),
                channel=channel_value,
                external_id=f"manual-{uuid4().hex[:12]}",
                title=clean_title,
                company_id=DEFAULT_COMPANY_ID,
                description=(description or "").strip(),
                city=(city or "").strip() or None,
            )
        )
        await uow.commit()

    logger.info("ui_vacancy_created", vacancy_id=str(vacancy.id), channel=channel_value.value)
    return RedirectResponse(
        url=_flash_url(
            f"/ui/vacancies/{vacancy.id}/scoring",
            "Вакансия создана. Задайте критерии скоринга.",
            kind="ok",
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/vacancies", response_class=HTMLResponse)
async def vacancies_page(
    request: Request,
    settings: SettingsDep,
    container: ContainerDep,
) -> Response:
    """Список вакансий и состояние их профилей скоринга."""
    redirect = _guard(request, settings)
    if redirect is not None:
        return redirect

    service = _scoring_service(container)
    async with container.uow_factory() as uow:
        vacancies = await uow.vacancies.list_active(listing_company())
    rows = []
    for vacancy in vacancies:
        live = await service.get_live(vacancy.id)
        draft = await service.get_draft(vacancy.id)
        # Показываем то, по чему реально считаются кандидаты: активную версию.
        # Черновик — отдельный признак «есть неприменённые правки».
        profile = live or draft
        # «Обновлена» — это последнее изменение настроек, а не карточки вакансии:
        # иначе после сохранения критериев дата не меняется и кажется, что
        # ничего не сохранилось.
        moments = [vacancy.updated_at]
        if profile is not None:
            moments.append(profile.published_at or profile.created_at)
        if draft is not None:
            moments.append(draft.created_at)
        latest = max(item for item in moments if item is not None)
        rows.append(
            {
                "id": vacancy.id,
                "title": vacancy.title,
                "city": vacancy.city,
                "channel": vacancy.channel.value,
                "criteria_count": len(profile.criteria) if profile else len(vacancy.criteria),
                "profile_version": profile.version if profile else None,
                "profile_status": profile.status.value if profile else None,
                "has_draft": draft is not None and (live is None or draft.version != live.version),
                "updated_at": format_moment(latest, timezone=_timezone(settings)),
            }
        )
    return _render(
        request,
        "vacancies.html",
        {"vacancies": rows, "active_nav": "vacancies"},
        settings=settings,
    )


def listing_company() -> UUID:
    """Компания по умолчанию для списка вакансий.

    Мультитенантность заложена в схему, но выбора компании в интерфейсе пока
    нет: используется та же компания, что и в воркере.
    """
    from hirebot.services.base import DEFAULT_COMPANY_ID

    return DEFAULT_COMPANY_ID


def _criterion_form_name(prefix: str, key: str) -> str:
    return f"{prefix}_{key}"


@router.get("/vacancies/{vacancy_id}/scoring", response_class=HTMLResponse)
async def scoring_page(
    request: Request,
    vacancy_id: UUID,
    settings: SettingsDep,
    container: ContainerDep,
) -> Response:
    """Экран настройки критериев: три шага в одной форме."""
    redirect = _guard(request, settings)
    if redirect is not None:
        return redirect

    from hirebot.criteria import load_catalog

    async with container.uow_factory() as uow:
        vacancy = await uow.vacancies.get(vacancy_id)
    if vacancy is None:
        return RedirectResponse(
            url=_flash_url("/ui/vacancies", "Вакансия не найдена", kind="warn"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    service = _scoring_service(container)
    live = await service.get_live(vacancy_id)
    draft = await service.get_draft(vacancy_id)
    profile = draft or live

    selected: dict[str, dict[str, Any]] = {}
    if profile is not None:
        for item in profile.criteria:
            selected[item.key] = {
                "weight": item.weight,
                "must_have": item.must_have,
                "title": item.title,
                "expectation": item.expectation,
            }

    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    groups = build_groups(catalog, selected)

    existing_questions = list(profile.questions) if profile is not None else []
    question_slots = []
    for index in range(5):
        item = existing_questions[index] if index < len(existing_questions) else None
        question_slots.append(
            {
                "index": index,
                "criterion_key": item.criterion_key if item else "",
                "text": item.text if item else "",
            }
        )

    thresholds = (
        profile.thresholds
        if profile is not None
        else __import__("hirebot.domain.scoring", fromlist=["ProfileThresholds"]).ProfileThresholds()
    )

    selectable = [
        {"key": item.key, "title": item.title} for item in (profile.criteria if profile is not None else [])
    ] or [{"key": item.key, "title": item.title} for item in catalog.criteria]

    return _render(
        request,
        "scoring.html",
        {
            "vacancy_id": vacancy.id,
            "vacancy_title": vacancy.title,
            "groups": groups,
            "selected_count": len(selected),
            "question_slots": question_slots,
            "selectable_criteria": selectable,
            "thresholds": thresholds,
            "profile": profile,
            "active_nav": "vacancies",
        },
        settings=settings,
    )


@router.post("/vacancies/{vacancy_id}/scoring")
async def scoring_submit(
    request: Request,
    vacancy_id: UUID,
    settings: SettingsDep,
    container: ContainerDep,
    csrf: Annotated[str | None, Form(alias="csrf_token")] = None,
    action: Annotated[str, Form()] = "draft",
) -> Response:
    """Сохранить черновик профиля и (опционально) опубликовать в shadow-режиме."""
    redirect = _guard(request, settings)
    if redirect is not None:
        return redirect
    verify_csrf(request, csrf)

    path = f"/ui/vacancies/{vacancy_id}/scoring"
    form = await request.form()

    async with container.uow_factory() as uow:
        vacancy = await uow.vacancies.get(vacancy_id)
    if vacancy is None:
        return RedirectResponse(
            url=_flash_url("/ui/vacancies", "Вакансия не найдена", kind="warn"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    from hirebot.criteria import load_catalog

    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    parsed = parse_scoring_form(form, catalog)
    order_problem = check_thresholds_order(parsed)
    if order_problem:
        return RedirectResponse(
            url=_flash_url(path, order_problem, kind="warn"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if not parsed.is_valid:
        return RedirectResponse(
            url=_flash_url(path, "; ".join(parsed.errors[:2]), kind="warn"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    service = _scoring_service(container)
    try:
        draft = await service.create_draft(
            vacancy=vacancy,
            criteria=parsed.criteria,
            questions=parsed.questions,
            thresholds=parsed.thresholds,
            created_by="recruiter",
        )
        if action == "publish":
            await service.publish(draft.id, as_shadow=True)
    except (ValueError, DomainError) as exc:
        message, kind = _domain_error_message(exc) if isinstance(exc, DomainError) else (str(exc), "warn")
        logger.warning("scoring_save_failed", vacancy_id=str(vacancy_id), error=str(exc))
        return RedirectResponse(
            url=_flash_url(path, message, kind=kind), status_code=status.HTTP_303_SEE_OTHER
        )

    text = (
        f"Профиль v{draft.version} опубликован в режиме проверки (shadow)"
        if action == "publish"
        else f"Черновик v{draft.version} сохранён"
    )
    logger.info("scoring_saved", vacancy_id=str(vacancy_id), version=draft.version, action=action)
    return RedirectResponse(url=_flash_url(path, text, kind="ok"), status_code=status.HTTP_303_SEE_OTHER)


def _int_or(value: object, default: int, *, low: int, high: int) -> int:
    """Безопасно превратить значение формы в целое в заданных границах."""
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


# ---------------------------------------------------------------------------
# Очередь
# ---------------------------------------------------------------------------


@router.get("/queue", response_class=HTMLResponse)
async def queue_page(
    request: Request,
    settings: SettingsDep,
    container: ContainerDep,
    state: str | None = None,
) -> Response:
    """Очередь диалогов с фильтром по состоянию."""
    redirect = _guard(request, settings)
    if redirect is not None:
        return redirect

    state_filter: ConversationState | None = None
    if state:
        try:
            state_filter = ConversationState(state)
        except ValueError:
            state_filter = None

    async with container.uow_factory() as uow:
        rows, counters = await build_queue(
            uow, state=state_filter, limit=QUEUE_LIMIT, timezone=_timezone(settings)
        )
        pending_total = len(await uow.drafts.list_pending(limit=1000))
    budget_summary = await _budget_summary(container)

    return _render(
        request,
        "queue.html",
        {
            "rows": rows,
            "counters": counters,
            "active_nav": "queue",
            "pending_total": pending_total,
            "channels": [channel.value for channel in container.enabled_channels],
            "budget": budget_summary,
        },
        settings=settings,
    )


@router.post("/poll")
async def poll_channel(
    request: Request,
    settings: SettingsDep,
    ingest: IngestServiceDep,
    csrf: Annotated[str | None, Form(alias="csrf_token")] = None,
    channel: Annotated[str, Form()] = "hh",
) -> Response:
    """Забрать свежие отклики с площадки — то же, что делает воркер по расписанию."""
    redirect = _guard(request, settings)
    if redirect is not None:
        return redirect
    verify_csrf(request, csrf)

    try:
        channel_name = Channel(channel)
    except ValueError:
        return RedirectResponse(
            url=_flash_url("/ui/queue", f"Неизвестная площадка: {channel}", kind="error"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    try:
        result = await ingest.poll(channel_name, limit=50)
    except DomainError as exc:
        message, kind = _domain_error_message(exc)
        return RedirectResponse(
            url=_flash_url("/ui/queue", message, kind=kind),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    logger.info("ui_poll", channel=channel_name.value, created=result.created)
    text = (
        f"Откликов найдено: {result.found}, новых диалогов: {result.created}, "
        f"уже обработанных: {result.duplicates}"
    )
    if result.errors:
        text += f". Ошибок: {len(result.errors)}"
    return RedirectResponse(
        url=_flash_url("/ui/queue", text, kind="ok"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Карточка диалога
# ---------------------------------------------------------------------------


@router.get("/conversations/{conversation_id}", response_class=HTMLResponse)
async def conversation_page(
    request: Request,
    conversation_id: UUID,
    settings: SettingsDep,
    container: ContainerDep,
) -> Response:
    """Карточка: кандидат, скрининг с цитатами, переписка, слоты, бронь."""
    redirect = _guard(request, settings)
    if redirect is not None:
        return redirect

    timezone = _timezone(settings)
    try:
        async with container.uow_factory() as uow:
            view = await build_conversation(uow, conversation_id, timezone=timezone)
            pending_total = len(await uow.drafts.list_pending(limit=1000))
    except NotFound:
        return _render(
            request,
            "queue.html",
            {
                "rows": [],
                "counters": [],
                "active_nav": "queue",
                "channels": [channel.value for channel in container.enabled_channels],
                "flash": "Диалог не найден",
                "flash_kind": "warn",
            },
            settings=settings,
            status_code=status.HTTP_404_NOT_FOUND,
        )

    # Отправка возможна только через подключённый канал: иначе форма ведёт к
    # ошибке после нажатия. Лучше показать причину заранее.
    channel_name = _channel_of(view)
    if channel_name in container.channels:
        view.can_send = True
    else:
        view.can_send = False
        view.send_blocked_reason = (
            f"Канал {view.channel!r} не подключён: нет учётных данных. "
            "Отправка станет доступна после настройки доступа к площадке."
        )

    return _render(
        request,
        "conversation.html",
        {"view": view, "pending_total": pending_total},
        settings=settings,
    )


async def _budget_summary(container: Container) -> Any:
    """Расходы за сегодня и лимит — для плашки в шапке.

    Ошибку не пробрасываем: недоступный бюджет не должен ломать страницу,
    он просто не показывается.
    """
    from hirebot.services.base import DEFAULT_COMPANY_ID
    from hirebot.services.budget import BudgetService

    try:
        return await BudgetService(container).usage(DEFAULT_COMPANY_ID)
    except Exception:
        logger.warning("budget_summary_failed", exc_info=True)
        return None


def _channel_of(view: Any) -> Any:
    """Канал диалога в терминах контейнера (Channel, а не строка)."""
    from hirebot.domain.enums import Channel

    try:
        return Channel(view.channel)
    except ValueError:  # pragma: no cover - защита от неизвестного значения
        return None


@router.get("/conversations/{conversation_id}/messages-fragment", response_class=HTMLResponse)
async def messages_fragment(
    request: Request,
    conversation_id: UUID,
    settings: SettingsDep,
    container: ContainerDep,
) -> Response:
    """Фрагмент переписки для автообновления (вызывается из app.js)."""
    redirect = _guard(request, settings)
    if redirect is not None:
        return redirect

    try:
        async with container.uow_factory() as uow:
            view = await build_conversation(uow, conversation_id, timezone=_timezone(settings))
    except NotFound:
        return HTMLResponse('<p class="muted">Диалог не найден.</p>', status_code=404)

    return templates.TemplateResponse(
        request, "_messages.html", {"view": view, "csrf_token": csrf_token(request)}
    )


@router.post("/conversations/{conversation_id}/process")
async def process_conversation(
    request: Request,
    conversation_id: UUID,
    settings: SettingsDep,
    conversation: ConversationServiceDep,
    csrf: Annotated[str | None, Form(alias="csrf_token")] = None,
) -> Response:
    """Прогнать шаг воронки: скрининг → вопросы → приглашение."""
    redirect = _guard(request, settings)
    if redirect is not None:
        return redirect
    verify_csrf(request, csrf)

    path = f"/ui/conversations/{conversation_id}"
    try:
        outcome = await conversation.process(conversation_id)
    except DomainError as exc:
        message, kind = _domain_error_message(exc)
        logger.warning("ui_process_failed", conversation_id=str(conversation_id), error=type(exc).__name__)
        return RedirectResponse(
            url=_flash_url(path, message, kind=kind), status_code=status.HTTP_303_SEE_OTHER
        )

    logger.info(
        "ui_process_ok",
        conversation_id=str(conversation_id),
        action=outcome.action_taken,
        state=outcome.state.value,
    )
    return RedirectResponse(
        url=_flash_url(path, f"Шаг выполнен: {outcome.action_taken}", kind="ok"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/conversations/{conversation_id}/message")
async def send_message(
    request: Request,
    conversation_id: UUID,
    settings: SettingsDep,
    container: ContainerDep,
    csrf: Annotated[str | None, Form(alias="csrf_token")] = None,
    text: Annotated[str, Form()] = "",
) -> Response:
    """Отправить сообщение кандидату от имени рекрутера.

    Текст пишет человек, поэтому сообщение не проходит проверки тона, но
    проходит проверку домена: пустое и слишком длинное не отправляются.
    """
    redirect = _guard(request, settings)
    if redirect is not None:
        return redirect
    verify_csrf(request, csrf)

    path = f"/ui/conversations/{conversation_id}"
    body = (text or "").strip()
    if not body:
        return RedirectResponse(
            url=_flash_url(path, "Сообщение не может быть пустым", kind="warn"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if len(body) > MAX_MESSAGE_CHARS:
        return RedirectResponse(
            url=_flash_url(path, f"Сообщение длиннее {MAX_MESSAGE_CHARS} символов", kind="warn"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    try:
        await _send_manual_message(container, conversation_id, body)
    except DomainError as exc:
        message, kind = _domain_error_message(exc)
        return RedirectResponse(
            url=_flash_url(path, message, kind=kind), status_code=status.HTTP_303_SEE_OTHER
        )

    logger.info("ui_manual_message", conversation_id=str(conversation_id), chars=len(body))
    return RedirectResponse(
        url=_flash_url(path, "Сообщение отправлено", kind="ok"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


async def _send_manual_message(container: Container, conversation_id: UUID, body: str) -> None:
    """Отправить сообщение через канал и сохранить его в истории.

    Логика намеренно минимальна: это ручное действие человека, а не шаг бота.
    Идемпотентность обеспечивает ключ, производный от диалога и времени.
    """
    from hirebot.domain.entities import Message, new_id
    from hirebot.domain.enums import MessageDirection, MessageKind

    async with container.uow_factory() as uow:
        conversation = await uow.conversations.get_with_lock(conversation_id)
        if conversation is None:
            raise NotFound(f"Диалог {conversation_id} не найден")
        channel = container.channel(conversation.channel)
        sent = await channel.send_message(
            conversation.external_id,
            body,
            idempotency_key=f"{conversation_id}:manual:{new_id()}",
            kind_hint=MessageKind.MANUAL.value,
        )
        await uow.messages.add(
            Message(
                id=new_id(),
                conversation_id=conversation_id,
                direction=MessageDirection.OUTBOUND,
                body=body,
                kind=MessageKind.MANUAL,
                external_id=sent.external_id,
                sent_at=sent.sent_at,
            )
        )
        conversation.last_outbound_at = sent.sent_at
        await uow.conversations.upsert(conversation)
        await uow.commit()


@router.post("/conversations/{conversation_id}/slots")
async def propose_slots(
    request: Request,
    conversation_id: UUID,
    settings: SettingsDep,
    container: ContainerDep,
    scheduling: SchedulingServiceDep,
    csrf: Annotated[str | None, Form(alias="csrf_token")] = None,
) -> Response:
    """Показать свободные слоты из календаря."""
    redirect = _guard(request, settings)
    if redirect is not None:
        return redirect
    verify_csrf(request, csrf)

    path = f"/ui/conversations/{conversation_id}"
    timezone = _timezone(settings)
    try:
        slots = await scheduling.propose_slots(conversation_id=conversation_id, limit=5)
        async with container.uow_factory() as uow:
            view = await build_conversation(uow, conversation_id, timezone=timezone)
    except DomainError as exc:
        message, kind = _domain_error_message(exc)
        return RedirectResponse(
            url=_flash_url(path, message, kind=kind), status_code=status.HTTP_303_SEE_OTHER
        )

    if not slots:
        return RedirectResponse(
            url=_flash_url(
                path,
                "Свободных слотов нет: календарь не подключён или всё занято",
                kind="warn",
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    view.slots = slots_view(slots, timezone=timezone)
    return _render(
        request,
        "conversation.html",
        {"view": view, "flash": f"Свободных слотов: {len(slots)}", "flash_kind": "ok"},
        settings=settings,
    )


@router.post("/conversations/{conversation_id}/book")
async def book_interview(
    request: Request,
    conversation_id: UUID,
    settings: SettingsDep,
    scheduling: SchedulingServiceDep,
    csrf: Annotated[str | None, Form(alias="csrf_token")] = None,
    slot_start: Annotated[str, Form()] = "",
) -> Response:
    """Забронировать собеседование на выбранное время.

    Время берётся из формы как ISO-строка: именно оно, а не индекс слота,
    адресует выбор кандидата — сетка слотов могла пересчитаться.
    """
    redirect = _guard(request, settings)
    if redirect is not None:
        return redirect
    verify_csrf(request, csrf)

    path = f"/ui/conversations/{conversation_id}"
    try:
        start = datetime.fromisoformat(slot_start)
    except ValueError:
        return RedirectResponse(
            url=_flash_url(path, "Некорректное время слота", kind="error"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if start.tzinfo is None:
        return RedirectResponse(
            url=_flash_url(path, "Время слота должно содержать часовой пояс", kind="error"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    try:
        booking = await scheduling.book(
            conversation_id=conversation_id,
            slot_start=start,
            requested_by="recruiter",
        )
    except DomainError as exc:
        message, kind = _domain_error_message(exc)
        logger.warning("ui_booking_failed", conversation_id=str(conversation_id), error=type(exc).__name__)
        return RedirectResponse(
            url=_flash_url(path, message, kind=kind), status_code=status.HTTP_303_SEE_OTHER
        )

    logger.info("ui_booking_ok", conversation_id=str(conversation_id), booking_id=str(booking.id))
    return RedirectResponse(
        url=_flash_url(path, "Собеседование назначено и подтверждено в календаре", kind="ok"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


__all__ = ["router", "templates"]
