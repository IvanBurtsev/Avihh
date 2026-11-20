"""Приём уведомлений площадок (вебхуки).

Что обязан делать этот эндпоинт и почему именно так:

1. **Проверить подпись/секрет.** Без этого любой желающий может заставить бота
   опрашивать площадку или обрабатывать диалоги. Секрет берётся из
   ``settings.security.webhook_secret``; если он не задан в нелокальном
   окружении — ``503``, а не «работаем как есть».
2. **Обеспечить идемпотентность.** Площадки повторяют доставку при таймауте
   (at-least-once), поэтому повтор события не должен приводить к повторной
   обработке. Ключ события берётся из заголовка (``X-Idempotency-Key`` /
   ``X-Event-Id``) или из поля тела, а в крайнем случае — из хэша тела.
3. **Ответить быстро (202) и поставить задачу в очередь.** Тяжёлая работа
   (резюме, LLM, календарь) в HTTP-запросе не выполняется: площадка ждёт ответ
   секунды, а обработка занимает минуты.

Тело запроса и его фрагменты в лог не пишутся: там персональные данные.
"""

from __future__ import annotations

import hashlib
import hmac
from secrets import compare_digest
from typing import Final

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError

from hirebot.api.actions import require_channel
from hirebot.api.deps import ContainerDep, SettingsDep, webhook_secret_or_none
from hirebot.api.schemas import WebhookAccepted, WebhookPayload
from hirebot.config import Settings
from hirebot.container import Container
from hirebot.domain.enums import Channel
from hirebot.logging_setup import get_logger
from hirebot.worker.tasks import JOB_POLL_CHANNEL, JOB_PROCESS_CONVERSATION

logger = get_logger(__name__)

router = APIRouter(tags=["webhooks"])

#: Заголовки, которые площадки используют для идентификатора события.
IDEMPOTENCY_HEADERS: Final[tuple[str, ...]] = ("X-Idempotency-Key", "X-Event-Id")

#: Подпись тела (``sha256=<hex>``) и — как более простой вариант — сам секрет.
SIGNATURE_HEADER: Final = "X-Hirebot-Signature"
SECRET_HEADER: Final = "X-Webhook-Secret"

#: Сколько помнить событие, чтобы отбросить повтор. Сутки — с запасом на ретраи.
DEDUPE_TTL_S: Final = 86_400.0

#: Предупреждение о выключенной проверке подписи печатаем один раз.
_warned_signature_skipped = False


