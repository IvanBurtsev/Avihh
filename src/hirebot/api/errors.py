"""Единая обработка ошибок HTTP API.

Принципы:

1. **Один формат ошибки** — :class:`~hirebot.api.schemas.ErrorResponse` для всех
   случаев: доменной ошибки, ошибки валидации, ``HTTPException`` и падения.
2. **Наружу — только ``error_id`` и общее сообщение.** Текст исключения может
   содержать имена, телефоны и адреса кандидатов, поэтому в ответ он не попадает:
   клиент получает идентификатор, по которому инцидент находится в логе.
3. **Доменные ошибки → HTTP-коды** одной таблицей соответствия, а не разбросанными
   ``try/except`` в роутерах.

Что попадает в лог: тип ошибки, код, путь, ``error_id``. Текст доменной ошибки
пишется только на уровне DEBUG: он формируется нашим кодом, но может содержать
данные кандидата, а логи по требованию 152-ФЗ не должны накапливать ПДн.
Тела запросов и полезная нагрузка вебхуков не логируются никогда.
"""

from __future__ import annotations

from typing import Final
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from hirebot.api.schemas import ErrorResponse
from hirebot.domain.errors import (
    AuthExpired,
    ChannelNotConfigured,
    ChannelUnavailable,
    ConfigError,
    DomainError,
    DuplicateAction,
    GuardrailViolation,
    LlmError,
    NotFound,
    PolicyViolation,
    RateLimited,
    ResumeAccessDenied,
    SlotConflict,
)
from hirebot.logging_setup import get_logger
from hirebot.observability.metrics import record_domain_error

logger = get_logger(__name__)

#: Заголовок с идентификатором инцидента: клиент показывает его в тикете поддержки.
ERROR_ID_HEADER: Final = "X-Error-ID"

#: Таблица «доменная ошибка -> HTTP». Порядок важен: сначала потомки,
#: потом базовые типы (``RateLimited`` до ``ChannelUnavailable``,
#: ``ChannelNotConfigured`` до ``ConfigError``).
_ERROR_MAP: Final[tuple[tuple[type[DomainError], int, str, str], ...]] = (
    (RateLimited, 429, "rate_limited", "Площадка ограничила частоту запросов, повторите позже"),
    (PolicyViolation, 409, "policy_violation", "Действие запрещено политикой безопасности"),
    (SlotConflict, 409, "slot_conflict", "Выбранный слот уже занят"),
    (NotFound, 404, "not_found", "Объект не найден"),
    (
        ChannelNotConfigured,
        503,
        "channel_not_configured",
        "Канал не сконфигурирован или отключён",
    ),
    (AuthExpired, 502, "auth_expired", "Требуется переавторизация во внешней системе"),
    (ResumeAccessDenied, 403, "resume_access_denied", "Резюме недоступно"),
    (ChannelUnavailable, 503, "channel_unavailable", "Внешняя система недоступна"),
    (LlmError, 502, "llm_error", "LLM-провайдер вернул ошибку"),
    (GuardrailViolation, 422, "guardrail_violation", "Текст не прошёл проверку безопасности"),
    # Дубль — не ошибка: повтор идемпотентного действия безопасен.
    (DuplicateAction, 200, "duplicate", "Действие уже выполнялось ранее"),
    (ConfigError, 503, "config_error", "Ошибка конфигурации сервиса"),
    (DomainError, 500, "domain_error", "Внутренняя ошибка обработки"),
)


def new_error_id() -> str:
    """Короткий идентификатор инцидента (попадает и в ответ, и в лог)."""
    return uuid4().hex[:16]


def _classify(exc: Exception) -> tuple[int, str, str]:
    """Определить HTTP-код, код ошибки и безопасное сообщение."""
    for error_type, status_code, code, message in _ERROR_MAP:
        if isinstance(exc, error_type):
            return status_code, code, message
    return 500, "internal_error", "Внутренняя ошибка сервиса"


def _retry_after(exc: Exception) -> float | None:
    """``Retry-After`` берём у ошибок, которые знают, когда вернуться."""
    value = getattr(exc, "retry_after_s", None)
    return float(value) if isinstance(value, int | float) else None


def error_response(
    *,
    status_code: int,
    error_id: str,
    code: str,
    message: str,
    retry_after_s: float | None = None,
) -> JSONResponse:
    """Собрать ответ об ошибке в едином формате."""
    body = ErrorResponse(error_id=error_id, code=code, message=message, retry_after_s=retry_after_s)
    headers = {ERROR_ID_HEADER: error_id}
    if retry_after_s is not None:
        # Округляем вверх: клиент не должен повторять раньше времени.
        headers["Retry-After"] = str(max(1, int(retry_after_s + 0.999)))
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"), headers=headers)


def _log_request(request: Request) -> dict[str, str]:
    """Минимальный набор полей запроса для лога (без тела и query-значений)."""
    return {"method": request.method, "path": request.url.path}


async def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Доменные ошибки: предсказуемый код ответа, детали — в лог."""
    error_id = new_error_id()
    status_code, code, message = _classify(exc)
    # Ошибки по типам — единственная метрика, по которой видно, что именно ломается.
    record_domain_error(exc)
    logger.warning(
        "api_domain_error",
        error_id=error_id,
        code=code,
        status_code=status_code,
        error_type=type(exc).__name__,
        **_log_request(request),
    )
    logger.debug("api_domain_error_detail", error_id=error_id, detail=str(exc))
    return error_response(
        status_code=status_code,
        error_id=error_id,
        code=code,
        message=message,
        retry_after_s=_retry_after(exc),
    )


async def http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """``HTTPException``: сообщение задаём мы сами, поэтому его можно показать."""
    error_id = new_error_id()
    status_code = getattr(exc, "status_code", 500)
    detail = getattr(exc, "detail", None)
    message = detail if isinstance(detail, str) and detail else "Запрос отклонён"
    logger.info(
        "api_http_error",
        error_id=error_id,
        status_code=status_code,
        **_log_request(request),
    )
    return error_response(status_code=status_code, error_id=error_id, code="http_error", message=message)


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Ошибки валидации: наружу — без значений полей (там бывают ПДн)."""
    error_id = new_error_id()
    locations: list[str] = []
    if isinstance(exc, RequestValidationError):
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ()))
            locations.append(f"{location}:{error.get('type', 'invalid')}")
    logger.info("api_validation_error", error_id=error_id, fields=locations, **_log_request(request))
    return error_response(
        status_code=422,
        error_id=error_id,
        code="validation_error",
        message="Некорректный запрос: проверьте обязательные поля и их типы",
    )


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Необработанное исключение: трассировка в лог, наружу — только ``error_id``."""
    error_id = new_error_id()
    logger.error(
        "api_unhandled_error",
        error_id=error_id,
        error_type=type(exc).__name__,
        exc_info=exc,
        **_log_request(request),
    )
    return error_response(
        status_code=500,
        error_id=error_id,
        code="internal_error",
        message="Внутренняя ошибка сервиса, обратитесь в поддержку с error_id",
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Подключить обработчики ошибок к приложению."""
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)


__all__ = [
    "ERROR_ID_HEADER",
    "domain_error_handler",
    "error_response",
    "http_error_handler",
    "install_exception_handlers",
    "new_error_id",
    "unexpected_error_handler",
    "validation_error_handler",
]
