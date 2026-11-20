"""Единственная точка исходящего HTTP-трафика.

Все адаптеры площадок (hh.ru, Avito, Talantix, LLM) ходят в сеть **только**
через ``HttpClientPort``. Иначе ретраи, таймауты, лимиты, кэш и редакция
секретов пришлось бы дублировать в каждом адаптере — и они неизбежно разъехались
бы. Цена ошибки здесь высокая: у hh.ru и Avito превышение лимита приводит к
блокировке аккаунта, а утёкший в лог токен — к угону сессии рекрутера.

Правила, которые реализованы ниже:

* ретраятся **только** идемпотентные запросы (``idempotent=True``); повтор
  «создать встречу» или «отправить сообщение» вслепую недопустим;
* 5xx и таймауты → ``ChannelUnavailable``; 401 → ``AuthExpired`` (ретрай
  бессмысленен, нужна переавторизация); 429 → ``RateLimited`` с ``retry_after_s``;
* ``Retry-After`` уважается: короткую паузу выдерживаем и повторяем, длинную —
  возвращаем вызывающей стороне, чтобы она решала сама (воркер отложит задачу);
* заголовки ``Authorization``/``X-Api-Key``/``Cookie`` и параметры
  ``access_token``/``client_secret``/``password`` в логах и в текстах ошибок
  заменяются на ``***``;
* тела запросов и ответов **не логируются никогда** — в них резюме и ПДн.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import orjson
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
)

from hirebot.config import Settings
from hirebot.domain.errors import AuthExpired, ChannelUnavailable, RateLimited
from hirebot.infra.clock import SystemClock
from hirebot.logging_setup import get_logger
from hirebot.ports.infra import CachePort, ClockPort, HttpClientPort, HttpResponse, LockPort, RateLimiterPort

logger = get_logger(__name__)

#: Заголовки, значение которых нельзя показывать даже в DEBUG.
SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-hh-apikey",
        "x-avito-token",
    }
)

#: Query-параметры и поля форм, которые являются секретами.
SENSITIVE_PARAMS: frozenset[str] = frozenset(
    {
        "access_token",
        "refresh_token",
        "client_secret",
        "password",
        "api_key",
        "apikey",
        "token",
        "secret",
        "code",
        "signature",
    }
)

#: Маска, которая подставляется вместо секрета.
REDACTED = "***"

#: Базовый таймаут запроса (секунды). Отдельной настройки нет: 30 с — разумный
#: компромисс между «площадка тормозит» и «воркер не висит вечно».
DEFAULT_TIMEOUT_S = 30.0
#: Сколько всего попыток делаем для идемпотентного запроса (включая первую).
DEFAULT_MAX_ATTEMPTS = 4
#: Ограничение на верхнюю границу backoff (секунды).
MAX_BACKOFF_S = 8.0
#: ``Retry-After`` больше этого значения не пережидаем внутри запроса: отдаём
#: наверх как ``RateLimited``, чтобы задачу отложил воркер, а не залипал воркер.
MAX_HONORED_RETRY_AFTER_S = 2.0


def redact_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Заменить значения секретных заголовков на ``***`` (имена сохраняем)."""
    if not headers:
        return {}
    return {
        name: (REDACTED if name.lower() in SENSITIVE_HEADERS else value) for name, value in headers.items()
    }