def _signature_matches(body: bytes, provided: str, secret: str) -> bool:
    """Сравнить подпись или «сырой» секрет без утечки по времени."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    candidates = {provided, provided.removeprefix("sha256="), provided.removeprefix("sha256=").lower()}
    if any(compare_digest(candidate.encode("utf-8"), digest.encode("utf-8")) for candidate in candidates):
        return True
    # Некоторые площадки не считают HMAC и просто повторяют секрет в заголовке.
    return compare_digest(provided.encode("utf-8"), secret.encode("utf-8"))


def verify_webhook(request: Request, body: bytes, settings: Settings) -> None:
    """Проверить подпись/секрет вебхука.

    Секрет не задан: локально — предупреждаем и пропускаем (демо без ключей),
    иначе ``503``. Секрет задан, но подпись неверна — ``401``.
    """
    global _warned_signature_skipped
    secret = webhook_secret_or_none(settings)
    if secret is None:
        if not _warned_signature_skipped:
            _warned_signature_skipped = True
            logger.warning(
                "webhook_signature_not_configured",
                hint="HIREBOT_SECURITY__WEBHOOK_SECRET не задан: подпись вебхуков не проверяется",
                env=settings.env,
            )
        return
    provided = request.headers.get(SIGNATURE_HEADER) or request.headers.get(SECRET_HEADER) or ""
    if not provided or not _signature_matches(body, provided, secret):
        logger.warning(
            "webhook_signature_rejected",
            path=request.url.path,
            has_signature=bool(request.headers.get(SIGNATURE_HEADER)),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Подпись вебхука не совпала",
        )


def event_key(request: Request, body: bytes, payload: WebhookPayload) -> str:
    """Определить ключ идемпотентности события.

    Порядок предпочтений: заголовок -> поле тела -> хэш тела. Хэш тела —
    последний рубеж: без него повторная доставка одного и того же уведомления
    запустила бы обработку заново.
    """
    for header in IDEMPOTENCY_HEADERS:
        value = request.headers.get(header)
        if value:
            return value.strip()
    for candidate in (payload.event_id, payload.id, payload.response_id):
        if candidate:
            return candidate
    return f"body:{hashlib.sha256(body).hexdigest()}"


async def claim_event(container: Container, channel: Channel, key: str) -> bool:
    """Захватить событие; ``False`` — событие уже обрабатывалось.

    ``CachePort.incr`` атомарен (в Redis — ``INCR``), поэтому два одновременных
    одинаковых вебхука не пройдут оба.
    """
    cache_key = f"webhook:{channel.value}:{key}"
    return await container.cache.incr(cache_key, ttl_s=DEDUPE_TTL_S) <= 1


async def release_event(container: Container, channel: Channel, key: str) -> None:
    """Снять отметку о событии, если поставить задачу не удалось.

    Иначе площадка получит ``5xx``, повторит доставку, а мы отбросим её как
    дубль — и уведомление потеряется навсегда.
    """
    await container.cache.delete(f"webhook:{channel.value}:{key}")


async def parse_payload(request: Request) -> WebhookPayload:
    """Разобрать тело уведомления максимально терпимо.

    Тело вебхука — данные от чужой системы: у площадок нет ни общей схемы, ни
    гарантии формата, а ``422`` заставит их повторять доставку бесконечно.
    Поэтому:

    * не JSON или не объект — считаем, что полей нет (сработает поллинг канала);
    * поля не того типа — пишем в лог *имена* проблемных полей (без значений,
      там персональные данные) и продолжаем без них.

    Дедупликация при этом не страдает: ключ события берётся из заголовка, а если
    его нет — из хэша тела.
    """
    try:
        raw = await request.json()
    except Exception:
        logger.debug("webhook_payload_not_json", path=request.url.path)
        return WebhookPayload()
    if not isinstance(raw, dict):
        logger.debug("webhook_payload_not_object", path=request.url.path)
        return WebhookPayload()
    try:
        return WebhookPayload.model_validate(raw)
    except ValidationError as exc:
        fields = [".".join(str(part) for part in error.get("loc", ())) for error in exc.errors()]
        logger.warning("webhook_payload_invalid", path=request.url.path, fields=fields)
        return WebhookPayload()


@router.post(
    "/webhooks/{channel}",
    response_model=WebhookAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Уведомление площадки о новом событии",
    responses={
        401: {"description": "Подпись не совпала"},
        503: {"description": "Канал или секрет вебхука не сконфигурированы"},
    },
    openapi_extra={
        "requestBody": {
            "required": False,
            "content": {
                "application/json": {
                    "schema": {"type": "object", "additionalProperties": True},
                }
            },
            "description": (
                "Уведомление площадки. Обязательных полей нет: известные поля "
                "(event_id, conversation_id, response_id) используются для "
                "идемпотентности и маршрутизации, остальные сохраняются как есть."
            ),
        }
    },
)
async def receive_webhook(
    channel: Channel,
    request: Request,
    container: ContainerDep,
    settings: SettingsDep,
) -> WebhookAccepted:
    """Принять уведомление, отбросить дубли и поставить задачу в очередь."""
    body = await request.body()
    verify_webhook(request, body, settings)
    require_channel(container, channel)

    event = await parse_payload(request)
    key = event_key(request, body, event)

    if not await claim_event(container, channel, key):
        logger.info("webhook_duplicate", channel=channel.value, event_id=key)
        return WebhookAccepted(channel=channel, job="none", event_id=key, duplicate=True)

    if event.conversation_id is not None:
        job = JOB_PROCESS_CONVERSATION
        job_payload: dict[str, object] = {"conversation_id": str(event.conversation_id)}
    else:
        job = JOB_POLL_CHANNEL
        job_payload = {"channel": channel.value}

    try:
        await container.queue.enqueue(job, job_payload, dedupe_key=f"webhook:{channel.value}:{key}")
    except Exception as exc:
        await release_event(container, channel, key)
        logger.error(
            "webhook_enqueue_failed",
            channel=channel.value,
            job=job,
            error_type=type(exc).__name__,
        )
        msg = "Не удалось поставить событие в очередь, повторите доставку"
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=msg) from exc

    logger.info("webhook_accepted", channel=channel.value, job=job, event_id=key)
    return WebhookAccepted(channel=channel, job=job, event_id=key)


__all__ = [
    "DEDUPE_TTL_S",
    "IDEMPOTENCY_HEADERS",
    "SECRET_HEADER",
    "SIGNATURE_HEADER",
    "claim_event",
    "event_key",
    "parse_payload",
    "receive_webhook",
    "release_event",
    "router",
    "verify_webhook",
]
