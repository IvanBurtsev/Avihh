"""Конфигурация приложения.

Все настройки читаются из переменных окружения с префиксом ``HIREBOT_``,
вложенность задаётся двойным подчёркиванием::

    HIREBOT_HH__CLIENT_ID=abc   ->  settings.hh.client_id

Секреты объявлены как ``SecretStr``: они не попадают в логи и в ``repr``.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Env = Literal["local", "test", "staging", "production"]


class DatabaseSettings(BaseModel):
    """Подключение к БД.

    По умолчанию — SQLite, чтобы проект запускался одной командой без Docker.
    Для продакшена обязателен PostgreSQL: см. ``dsn`` в .env.
    """

    dsn: str = "sqlite+aiosqlite:///./hirebot.db"
    echo_sql: bool = False
    pool_size: int = Field(default=10, ge=1, le=100)
    max_overflow: int = Field(default=20, ge=0, le=200)
    statement_timeout_ms: int = Field(default=15_000, ge=100)

    @property
    def is_postgres(self) -> bool:
        return self.dsn.startswith("postgresql")


class RedisSettings(BaseModel):
    """Redis используется для кэша, распределённых локов и rate limit."""

    dsn: str = "redis://localhost:6380/0"
    # Если Redis недоступен, приложение продолжает работать:
    # кэш и лимиты деградируют до in-process реализации.
    required: bool = False


class QueueSettings(BaseModel):
    """Очередь задач.

    ``arq``    — брокер Redis (продакшен, несколько воркеров);
    ``inline`` — задачи выполняются в текущем процессе (демо, тесты, отладка).

    Семантика одна и та же, меняется только реализация порта ``JobQueue``.
    """

    backend: Literal["arq", "inline"] = "inline"
    queue_name: str = "hirebot"
    max_attempts: int = Field(default=5, ge=1, le=20)
    job_timeout_s: int = Field(default=300, ge=10)


class SecuritySettings(BaseModel):
    """Ключ шифрования персональных данных (152-ФЗ)."""

    pii_key: SecretStr | None = None
    # Ключ подписи вебхуков; если не задан — подпись не проверяется (только для local).
    webhook_secret: SecretStr | None = None

    @field_validator("pii_key")
    @classmethod
    def _validate_fernet_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value().strip()
        if not raw:
            return None
        try:
            decoded = base64.urlsafe_b64decode(raw.encode())
        except (ValueError, TypeError) as exc:  # pragma: no cover - defensive
            msg = "HIREBOT_SECURITY__PII_KEY не является корректным base64"
            raise ValueError(msg) from exc
        if len(decoded) != 32:
            msg = "HIREBOT_SECURITY__PII_KEY должен содержать 32 байта (Fernet.generate_key())"
            raise ValueError(msg)
        return SecretStr(raw)


class RateLimitSettings(BaseModel):
    """Ограничения исходящих запросов к внешним API."""

    requests_per_second: float = Field(default=3.0, gt=0)
    burst: int = Field(default=5, ge=1)
    # Джойтер для человекоподобной паузы между сообщениями в чате.
    min_send_delay_s: float = Field(default=2.0, ge=0)
    max_send_delay_s: float = Field(default=6.0, ge=0)

    @model_validator(mode="after")
    def _check_delay_order(self) -> RateLimitSettings:
        if self.max_send_delay_s < self.min_send_delay_s:
            msg = "max_send_delay_s не может быть меньше min_send_delay_s"
            raise ValueError(msg)
        return self


class HHSettings(BaseModel):
    """Официальный Employer API hh.ru.

    Логин и пароль пользователя НЕ используются и не хранятся: условия API
    hh.ru это прямо запрещают. Только OAuth-токен приложения.
    """

    enabled: bool = False
    base_url: str = "https://api.hh.ru"
    client_id: str = ""
    client_secret: SecretStr | None = None
    access_token: SecretStr | None = None
    refresh_token: SecretStr | None = None
    # Публичный идентификатор приложения для заголовка User-Agent (требование hh.ru).
    user_agent: str = "hirebot/0.1 (recruiting-automation)"
    manager_id: str | None = None
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)


class AvitoSettings(BaseModel):
    """Avito API.

    Доступ к Avito API выдаётся по заявке и подтверждается модерацией,
    поэтому адаптер умеет работать в режиме ``enabled=False`` без падений.
    """

    enabled: bool = False
    base_url: str = "https://api.avito.ru"
    client_id: str = ""
    client_secret: SecretStr | None = None
    access_token: SecretStr | None = None
    user_id: int | None = None
    rate_limit: RateLimitSettings = Field(
        default_factory=lambda: RateLimitSettings(requests_per_second=1.0, min_send_delay_s=3.0)
    )


class TalantixSettings(BaseModel):
    """Talantix — GraphQL API (``POST /graphql``)."""

    enabled: bool = False
    base_url: str = "https://api.talantix.ru"
    access_token: SecretStr | None = None
    refresh_token: SecretStr | None = None
    # Окно выборки встреч ограничено 93 днями на стороне API.
    max_window_days: int = Field(default=90, ge=1, le=93)
    default_timezone: str = "Europe/Moscow"
    rate_limit: RateLimitSettings = Field(
        default_factory=lambda: RateLimitSettings(requests_per_second=2.0, burst=4)
    )


class LlmSettings(BaseModel):
    """LLM-провайдер.

    ``fake``          — детерминированная заглушка: пайплайн работает без ключей и сети;
    ``openai_compat`` — любой OpenAI-совместимый эндпоинт (OpenAI, vLLM, OpenRouter, GigaChat-прокси).
    """

    provider: Literal["fake", "openai_compat"] = "fake"
    base_url: str = "https://api.openai.com/v1"
    api_key: SecretStr | None = None
    model: str = "gpt-4o-mini"
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1200, ge=64, le=16_384)
    timeout_s: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    # Кэш ответов модели по хэшу нормализованного промпта.
    cache_ttl_s: int = Field(default=86_400, ge=0)
    # Отправлять ли персональные данные кандидата в модель.
    send_pii: bool = False


class PolicySettings(BaseModel):
    """Правила, ограничивающие автономность бота.

    Это не «настройки удобства», а защита от юридических и репутационных
    последствий: автоотказ по защищённым признакам запрещён на уровне кода.
    """

    require_human_approval: bool = False
    max_auto_messages_per_candidate: int = Field(default=4, ge=1, le=20)
    min_score_to_invite: float = Field(default=70.0, ge=0, le=100)
    auto_reject_enabled: bool = False
    # Часы, в которые разрешено писать кандидатам (локальное время кандидата).
    quiet_hours_start: int = Field(default=21, ge=0, le=23)
    quiet_hours_end: int = Field(default=9, ge=0, le=23)


class PrivacySettings(BaseModel):
    """Сроки хранения персональных данных (152-ФЗ).

    Принцип: ПДн нельзя хранить «на всякий случай». Для каждой категории задан
    срок, после которого данные удаляются автоматической задачей. Значения по
    умолчанию соответствуют обычной практике рекрутинга; юрист заказчика должен
    их подтвердить.
    """

    #: Тексты резюме удаляются раньше остального: это самый чувствительный объём.
    resume_retention_days: int = Field(default=180, ge=1, le=3650)
    #: История переписки.
    message_retention_days: int = Field(default=365, ge=1, le=3650)
    #: Результаты скрининга (обезличенная аналитика качества).
    screening_retention_days: int = Field(default=365, ge=1, le=3650)
    #: Журнал действий: он нужен для разбора инцидентов, поэтому живёт дольше.
    audit_retention_days: int = Field(default=730, ge=30, le=3650)
    #: Черновики, которые никто не одобрил, теряют смысл быстро.
    draft_retention_days: int = Field(default=30, ge=1, le=365)
    #: Сколько записей удалять за один проход (чтобы не держать долгую транзакцию).
    purge_batch_size: int = Field(default=1000, ge=1, le=50_000)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HIREBOT_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Env = "local"
    log_level: str = "INFO"
    log_json: bool = True
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    hh: HHSettings = Field(default_factory=HHSettings)
    avito: AvitoSettings = Field(default_factory=AvitoSettings)
    talantix: TalantixSettings = Field(default_factory=TalantixSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    privacy: PrivacySettings = Field(default_factory=PrivacySettings)

    @model_validator(mode="after")
    def _fail_fast_in_production(self) -> Settings:
        """В продакшене небезопасные значения по умолчанию запрещены."""
        if self.env != "production":
            return self

        problems: list[str] = []
        if not self.database.is_postgres:
            problems.append("HIREBOT_DATABASE__DSN должен указывать на PostgreSQL")
        if self.security.pii_key is None:
            problems.append("HIREBOT_SECURITY__PII_KEY обязателен (шифрование ПДн)")
        if self.security.webhook_secret is None:
            # Без секрета защищённые эндпоинты (API, метрики, UI) отвечают 503,
            # то есть сервис поднимается неработоспособным. Лучше упасть на старте.
            problems.append(
                "HIREBOT_SECURITY__WEBHOOK_SECRET обязателен (ключ доступа к API, метрикам и интерфейсу)"
            )
        if self.queue.backend != "arq":
            problems.append("HIREBOT_QUEUE__BACKEND=arq обязателен в продакшене")
        if self.llm.provider != "fake" and self.llm.api_key is None:
            problems.append("HIREBOT_LLM__API_KEY обязателен для выбранного провайдера")
        if problems:
            msg = "Некорректная конфигурация для production: " + "; ".join(problems)
            raise ValueError(msg)
        return self

    @property
    def is_local(self) -> bool:
        return self.env in {"local", "test"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кэшированный доступ к настройкам (читаются один раз за процесс)."""
    return Settings()


def reset_settings_cache() -> None:
    """Сбросить кэш настроек — нужно тестам, меняющим окружение."""
    get_settings.cache_clear()
