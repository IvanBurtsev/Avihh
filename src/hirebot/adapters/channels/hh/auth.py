"""OAuth-токены hh.ru: хранение, single-flight обновление, 3-legged вход.

Почему всё так сложно (и почему это нельзя упростить):

* refresh-токен hh.ru **одноразовый** и работает **только после истечения**
  access-токена. Повторный запрос даёт ``400 invalid_grant: token has already
  been refreshed`` и теряет сессию работодателя. Значит:
  - параллельные обновления обязаны быть сериализованы (single-flight);
  - «проактивное» обновление с запасом допустимо только как попытка, которая
    может быть отклонена ответом ``token not expired`` — и тогда нужно
    продолжить работу со старым токеном, а не падать;
* логины и пароли пользователей не хранятся никогда (п. 3.6 Условий API hh.ru),
  поэтому единственный источник доступа — эта пара токенов.

Состояние токенов сохраняется в кэш (в зашифрованном виде, если передан шифр):
без этого после рестарта процесса был бы потерян уже ротированный refresh-токен,
и пришлось бы требовать повторной авторизации менеджера.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlparse

from hirebot.adapters.http_utils import error_code, error_summary, retry_after_s
from hirebot.adapters.locks import LocalLock
from hirebot.adapters.parsing import as_dict, parse_datetime, str_or_none
from hirebot.adapters.secrets import open_secret, seal_secret
from hirebot.config import HHSettings
from hirebot.domain.errors import (
    AuthExpired,
    ChannelNotConfigured,
    ChannelUnavailable,
    ConfigError,
    RateLimited,
)
from hirebot.logging_setup import get_logger
from hirebot.ports.dto import TokenBundle
from hirebot.ports.infra import CachePort, ClockPort, HttpClientPort, HttpResponse, LockPort

__all__ = ["HhTokenManager", "oauth_base_url"]

logger = get_logger(__name__)

#: Запас до истечения токена, при котором начинаем «проактивную» попытку обновления.
#: hh.ru отклоняет обновление живого токена, поэтому попытка ровно одна:
#: после отказа сервера ждём фактического истечения (см. ``_early_block_until``).
DEFAULT_REFRESH_MARGIN_S = 300.0

#: TTL хранения состояния токенов в кэше (refresh-токен hh.ru живёт 14 суток).
_TOKEN_CACHE_TTL_S = 60 * 60 * 24 * 14


def oauth_base_url(base_url: str) -> str:
    """Получить базу OAuth из базы API.

    Токены hh.ru выдаёт ``hh.ru``, а API живёт на ``api.hh.ru``; при этом в
    тестах и на прокси ``base_url`` может быть произвольным — тогда используем
    его как есть.
    """
    parsed = urlparse(base_url)
    host = parsed.netloc
    if host.startswith("api."):
        host = host[4:]
    scheme = parsed.scheme or "https"
    if not host:
        return "https://hh.ru"
    return f"{scheme}://{host}"


@dataclass(slots=True)
class _TokenState:
    """Внутреннее состояние токенов.

    ``expires_at = None`` означает «срок неизвестен» (например, токен задан
    в конфиге без времени жизни): такой токен не обновляем проактивно.
    """

    access_token: str
    refresh_token: str | None
    expires_at: datetime | None


class HhTokenManager:
    """Хранилище и обновление токенов hh.ru.

    Использование: ``token = await manager.get_access_token()`` перед каждым
    запросом; при ``403 token_expired`` — ``await manager.force_refresh()``.
    """

    def __init__(
        self,
        settings: HHSettings,
        *,
        http: HttpClientPort,
        clock: ClockPort,
        cache: CachePort,
        locks: LockPort | None = None,
        cipher: object | None = None,
        oauth_url: str | None = None,
        refresh_margin_s: float = DEFAULT_REFRESH_MARGIN_S,
    ) -> None:
        self._settings = settings
        self._http = http
        self._clock = clock
        self._cache = cache
        self._locks: LockPort = locks if locks is not None else LocalLock()
        self._cipher = cipher
        self._oauth_url = oauth_url or f"{oauth_base_url(settings.base_url)}/oauth/token"
        self._refresh_margin_s = max(0.0, refresh_margin_s)
        self._local_lock = asyncio.Lock()
        self._state: _TokenState | None = None
        self._early_block_until: datetime | None = None
        self._cache_key = f"hh:tokens:{settings.client_id or 'default'}"
        self._lock_key = f"hh:token-refresh:{settings.client_id or 'default'}"

    # ---------------------------------------------------------------- публичное

    @property
    def oauth_token_url(self) -> str:
        return self._oauth_url

    def authorization_url(self, *, redirect_uri: str, state: str = "") -> str:
        """Шаг 1 трёхстороннего OAuth: ссылка, на которую идёт менеджер.

        ``client_credentials`` для методов работодателя бесполезен:
        он даёт ``403 oauth/user_auth_expected``. Нужен именно код авторизации,
        который менеджер подтверждает в браузере.
        """
        if not self._settings.client_id:
            msg = "HIREBOT_HH__CLIENT_ID не задан: нельзя построить ссылку авторизации hh.ru"
            raise ChannelNotConfigured(msg)
        query = {
            "response_type": "code",
            "client_id": self._settings.client_id,
            "redirect_uri": redirect_uri,
        }
        if state:
            query["state"] = state
        return f"{oauth_base_url(self._settings.base_url)}/oauth/authorize?{urlencode(query)}"

    async def exchange_code(self, code: str, *, redirect_uri: str) -> TokenBundle:
        """Шаг 3: обменять ``authorization_code`` на пару токенов."""
        secret = self._client_secret()
        if not self._settings.client_id or secret is None:
            msg = "Для обмена кода авторизации нужны HIREBOT_HH__CLIENT_ID и HIREBOT_HH__CLIENT_SECRET"
            raise ChannelNotConfigured(msg)
        form = {
            "grant_type": "authorization_code",
            "client_id": self._settings.client_id,
            "client_secret": secret,
            "redirect_uri": redirect_uri,
            "code": code,
        }
        response = await self._post_form(form)
        if response.status_code >= 400:
            self._raise_oauth_error(response, context="Обмен кода авторизации hh.ru")
        state = self._parse_token_response(response)
        if state is None:
            msg = "hh.ru вернул ответ без access_token при обмене кода авторизации"
            raise ChannelUnavailable(msg)
        self._state = state
        await self._save(state)
        return self._to_bundle(state)

    async def get_access_token(self) -> str:
        """Действующий access-токен (при необходимости обновляет его)."""
        state = await self._reload()
        if state is None:
            msg = (
                "Нет токенов hh.ru. Задайте HIREBOT_HH__ACCESS_TOKEN/REFRESH_TOKEN "
                "или пройдите авторизацию менеджера (authorization_code)"
            )
            raise ChannelNotConfigured(msg)
        if not self._needs_refresh(state):
            return state.access_token
        return await self._refresh(force=False)

    async def force_refresh(self) -> str:
        """Принудительно перевыпустить access-токен.

        Вызывается после ``403 oauth/token_expired``: время жизни мы считаем по
        ``expires_at``, но оно могло быть отозвано/сдвинуто на стороне площадки.
        """
        return await self._refresh(force=True)

    async def current_bundle(self) -> TokenBundle | None:
        state = await self._reload()
        return self._to_bundle(state) if state is not None else None

    async def invalidate(self) -> None:
        """Забыть состояние в памяти (следующий вызов перечитает кэш и конфиг)."""
        self._state = None
        self._early_block_until = None

    # ---------------------------------------------------------------- внутреннее

    def _client_secret(self) -> str | None:
        secret = self._settings.client_secret
        return secret.get_secret_value() if secret is not None else None

    def _needs_refresh(self, state: _TokenState) -> bool:
        if state.expires_at is None:
            return False
        now = self._clock.now()
        if now >= state.expires_at:
            return True
        if self._early_block_until is not None and now < self._early_block_until:
            return False
        return now + timedelta(seconds=self._refresh_margin_s) >= state.expires_at

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

    async def _reload(self) -> _TokenState | None:
        """Перечитать состояние: кэш важнее памяти, память важнее конфига.

        Кэш важнее, потому что после ротации refresh-токена в другом процессе
        (или после рестарта) актуальная пара лежит именно там.
        """
        cached = await self._read_cache()
        if cached is not None:
            self._state = cached
        elif self._state is None:
            self._state = self._from_settings()
        return self._state

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

    @staticmethod
    def _to_bundle(state: _TokenState) -> TokenBundle:
        """Состояние -> публичный DTO (``expires_at`` в DTO обязателен)."""
        expires_at = state.expires_at if state.expires_at is not None else datetime.now(tz=UTC)
        return TokenBundle(
            access_token=state.access_token,
            refresh_token=state.refresh_token,
            expires_at=expires_at,
        )

    async def _refresh(self, *, force: bool) -> str:
        """Single-flight обновление токена.

        Двойная защита: ``asyncio.Lock`` (внутри процесса) + ``LockPort``
        (между воркерами). Локальный лок снимает гонку даже там, где
        распределённый лок не передан, — а именно этот случай ломает сессию
        hh.ru чаще всего.
        """
        async with self._local_lock:
            state = await self._reload()
            if state is None:
                msg = "Нет токенов hh.ru для обновления: задайте HIREBOT_HH__ACCESS_TOKEN"
                raise ChannelNotConfigured(msg)
            if not force and not self._needs_refresh(state):
                return state.access_token

            async with self._locks.acquire(self._lock_key, ttl_s=30.0, wait_s=10.0) as acquired:
                if not acquired:
                    # Другой воркер обновляет прямо сейчас: подхватываем его результат.
                    await self._clock.sleep(1.0)
                    fresh = await self._reload()
                    if fresh is not None and not self._needs_refresh(fresh):
                        return fresh.access_token
                    msg = "Обновление токена hh.ru уже выполняется другим воркером"
                    raise ChannelUnavailable(msg, retry_after_s=5.0)

                state = await self._reload()
                if state is None:
                    msg = "Нет токенов hh.ru для обновления: задайте HIREBOT_HH__ACCESS_TOKEN"
                    raise ChannelNotConfigured(msg)
                if not force and not self._needs_refresh(state):
                    return state.access_token
                if state.refresh_token is None:
                    msg = (
                        "refresh_token hh.ru отсутствует: автоматическое обновление невозможно, "
                        "нужна повторная авторизация менеджера (authorization_code)"
                    )
                    raise AuthExpired(msg)

                form = {
                    "grant_type": "refresh_token",
                    "refresh_token": state.refresh_token,
                }
                response = await self._post_form(form)
                if response.status_code == 400:
                    code = (error_code(response) or "").lower()
                    description = error_summary(response).lower()
                    if code == "invalid_grant" and "not expired" in description:
                        # Сервер считает текущий токен живым: продолжаем с ним.
                        # Повторную попытку отложим до фактического истечения.
                        self._early_block_until = state.expires_at
                        logger.info("hh_refresh_too_early", expires_at=_iso(state.expires_at))
                        return state.access_token
                    self._raise_oauth_error(response, context="Обновление токена hh.ru")
                if response.status_code >= 400:
                    self._raise_oauth_error(response, context="Обновление токена hh.ru")

                updated = self._parse_token_response(response)
                if updated is None:
                    msg = "hh.ru вернул ответ без access_token при обновлении токена"
                    raise ChannelUnavailable(msg)
                self._state = updated
                self._early_block_until = None
                await self._save(updated)
                logger.info("hh_token_refreshed", expires_at=_iso(updated.expires_at))
                return updated.access_token

    async def _post_form(self, form: dict[str, str]) -> HttpResponse:
        """Запрос к ``/oauth/token``.

        ``idempotent=False`` — принципиально: повтор refresh-запроса ломает сессию,
        поэтому автоматические ретраи транспорта здесь запрещены.
        """
        headers = {"HH-User-Agent": self._settings.user_agent, "Accept": "application/json"}
        return await self._http.request(
            "POST",
            self._oauth_url,
            form=form,
            headers=headers,
            idempotent=False,
            rate_limit_key="hh",
            rate_limit_rps=self._settings.rate_limit.requests_per_second,
            bucket="hh",
        )

    def _raise_oauth_error(self, response: HttpResponse, *, context: str) -> None:
        detail = error_summary(response)
        code = (error_code(response) or "").lower()
        status = int(response.status_code)
        message = f"{context}: {code or 'error'} — {detail}"
        if status == 429:
            raise RateLimited(message, retry_after_s=retry_after_s(response, now=self._clock.now()))
        if status in {401, 403} or code in {
            "invalid_grant",
            "invalid_client",
            "token_revoked",
            "token_deactivated",
        }:
            raise AuthExpired(message)
        if status == 400 and code in {"invalid_request"}:
            raise ConfigError(message)
        raise ChannelUnavailable(message)

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


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
