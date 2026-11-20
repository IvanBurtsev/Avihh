"""Трансляция HTTP-ответов в доменные ошибки и разбор тел ошибок.

Правило слоя адаптеров: наружу (в сервисы) уходят только доменные ошибки
(``hirebot.domain.errors``). Сервис принимает решение по типу ошибки —
«ретраить», «спросить человека», «пометить резюме недоступным», — а не по
HTTP-коду. Поэтому весь разбор кодов и тел ошибок собран здесь, а не размазан
по адаптерам.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from hirebot.adapters.parsing import as_dict, as_list, str_or_none
from hirebot.domain.errors import ChannelUnavailable
from hirebot.ports.infra import HttpResponse

__all__ = [
    "error_code",
    "error_summary",
    "json_items",
    "json_object",
    "retry_after_s",
    "status_of",
]


def status_of(response: HttpResponse) -> int:
    return int(response.status_code)


def _payload(response: HttpResponse) -> dict[str, Any]:
    return as_dict(response.body)


def error_code(response: HttpResponse) -> str | None:
    """Код ошибки площадки: hh.ru кладёт его в ``type``/``value``, Talantix — в ``type``.

    Значение нужно для точных решений («no_invitation» — это бизнес-правило,
    а не сбой транспорта), поэтому код разбирается, а не только логируется.
    """
    body = _payload(response)
    for key in ("value", "type", "error", "errorType"):
        found = str_or_none(body.get(key))
        if found:
            return found
    return None


def error_summary(response: HttpResponse) -> str:
    """Человекочитаемое описание ошибки — для логов и текста доменного исключения.

    Секреты сюда не попадают: из тела берутся только поля-описания.
    """
    body = _payload(response)
    for key in ("error_description", "detail", "description", "message", "error"):
        found = str_or_none(body.get(key))
        if found:
            return found
    text = response.text.strip()
    if text:
        return text[:300]
    return f"HTTP {response.status_code}"


def retry_after_s(response: HttpResponse, *, now: datetime | None = None) -> float | None:
    """Разобрать заголовок ``Retry-After`` (число секунд или HTTP-дата).

    hh.ru официально не документирует заголовок, но отдаёт его при 429 —
    игнорировать нельзя: без паузы повторы продлевают блокировку.
    """
    raw: str | None = None
    for key, value in response.headers.items():
        if key.lower() == "retry-after":
            raw = value
            break
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    reference = now or datetime.now(tz=UTC)
    return max(0.0, (moment - reference).total_seconds())


def json_object(response: HttpResponse, *, context: str) -> dict[str, Any]:
    """Тело ответа как JSON-объект.

    Если площадка вернула не объект (или не JSON вовсе) — это не ошибка бизнес-логики,
    а неожиданный ответ: бросаем ``ChannelUnavailable`` на стороне адаптера.
    """
    body = response.body
    if isinstance(body, dict):
        return body
    msg = f"{context}: ожидался JSON-объект, получено {type(body).__name__} ({response.status_code})"
    raise ChannelUnavailable(msg)


def json_items(response: HttpResponse, *, context: str) -> list[Any]:
    """Список из поля ``items`` ответа (пустой, если поля нет)."""
    return as_list(json_object(response, context=context).get("items"))