def redact_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Заменить значения секретных query-параметров на ``***``."""
    if not params:
        return {}
    return {
        str(name): (REDACTED if str(name).lower() in SENSITIVE_PARAMS else value)
        for name, value in params.items()
    }


def redact_url(url: str) -> str:
    """Скрыть секреты в query-строке URL.

    Нужно не только для логов: URL попадает в текст исключений, а исключения
    уходят в Sentry/логи — то есть утекают тем же путём.
    """
    parts = urlsplit(url)
    if not parts.query:
        return url
    safe_pairs = [
        (name, REDACTED if name.lower() in SENSITIVE_PARAMS else value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(safe_pairs)))


def build_cache_key(
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    json_body: Any | None = None,
    form: Mapping[str, Any] | None = None,
) -> str:
    """Детерминированный ключ кэша: метод + URL + параметры + тело.

    Хэш, а не «сырая» строка: URL бывает длиной в килобайт (GraphQL-запросы), и
    такие ключи дорого хранить в Redis.
    """
    material: dict[str, Any] = {
        "method": method.upper(),
        "url": url,
        "params": dict(sorted(params.items())) if params else None,
        "json": json_body,
        "form": dict(sorted(form.items())) if form else None,
    }
    digest = hashlib.sha256(orjson.dumps(material, default=str)).hexdigest()
    return f"http:{method.upper()}:{digest}"


class _RetryAfterSignal(ChannelUnavailable):
    """Внутренний сигнал: «пауза выдержана, можно повторить».

    Наследуется от ``ChannelUnavailable``, поэтому tenacity ретраит его как
    обычную временную ошибку; наружу он не выходит — превращается в
    ``RateLimited`` с исходным ``retry_after_s``.
    """


@dataclass(slots=True, frozen=True)
class _Request:
    """Параметры одного запроса (передаются между попытками без изменений)."""

    method: str
    url: str
    params: Mapping[str, Any] | None = None
    json_body: Any | None = None
    form: Mapping[str, Any] | None = None
    headers: Mapping[str, str] | None = None


def _is_retryable(exc: BaseException) -> bool:
    """Ретраить ли исключение.

    ``RateLimited`` (длинный ``Retry-After``) намеренно не ретраится: повтор
    «в лоб» только усиливает блокировку на стороне площадки. ``AuthExpired``
    тоже бессмысленно повторять — нужен новый токен.
    """
    return isinstance(exc, ChannelUnavailable) and not isinstance(exc, RateLimited)


def _outcome_exception(state: RetryCallState) -> BaseException | None:
    """Исключение последней попытки (``None``, если попытка ещё не завершилась)."""
    outcome = state.outcome
    if outcome is None:
        return None
    try:
        return outcome.exception()
    except BaseException:  # pragma: no cover - защита от отменённого future
        return None


def _wait_strategy(state: RetryCallState) -> float:
    """Пауза перед повтором.

    Два режима:
    * ``Retry-After`` от площадки — выдерживаем **ровно** указанное время
      (площадка знает лучше нас, когда снимется лимит);
    * остальные временные ошибки — экспоненциальная пауза с джиттером, чтобы
      несколько воркеров не били по упавшему API синхронно.
    """
    exception = _outcome_exception(state)
    if isinstance(exception, _RetryAfterSignal):
        return max(0.0, exception.retry_after_s or 0.0)
    attempt: int = int(state.attempt_number)
    base = min(MAX_BACKOFF_S, 0.4 * (2 ** (attempt - 1)))
    return float(base * random.uniform(0.5, 1.5))


def _parse_retry_after(value: str | None) -> float | None:
    """Разобрать ``Retry-After`` (секунды). HTTP-дата не поддерживается осознанно."""
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _serialize_response(response: HttpResponse) -> dict[str, Any]:
    """Представление ответа для кэша (без ``httpx.Response``)."""
    return {
        "status_code": response.status_code,
        "body": response.body,
        "text": response.text,
        "headers": dict(response.headers),
        "url": response.url,
    }


def _deserialize_response(payload: Mapping[str, Any]) -> HttpResponse:
    """Восстановить ответ из кэша, не доверяя форме данных слепо."""
    return HttpResponse(
        status_code=int(payload.get("status_code", 200)),
        body=_payload_body(payload.get("body")),
        text=str(payload.get("text") or ""),
        headers=_payload_headers(payload.get("headers")),
        url=str(payload.get("url") or ""),
        from_cache=True,
    )


def _payload_body(raw: Any) -> dict[str, Any] | list[Any] | None:
    if isinstance(raw, dict):
        return cast("dict[str, Any]", raw)
    if isinstance(raw, list):
        return raw
    return None


def _payload_headers(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    return {}


class HttpxClient:
    """HTTP-клиент на одном общем ``httpx.AsyncClient``.

    Один клиент на процесс — это один пул соединений (keep-alive, TLS-сессии) и
    одни таймауты. Создавать клиент на запрос — типовая ошибка: соединения не
    переиспользуются, а при пиковой нагрузке кончаются эфемерные порты.
    """

    __slots__ = (
        "_cache",
        "_client",
        "_clock",
        "_default_burst",
        "_default_rps",
        "_locks",
        "_max_attempts",
        "_max_retry_after_s",
        "_rate_limiter",
        "_timeout_s",
    )

    def __init__(
        self,
        *,
        cache: CachePort,
        rate_limiter: RateLimiterPort,
        locks: LockPort,
        clock: ClockPort,
        default_headers: Mapping[str, str] | None = None,
        default_rate_per_second: float = 1.0,
        default_burst: int = 1,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_retry_after_s: float = MAX_HONORED_RETRY_AFTER_S,
        max_connections: int = 20,
        max_keepalive_connections: int = 10,
    ) -> None:
        self._cache = cache
        self._rate_limiter = rate_limiter
        self._locks = locks
        self._clock: ClockPort = clock if clock is not None else SystemClock()
        self._default_rps = default_rate_per_second
        self._default_burst = max(1, default_burst)
        self._timeout_s = timeout_s
        self._max_attempts = max(1, max_attempts)
        self._max_retry_after_s = max(0.0, max_retry_after_s)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_keepalive_connections,
            ),
            headers=dict(default_headers) if default_headers else None,
            follow_redirects=False,
        )

    @property
    def client(self) -> httpx.AsyncClient:
        """Доступ к транспорту (нужен тестам и ``aclose``)."""
        return self._client

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        form: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool = True,
        cache_ttl_s: float = 0.0,
        rate_limit_key: str | None = None,
        rate_limit_rps: float | None = None,
        bucket: str = "default",
    ) -> HttpResponse:
        request = _Request(
            method=method.upper(),
            url=url,
            params=params,
            json_body=json_body,
            form=form,
            headers=headers,
        )
        await self._respect_rate_limit(request, rate_limit_key, rate_limit_rps, bucket)

        if cache_ttl_s <= 0:
            return await self._execute(request, idempotent=idempotent)

        cache_key = build_cache_key(request.method, url, params=params, json_body=json_body, form=form)
        return await self._execute_cached(request, cache_key, cache_ttl_s, idempotent=idempotent)

    async def _respect_rate_limit(
        self,
        request: _Request,
        rate_limit_key: str | None,
        rate_limit_rps: float | None,
        bucket: str,
    ) -> None:
        """Выдержать per-bucket лимит перед запросом."""
        host = urlsplit(request.url).netloc or "unknown"
        key = rate_limit_key or f"{bucket}:{host}"
        rps = rate_limit_rps if rate_limit_rps is not None else self._default_rps
        burst = self._default_burst if rate_limit_rps is None else max(self._default_burst, 1)
        waited = await self._rate_limiter.acquire(key, rate_per_second=rps, burst=burst)
        if waited > 0:
            logger.debug("http_rate_limit_waited", bucket=key, waited_s=round(waited, 3))

    async def _execute_cached(
        self, request: _Request, cache_key: str, cache_ttl_s: float, *, idempotent: bool
    ) -> HttpResponse:
        """GET-подобный запрос с кэшем и защитой от одновременной «промашки».

        Два воркера, одновременно промахнувшихся по одному ключу, сходят в API
        дважды. Лок превращает это в single-flight; если лок занят — не ждём,
        а просто идём в сеть (лучше лишний запрос, чем задержка ответа).
        """
        cached = await self._cache.get_json(cache_key)
        if isinstance(cached, dict):
            logger.debug("http_cache_hit", method=request.method, url=redact_url(request.url))
            return _deserialize_response(cached)

        async with self._locks.acquire(f"http-cache:{cache_key}", ttl_s=30.0, wait_s=2.0) as owned:
            if owned:
                # Повторная проверка: пока ждали лок, сосед мог уже сходить в API.
                cached = await self._cache.get_json(cache_key)
                if isinstance(cached, dict):
                    logger.debug(
                        "http_cache_hit_after_lock", method=request.method, url=redact_url(request.url)
                    )
                    return _deserialize_response(cached)
            response = await self._execute(request, idempotent=idempotent)
            if owned and response.ok:
                await self._cache.set_json(cache_key, _serialize_response(response), ttl_s=cache_ttl_s)
            return response

    async def _execute(self, request: _Request, *, idempotent: bool) -> HttpResponse:
        """Выполнить запрос, при необходимости — с ретраями."""
        if not idempotent:
            # Ровно одна попытка: повтор неидемпотентного запроса создаёт дубль
            # (второе приглашение, вторая встреча в календаре). Сигнал о
            # выдержанной паузе здесь тоже обязан стать ``RateLimited``: иначе
            # наружу утёк бы приватный ``_RetryAfterSignal``, а вызывающая
            # сторона не узнала бы про 429 (см. докстринг класса).
            try:
                return await self._send_once(request)
            except _RetryAfterSignal as exc:
                raise self._rate_limited(request, exc) from exc

        result: HttpResponse | None = None
        retrying: AsyncRetrying = AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=_wait_strategy,
            retry=retry_if_exception(_is_retryable),
            # Пауза между попытками идёт через ClockPort: в тестах это виртуальное
            # время, в продакшене — настоящий asyncio.sleep. Иначе тесты ретраев
            # реально спали бы секундами.
            sleep=self._clock.sleep,
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    result = await self._send_once(request)
        except _RetryAfterSignal as exc:
            raise self._rate_limited(request, exc) from exc
        if result is None:  # pragma: no cover - защита от изменения поведения tenacity
            msg = f"{request.method} {redact_url(request.url)}: ретраи исчерпаны без ответа"
            raise ChannelUnavailable(msg)
        return result

    @staticmethod
    def _rate_limited(request: _Request, signal: _RetryAfterSignal) -> RateLimited:
        """Превратить внутренний сигнал в доменную ошибку ``RateLimited``.

        Один и тот же 429 обязан выглядеть одинаково для чтения и для отправки:
        приватный ``_RetryAfterSignal`` наружу не выходит никогда.
        """
        return RateLimited(
            f"{request.method} {redact_url(request.url)}: 429, пауза исчерпана",
            retry_after_s=signal.retry_after_s,
        )

    async def _send_once(self, request: _Request) -> HttpResponse:
        """Одна сетевая попытка + трансляция ответа в доменные ошибки."""
        log_url = redact_url(request.url)
        logger.debug(
            "http_request",
            method=request.method,
            url=log_url,
            params=redact_params(request.params),
            headers=redact_headers(request.headers),
            json_bytes=_payload_size(request.json_body),
            form_bytes=_payload_size(request.form),
        )
        started = self._clock.now()
        try:
            raw = await self._client.request(
                request.method,
                request.url,
                params=dict(request.params) if request.params else None,
                json=request.json_body,
                data=dict(request.form) if request.form else None,
                headers=dict(request.headers) if request.headers else None,
            )
        except httpx.TimeoutException as exc:
            msg = f"{request.method} {log_url}: таймаут {self._timeout_s:g} c"
            raise ChannelUnavailable(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"{request.method} {log_url}: сетевая ошибка {type(exc).__name__}"
            raise ChannelUnavailable(msg) from exc

        elapsed = (self._clock.now() - started).total_seconds()
        logger.debug(
            "http_response",
            method=request.method,
            url=log_url,
            status=raw.status_code,
            duration_s=round(elapsed, 4),
            body_bytes=len(raw.content),
        )
        return self._translate(request, raw, log_url)

    def _translate(self, request: _Request, raw: httpx.Response, log_url: str) -> HttpResponse:
        """Превратить статус в доменную ошибку или в нормализованный ответ.

        Решение по 429: короткий ``Retry-After`` выдерживаем и повторяем (это
        дешевле, чем ждать внешнего ретрая воркера), длинный — возвращаем наверх
        как ``RateLimited(retry_after_s=...)``, чтобы задачу отложил воркер.
        Слепо ретраить 429 нельзя: площадка читает это как нарушение лимита.
        """
        status = raw.status_code
        if status == 401:
            msg = f"{request.method} {log_url}: 401 Unauthorized — требуется переавторизация"
            raise AuthExpired(msg)
        if status == 429:
            retry_after = _parse_retry_after(raw.headers.get("retry-after"))
            msg = f"{request.method} {log_url}: 429 Too Many Requests (retry_after={retry_after})"
            if retry_after is not None and retry_after <= self._max_retry_after_s:
                logger.warning("http_rate_limited_retry", url=log_url, retry_after_s=retry_after)
                raise _RetryAfterSignal(msg, retry_after_s=retry_after)
            raise RateLimited(msg, retry_after_s=retry_after)
        if status >= 500 or status == 408:
            msg = f"{request.method} {log_url}: {status} (площадка недоступна)"
            raise ChannelUnavailable(msg)
        return HttpResponse(
            status_code=status,
            body=_parse_body(raw),
            text=raw.text,
            headers={str(k): str(v) for k, v in raw.headers.items()},
            url=str(raw.url),
        )

    async def aclose(self) -> None:
        """Закрыть общий клиент (вызывается при остановке приложения)."""
        await self._client.aclose()


def _payload_size(payload: Any) -> int | None:
    """Размер тела в байтах — вместо самого тела (в нём ПДн и резюме)."""
    if payload is None:
        return None
    try:
        return len(orjson.dumps(payload, default=str))
    except TypeError:
        return None


def _parse_body(raw: httpx.Response) -> dict[str, Any] | list[Any] | None:
    """Разобрать JSON-тело, если оно есть. Невалидный JSON — не ошибка транспорта."""
    if not raw.content:
        return None
    try:
        parsed: Any = raw.json()
    except ValueError:
        return None
    if isinstance(parsed, dict):
        return cast("dict[str, Any]", parsed)
    if isinstance(parsed, list):
        return parsed
    return None


def create_http_client(
    settings: Settings,
    *,
    cache: CachePort,
    rate_limiter: RateLimiterPort,
    locks: LockPort,
    clock: ClockPort,
) -> HttpClientPort:
    """Собрать HTTP-клиент по настройкам (обычная функция, не корутина).

    ``httpx.AsyncClient`` создаётся без сети, поэтому ``async`` здесь не нужен:
    контейнер собирает инфраструктуру в одном месте.
    """
    rate_configs = (
        settings.hh.rate_limit,
        settings.avito.rate_limit,
        settings.talantix.rate_limit,
    )
    # По умолчанию берём самый строгий лимит из настроенных каналов: при ошибке
    # в коде адаптера (не передал rate_limit_rps) безопаснее быть медленнее,
    # чем получить 429 или блокировку аккаунта.
    default_rps = min(config.requests_per_second for config in rate_configs)
    default_burst = min(config.burst for config in rate_configs)
    return HttpxClient(
        cache=cache,
        rate_limiter=rate_limiter,
        locks=locks,
        clock=clock,
        default_headers={"User-Agent": settings.hh.user_agent},
        default_rate_per_second=default_rps,
        default_burst=default_burst,
    )


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_TIMEOUT_S",
    "MAX_HONORED_RETRY_AFTER_S",
    "REDACTED",
    "SENSITIVE_HEADERS",
    "SENSITIVE_PARAMS",
    "HttpxClient",
    "build_cache_key",
    "create_http_client",
    "redact_headers",
    "redact_params",
    "redact_url",
]
