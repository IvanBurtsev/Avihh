"""Доменные ошибки.

Правило: домен не знает про HTTP и БД. Адаптеры транслируют свои ошибки
в эти типы, сервисы принимают решения по типу ошибки, а не по коду ответа.
"""

from __future__ import annotations


class DomainError(Exception):
    """Базовая ошибка домена."""


class ConfigError(DomainError):
    """Не хватает или неверна конфигурация (нет токена, не включён канал)."""


class ChannelNotConfigured(ConfigError):
    """Площадка выключена или не имеет учётных данных."""


class ChannelUnavailable(DomainError):
    """Площадка недоступна: сеть, 5xx, таймаут. Ретраится."""

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class RateLimited(ChannelUnavailable):
    """429 или исчерпана квота. Обязательно учитывать ``retry_after_s``."""


class AuthExpired(DomainError):
    """Токен истёк/отозван. Требуется переавторизация, ретрай бессмысленен."""


class ResumeAccessDenied(DomainError):
    """Резюме недоступно: нет прав, скрыто кандидатом или лимит просмотров."""


class DuplicateAction(DomainError):
    """Действие уже выполнялось (сработал ключ идемпотентности).

    Не ошибка: означает, что повтор безопасен и его нужно просто пропустить.
    """


class PolicyViolation(DomainError):
    """Действие запрещено политикой (автоотказ, лимит сообщений, тихие часы)."""


class SlotConflict(DomainError):
    """Слот занят или пересекается с существующей встречей."""


class LlmError(DomainError):
    """Модель вернула ошибку или невалидный по схеме ответ."""


class GuardrailViolation(DomainError):
    """Текст не прошёл проверку безопасности (утечка ПДн, инъекция промпта)."""


class BudgetExceeded(DomainError):
    """Исчерпан лимит платных действий.

    Это не сбой, а защита: действие заблокировано до следующего периода или
    до повышения лимита. Сообщение должно объяснять, сколько и чего не хватает.
    """


class NotFound(DomainError):
    """Сущность не найдена."""
