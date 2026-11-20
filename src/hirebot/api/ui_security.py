"""Аутентификация и защита UI рекрутера.

UI показывает персональные данные кандидатов, поэтому здесь действуют три
правила, каждое из которых закрывает конкретную атаку:

1. **Вход по ключу с подписанной кукой.** Секрет берётся из
   ``HIREBOT_SECURITY__WEBHOOK_SECRET``. Кука подписана HMAC-SHA256 вместе со
   сроком действия: подделать её без секрета нельзя, а истёкшая перестаёт
   работать сама. Сравнение подписей — ``compare_digest`` (защита от timing-атак).
2. **CSRF через double-submit.** Токен кладётся в куку и в скрытое поле формы;
   POST без совпадения отклоняется. Без этого чужая страница могла бы отправить
   от имени рекрутера приглашение кандидату.
3. **Local-режим — только для разработки.** Если секрет не задан локально, UI
   открыт, но в шапке висит предупреждение. В любом другом окружении вход
   обязателен, а `503` вместо молчаливо открытого доступа.

Здесь нет хранения паролей и нет пользовательских сессий в БД: один статический
ключ на инсталляцию. Полноценный RBAC — отдельная задача (см. docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import quote

from fastapi import HTTPException, Request, status

from hirebot.config import Settings

#: Имена кук. Префикс ``hirebot_`` избавляет от конфликтов при нескольких инсталляциях.
SESSION_COOKIE: Final = "hirebot_ui_session"
CSRF_COOKIE: Final = "hirebot_ui_csrf"
CSRF_FIELD: Final = "csrf_token"

#: Сколько живёт сессия рекрутера.
SESSION_TTL: Final = timedelta(hours=12)

#: Заголовки, по которым проверяется происхождение POST-запроса.
_ORIGIN_HEADERS: Final = ("origin", "referer")

_MISSING_SECRET_MESSAGE: Final = (
    "UI недоступен: не задан HIREBOT_SECURITY__WEBHOOK_SECRET. Задайте секрет и перезапустите сервис."
)


def configured_secret(settings: Settings) -> str | None:
    """Секрет UI в виде строки (``SecretStr`` не должен утекать в шаблоны)."""
    secret = settings.security.webhook_secret
    if secret is None:
        return None
    value = secret.get_secret_value().strip()
    return value or None


def is_open_access(settings: Settings) -> bool:
    """Режим без входа: допустим только локально и только без секрета."""
    return configured_secret(settings) is None and settings.is_local


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_session(secret: str, *, now: datetime | None = None) -> str:
    """Выпустить значение куки сессии ``<expires_ts>.<hmac>``."""
    expires_at = (now or datetime.now(tz=UTC)) + SESSION_TTL
    payload = str(int(expires_at.timestamp()))
    return f"{payload}.{_sign(secret, payload)}"


def verify_session(secret: str, value: str | None, *, now: datetime | None = None) -> bool:
    """Проверить подпись и срок действия куки сессии."""
    if not value:
        return False
    payload, _, signature = value.partition(".")
    if not payload or not signature:
        return False
    if not payload.isdigit():
        return False
    if not hmac.compare_digest(_sign(secret, payload), signature):
        return False
    expires_at = datetime.fromtimestamp(int(payload), tz=UTC)
    return expires_at > (now or datetime.now(tz=UTC))


def is_authenticated(request: Request, settings: Settings) -> bool:
    """Есть ли у запроса действующая сессия рекрутера (или доступ открыт локально)."""
    if is_open_access(settings):
        return True
    secret = configured_secret(settings)
    if secret is None:
        return False
    return verify_session(secret, request.cookies.get(SESSION_COOKIE))


def check_key(settings: Settings, provided: str) -> bool:
    """Сравнить введённый ключ с секретом без утечки по времени."""
    secret = configured_secret(settings)
    if secret is None:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), secret.encode("utf-8"))


def csrf_token(request: Request) -> str:
    """Токен CSRF для текущего запроса (создаётся при первом обращении)."""
    existing = request.cookies.get(CSRF_COOKIE)
    if existing and existing.isascii() and len(existing) <= 128:
        return existing
    return secrets.token_urlsafe(32)


def verify_csrf(request: Request, submitted: str | None) -> None:
    """Проверить CSRF: токен формы обязан совпасть с кукой.

    Дополнительно проверяется заголовок ``Origin``/``Referer``, если он есть:
    это отсекает подделку формы с чужого домена даже при утёкшем токене.
    """
    cookie_token = request.cookies.get(CSRF_COOKIE) or ""
    if not submitted or not cookie_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Отсутствует CSRF-токен: обновите страницу и повторите действие",
        )
    if not hmac.compare_digest(submitted, cookie_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Неверный CSRF-токен",
        )
    _check_same_origin(request)


def _check_same_origin(request: Request) -> None:
    """Если браузер прислал Origin/Referer — он обязан совпадать с нашим хостом."""
    for header in _ORIGIN_HEADERS:
        value = request.headers.get(header)
        if not value:
            continue
        host = request.headers.get("host", "")
        if not host:
            return
        if header == "origin":
            expected = request.url.scheme + "://" + host
            if value.rstrip("/") != expected.rstrip("/"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Запрос пришёл с чужого источника",
                )
            return
        # Referer содержит путь, поэтому сравниваем только схему и хост.
        expected_prefix = request.url.scheme + "://" + host
        if not value.startswith(expected_prefix):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Запрос пришёл с чужого источника",
            )
        return


def safe_next(target: str | None) -> str:
    """Куда вернуть пользователя после входа.

    Принимаем **только** путь внутри приложения: ``/ui/...``. Абсолютные адреса
    и ``//host`` отбрасываются — иначе открытый редирект уводил бы рекрутера на
    чужой сайт с валидной сессией.
    """
    if not target:
        return "/ui/queue"
    if not target.startswith("/ui/"):
        return "/ui/queue"
    if target.startswith("//") or "\\" in target:
        return "/ui/queue"
    return quote(target, safe="/?=&%#:+,;@!$'()*[]")


__all__ = [
    "CSRF_COOKIE",
    "CSRF_FIELD",
    "SESSION_COOKIE",
    "SESSION_TTL",
    "check_key",
    "configured_secret",
    "csrf_token",
    "is_authenticated",
    "is_open_access",
    "issue_session",
    "safe_next",
    "verify_csrf",
    "verify_session",
]
