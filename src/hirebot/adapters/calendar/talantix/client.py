"""GraphQL-транспорт Talantix: запрос, разбор ошибок, обновление токена.

Что здесь важно:

* Talantix — это GraphQL, но ошибки у него трёх разных природ:
  1) HTTP-ошибки предвалидации (``400``/``401``/``403``/``413``/``429``) с телом
     ``{"type", "detail"}``;
  2) технические ошибки выполнения в ``errors[]`` с ``extensions.errorType``
     (``INTERNAL`` — HTTP 210, ``EXTERNAL_SYSTEM_ERROR`` — HTTP 269);
  3) бизнес-ошибки внутри ``data`` (union-ветки ``*Error``) — их разбирает mapper.
  Клиент различает эти случаи и переводит их в разные доменные ошибки;

* ответ нужно читать при HTTP 200, 210 и 269 — «2xx» здесь не значит «успех»;

* refresh-токен **одноразовый с ротацией**: повторное использование старой пары
  аннулирует сессию, поэтому обновление строго single-flight (``asyncio.Lock``
  + ``LockPort``), а новый refresh-токен сразу сохраняется в кэш.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from hirebot.adapters.calendar.talantix.mapper import TalantixManager, parse_managers_page
from hirebot.adapters.calendar.talantix.queries import MANAGERS_QUERY
from hirebot.adapters.http_utils import error_code, error_summary, json_object, retry_after_s
from hirebot.adapters.locks import LocalLock
from hirebot.adapters.parsing import as_dict, as_list, dig, parse_datetime, str_or_empty, str_or_none
from hirebot.adapters.secrets import open_secret, seal_secret
from hirebot.config import TalantixSettings
from hirebot.domain.errors import (
    AuthExpired,
    ChannelNotConfigured,
    ChannelUnavailable,
    ConfigError,
    NotFound,
    RateLimited,
)
from hirebot.logging_setup import get_logger
from hirebot.ports.dto import TokenBundle
from hirebot.ports.infra import CachePort, ClockPort, HttpClientPort, HttpResponse, LockPort

__all__ = ["TalantixClient"]

logger = get_logger(__name__)

_REFRESH_MARGIN_S = 300.0
_TOKEN_CACHE_TTL_S = 60 * 60 * 24 * 120
_MANAGERS_PAGE_SIZE = 50
#: Страховка от бесконечного цикла пагинации при «залипшем» курсоре.
_MAX_MANAGER_PAGES = 40


@dataclass(slots=True)
class _TokenState:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None


class TalantixClient:
    """Транспорт ``POST /graphql`` + обновление токенов."""

    def __init__(
        self,
        settings: TalantixSettings,
        *,
        http: HttpClientPort,
        clock: ClockPort,
        cache: CachePort,
        locks: LockPort | None = None,
        cipher: object | None = None,
    ) -> None:
        self._settings = settings
        self._http = http
        self._clock = clock
        self._cache = cache
        self._locks: LockPort = locks if locks is not None else LocalLock()
        self._cipher = cipher
        self._base_url = settings.base_url.rstrip("/")
        self._graphql_url = f"{self._base_url}/graphql"
        self._token_url = f"{self._base_url}/oauth/token"
        self._auth_check_url = f"{self._base_url}/auth_check"
        self._cache_key = "talantix:tokens"
        self._lock_key = "talantix:token-refresh"
        self._local_lock = asyncio.Lock()
        self._state: _TokenState | None = None

    # ------------------------------------------------------------ транспорт

    async def graphql(
        self,
        query: str,
        variables: Mapping[str, Any] | None = None,
        *,
        mutation: bool = False,
        retry_on_auth: bool = True,
    ) -> dict[str, Any]:
        """Выполнить запрос и вернуть ``data``.

        Технические ошибки (``errors[]``, HTTP-коды) уже переведены в доменные.
        ``mutation=True`` отключает автоматические ретраи транспорта: повтор
        создания встречи вслепую недопустим — идемпотентность держится на
        маркере в ``comment`` и на таблице броней, а не на повторе запроса.
        """
        token = await self._access_token()
        body: dict[str, Any] = {"query": query}
        if variables:
            body["variables"] = dict(variables)
        response = await self._http.request(
            "POST",
            self._graphql_url,
            json_body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            idempotent=not mutation,
            rate_limit_key="talantix",
            rate_limit_rps=self._settings.rate_limit.requests_per_second,
            bucket="talantix",
        )
        if retry_on_auth and _is_token_expired(response):
            # Токен просрочен — обновляем под локом и повторяем один раз.
            await self._refresh(force=True)
            return await self.graphql(query, variables, mutation=mutation, retry_on_auth=False)
        self._raise_transport_error(response, context="POST /graphql")
        payload = json_object(response, context="Talantix GraphQL")
        self._raise_graphql_errors(payload)
        return as_dict(payload.get("data"))

    async def auth_check(self) -> bool:
        """``GET /auth_check`` — валиден ли access-токен (успех: ``204``)."""
        token = await self._access_token()
        response = await self._http.request(
            "GET",
            self._auth_check_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            idempotent=True,
            rate_limit_key="talantix",
            rate_limit_rps=self._settings.rate_limit.requests_per_second,
            bucket="talantix",
        )
        return int(response.status_code) == 204

    async def list_managers(self, *, first: int = _MANAGERS_PAGE_SIZE) -> list[TalantixManager]:
        """Все менеджеры компании (курсорная пагинация, ``first`` ≤ 50)."""
        page_size = max(1, min(_MANAGERS_PAGE_SIZE, first))
        managers: list[TalantixManager] = []
        cursor: str | None = None
        for _ in range(_MAX_MANAGER_PAGES):
            data = await self.graphql(MANAGERS_QUERY, {"first": page_size, "after": cursor})
            page, cursor, has_next = parse_managers_page(as_dict(data.get("managers")), context="managers")
            managers.extend(page)
            if not has_next or cursor is None:
                break
        return managers

    # ------------------------------------------------------------ ошибки

    def _raise_transport_error(self, response: HttpResponse, *, context: str) -> None:
        status = int(response.status_code)
        if status in {200, 210, 269}:
            return
        code = (error_code(response) or "").lower()
        detail = error_summary(response)
        message = f"{context}: {code or status} — {detail}"
        if status == 429:
            raise RateLimited(message, retry_after_s=retry_after_s(response, now=self._clock.now()))
        if status == 401:
            raise AuthExpired(message)
        if status == 403:
            if "authorisation" in detail.casefold():
                raise AuthExpired(message)
            msg = f"{message}. Проверьте тариф и права владельца токена Talantix"
            raise ConfigError(msg)
        if status == 404:
            raise NotFound(message)
        if status == 413:
            msg = f"{message}. Тело запроса превышает лимит Talantix"
            raise ConfigError(msg)
        if 400 <= status < 500:
            raise ConfigError(message)
        raise ChannelUnavailable(message, retry_after_s=retry_after_s(response, now=self._clock.now()))

    def _raise_graphql_errors(self, payload: Mapping[str, Any]) -> None:
        """``errors[]`` -> доменная ошибка.

        Ошибки валидации запроса и превышение лимитов сложности — это дефект
        нашего кода (``ConfigError``): повтор не поможет. ``INTERNAL`` и
        ``EXTERNAL_SYSTEM_ERROR`` — отказ на стороне Talantix/внешней системы
        (``ChannelUnavailable``): такой запрос имеет смысл повторить.
        """
        errors = as_list(payload.get("errors"))
        if not errors:
            return
        first = as_dict(errors[0])
        message = str_or_empty(first.get("message")) or "GraphQL error"
        error_type = str_or_empty(dig(first, "extensions", "errorType")).upper()
        lowered = message.casefold()
        if "complexity" in lowered or "root selection" in lowered:
            msg = (
                f"Talantix отклонил запрос по сложности/числу корневых полей: {message}. "
                "Нужно упростить запрос: одно корневое поле на вызов, сложность ≤ 2500"
            )
            raise ConfigError(msg)
        if (
            "validation" in lowered
            or "fieldundefined" in lowered
            or "unknown argument" in lowered
            or "cannot query field" in lowered
        ):
            msg = (
                f"Talantix отверг запрос как невалидный: {message}. "
                "Сверьте запрос со схемой: POST /graphql/schema"
            )
            raise ConfigError(msg)
        if error_type == "INTERNAL":
            raise ChannelUnavailable(f"Talantix: внутренняя ошибка сервера — {message}")
        if error_type == "EXTERNAL_SYSTEM_ERROR":
            raise ChannelUnavailable(f"Talantix: отказ внешней системы — {message}")
        raise ChannelUnavailable(f"Talantix вернул ошибку GraphQL: {message}")

    # ------------------------------------------------------------ токены

    async def current_bundle(self) -> TokenBundle | None:
        state = await self._reload()
        if state is None:
            return None
        return TokenBundle(
            access_token=state.access_token,
            refresh_token=state.refresh_token,
            expires_at=state.expires_at or self._clock.now(),
        )

    async def _access_token(self) -> str:
        state = await self._reload()
        if state is None:
            msg = (
                "Talantix: не задан HIREBOT_TALANTIX__ACCESS_TOKEN. Токены выдаёт только "
                "администратор Talantix в личном кабинете (тариф «Оптимальный»)"
            )
            raise ChannelNotConfigured(msg)
        if not self._needs_refresh(state):
            return state.access_token
        return await self._refresh(force=False)

    def _needs_refresh(self, state: _TokenState) -> bool:
        if state.expires_at is None:
            return False
        return self._clock.now() + timedelta(seconds=_REFRESH_MARGIN_S) >= state.expires_at

    async def _reload(self) -> _TokenState | None:
        cached = await self._read_cache()
        if cached is not None:
            self._state = cached
        elif self._state is None:
            self._state = self._from_settings()
        return self._state

    def _from_settings(self) -> _TokenState | None:
        access = self._settings.access_token
        if access is None:
            return None
        refresh = self._settings.refresh_token
        return _TokenState(
            access_token=access.get_secret_value(),
            refresh_token=refresh.get_secret_value() if refresh is not None else None,
            expires_at=None,
        )

    async def _read_cache(self) -> _TokenState | None:
        raw = as_dict(await self._cache.get_json(self._cache_key))
        raw_access = str_or_none(raw.get("access_token"))
        if raw_access is None:
            return None
        access = open_secret(self._cipher, raw_access)
        if not access:
            return None
        refresh_raw = str_or_none(raw.get("refresh_token"))
        refresh = open_secret(self._cipher, refresh_raw) if refresh_raw else None
        return _TokenState(
            access_token=access,
            refresh_token=refresh,
            expires_at=parse_datetime(raw.get("expires_at")),
        )

    async def _save(self, state: _TokenState) -> None:
        await self._cache.set_json(
            self._cache_key,
            {
                "access_token": seal_secret(self._cipher, state.access_token),
                "refresh_token": (
                    seal_secret(self._cipher, state.refresh_token)
                    if state.refresh_token is not None
                    else None
                ),
                "expires_at": state.expires_at.isoformat() if state.expires_at else None,
            },
            ttl_s=float(_TOKEN_CACHE_TTL_S),
        )

    async def _refresh(self, *, force: bool) -> str:
        """Обновить пару токенов (single-flight, ротация refresh-токена)."""
        async with self._local_lock:
            state = await self._reload()
            if state is None:
                msg = "Talantix: нет токенов для обновления (HIREBOT_TALANTIX__ACCESS_TOKEN)"
                raise ChannelNotConfigured(msg)
            if not force and not self._needs_refresh(state):
                return state.access_token

            async with self._locks.acquire(self._lock_key, ttl_s=30.0, wait_s=10.0) as acquired:
                if not acquired:
                    await self._clock.sleep(1.0)
                    fresh = await self._reload()
                    if fresh is not None and not self._needs_refresh(fresh):
                        return fresh.access_token
                    msg = "Обновление токена Talantix уже выполняется другим воркером"
                    raise ChannelUnavailable(msg, retry_after_s=5.0)

                state = await self._reload()
                if state is None:
                    msg = "Talantix: нет токенов для обновления (HIREBOT_TALANTIX__ACCESS_TOKEN)"
                    raise ChannelNotConfigured(msg)
                if not force and not self._needs_refresh(state):
                    return state.access_token
                if state.refresh_token is None:
                    msg = (
                        "Talantix: refresh_token отсутствует, автоматическое обновление "
                        "невозможно. Создайте новую пару токенов в личном кабинете"
                    )
                    raise AuthExpired(msg)

                response = await self._post_refresh(state.refresh_token)
                status = int(response.status_code)
                if status >= 400:
                    code = (error_code(response) or "").lower()
                    detail = error_summary(response)
                    message = f"Обновление токена Talantix: {code or status} — {detail}"
                    if status == 429:
                        raise RateLimited(
                            message, retry_after_s=retry_after_s(response, now=self._clock.now())
                        )
                    if code == "invalid_grant" or status in {400, 401, 403}:
                        raise AuthExpired(message)
                    raise ChannelUnavailable(message)
                updated = self._parse_token_response(response)
                if updated is None:
                    msg = "Talantix вернул ответ без access_token при обновлении токена"
                    raise ChannelUnavailable(msg)
                self._state = updated
                await self._save(updated)
                logger.info("talantix_token_refreshed", expires_at=_iso(updated.expires_at))
                return updated.access_token

    async def _post_refresh(self, refresh_token: str) -> HttpResponse:
        return await self._http.request(
            "POST",
            self._token_url,
            form={"grant_type": "refresh_token", "refresh_token": refresh_token},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            idempotent=False,
            rate_limit_key="talantix",
            rate_limit_rps=self._settings.rate_limit.requests_per_second,
            bucket="talantix",
        )

    def _parse_token_response(self, response: HttpResponse) -> _TokenState | None:
        body = as_dict(response.body)
        access = str_or_none(body.get("access_token"))
        if access is None:
            return None
        expires_in = body.get("expires_in")
        expires_at: datetime | None = None
        if isinstance(expires_in, int | float) and not isinstance(expires_in, bool):
            expires_at = self._clock.now() + timedelta(seconds=float(expires_in))
        return _TokenState(
            access_token=access,
            refresh_token=str_or_none(body.get("refresh_token")),
            expires_at=expires_at,
        )


def _is_token_expired(response: HttpResponse) -> bool:
    """401 ``invalid_token``/``token_expired`` — случай, когда нужен refresh."""
    if int(response.status_code) != 401:
        return False
    detail = error_summary(response).casefold()
    return "token_expired" in detail or "invalid_token" in detail


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
