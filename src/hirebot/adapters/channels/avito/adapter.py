"""Адаптер канала Avito (Messenger API).

Доступ к Avito API выдаётся по заявке и подтверждается модерацией: без
подтверждённого доступа контракт Messenger API проверить не на чем. Условия
использования Avito запрещают автоматизацию пользовательского интерфейса и
парсинг — поэтому адаптер **не должен** использоваться для сбора данных из UI:
только официальные endpoint-ы, и только после выдачи доступа. Пока доступ не
подтверждён, Avito в проекте — риск, а не функция (раздел 6.2 ARCHITECTURE.md).

Практическое следствие для кода: там, где контракт неизвестен, адаптер **не
угадывает путь**, а бросает ``ChannelNotConfigured`` с понятным текстом. Это
честная деградация: поллинг не падает, а интеграция не отправляет запросы
«наугад» на боевой аккаунт.

Режим ``HIREBOT_AVITO__ENABLED=false`` (значение по умолчанию):

* методы чтения отдают пустые списки/пустые DTO — пайплайн продолжает работу;
* методы записи бросают ``ChannelNotConfigured`` — писать в выключенный канал
  нельзя даже случайно.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from hirebot.config import AvitoSettings
from hirebot.domain.enums import Channel
from hirebot.domain.errors import ChannelNotConfigured
from hirebot.logging_setup import get_logger
from hirebot.ports.channels import ChannelPort
from hirebot.ports.dto import (
    ConversationSnapshot,
    InboundMessage,
    InboundResponse,
    ResumePayload,
    SentMessage,
    VacancyPayload,
)
from hirebot.ports.infra import CachePort, ClockPort, HttpClientPort

__all__ = ["AvitoChannelAdapter"]

logger = get_logger(__name__)


class AvitoChannelAdapter(ChannelPort):
    """Площадка Avito.

    Доступ к Avito API выдаётся по заявке и подтверждается модерацией, при этом
    часть методов может быть недоступна даже после одобрения. Автоматизация UI
    (в том числе парсинг страниц) прямо запрещена условиями использования и
    приводит к блокировке аккаунта, поэтому адаптер работает исключительно
    через официальный API и не должен использоваться для парсинга.

    Текущий статус: контракт Messenger API не подтверждён — исследовательских
    материалов по нему в репозитории нет (``docs/research/avito-api.md`` не
    существует, а страницы документации отдаются как SPA без схемы запросов).
    Поэтому при ``enabled=True`` методы, требующие сетевого вызова, поднимают
    ``ChannelNotConfigured`` и **не выполняют запросов**.
    """

    channel = Channel.AVITO

    def __init__(
        self,
        settings: AvitoSettings,
        *,
        http: HttpClientPort,
        clock: ClockPort,
        cache: CachePort | None = None,
        cipher: object | None = None,
    ) -> None:
        self._settings = settings
        # Зависимости сохраняются, чтобы после подтверждения доступа реализовать
        # вызовы без изменения конструктора: трафик идёт только через HttpClientPort,
        # токены хранятся зашифрованными (cipher), справочники кэшируются (cache).
        self._http = http
        self._clock = clock
        self._cache = cache
        self._cipher = cipher

    @property
    def is_enabled(self) -> bool:
        return bool(self._settings.enabled)

    @property
    def base_url(self) -> str:
        """Базовый URL Avito API (используется, когда контракт будет подтверждён)."""
        return self._settings.base_url

    # ------------------------------------------------------------ служебное

    def _not_configured_error(self, operation: str, *, detail: str = "") -> ChannelNotConfigured:
        """Собрать ошибку «площадка не готова к вызову» с понятным текстом.

        Возвращаем объект ошибки, а не бросаем её здесь: так виден явный
        ``raise`` в каждом методе, и невозможно случайно «провалиться» дальше.
        """
        # TODO(avito): уточнить endpoint после получения доступа к API.
        suffix = f" ({detail})" if detail else ""
        if not self._settings.enabled:
            return ChannelNotConfigured(
                f"Avito выключен (HIREBOT_AVITO__ENABLED=false): операция {operation!r} "
                f"недоступна{suffix}. Включите канал и задайте HIREBOT_AVITO__ACCESS_TOKEN "
                "после одобрения заявки на API."
            )
        return ChannelNotConfigured(
            f"Avito: контракт Messenger API для операции {operation!r}{suffix} не подтверждён "
            "(доступ к API выдаётся по заявке и подтверждается модерацией). Endpoint не "
            "реализован намеренно: угадывать пути к боевому аккаунту нельзя."
        )

    async def healthcheck(self) -> bool:
        """Проверка доступности канала.

        Всегда ``False``, пока контракт не подтверждён: подтверждать «здоровье»
        несуществующего вызова нельзя — иначе мониторинг будет врать.
        """
        if not self._settings.enabled:
            return False
        if self._settings.access_token is None:
            logger.warning("avito_token_missing", hint="HIREBOT_AVITO__ACCESS_TOKEN не задан")
            return False
        logger.warning("avito_contract_unverified", hint="endpoint-ы Messenger API не подтверждены")
        return False

    # ------------------------------------------------------------ чтение

    async def list_vacancies(self) -> Sequence[VacancyPayload]:
        if not self.is_enabled:
            return []
        raise self._not_configured_error("list_vacancies")

    async def fetch_new_responses(
        self, *, vacancy_external_id: str | None = None, limit: int = 50
    ) -> Sequence[InboundResponse]:
        if not self.is_enabled:
            return []
        raise self._not_configured_error(
            "fetch_new_responses",
            detail=f"vacancy={vacancy_external_id or 'все'}, limit={limit}",
        )

    async def fetch_resume(self, resume_external_id: str) -> ResumePayload:
        """Резюме недоступно, но это не ошибка: DTO несёт причину.

        Возвращаем ``unavailable_reason``, а не бросаем исключение: сервис
        обработает кандидата по общей ветке «резюме недоступно», и цикл не упадёт.
        """
        if not self.is_enabled:
            return ResumePayload(
                external_id=resume_external_id,
                text="",
                unavailable_reason="Канал Avito выключен (HIREBOT_AVITO__ENABLED=false)",
            )
        raise self._not_configured_error("fetch_resume", detail=f"resume={resume_external_id}")

    async def get_conversation(self, conversation_external_id: str) -> ConversationSnapshot:
        if not self.is_enabled:
            return ConversationSnapshot(
                external_id=conversation_external_id,
                actions=(),
                unread_count=0,
                messaging_available=False,
                reason_unavailable="Канал Avito выключен (HIREBOT_AVITO__ENABLED=false)",
            )
        raise self._not_configured_error(
            "get_conversation", detail=f"conversation={conversation_external_id}"
        )

    async def list_messages(
        self, conversation_external_id: str, *, since: datetime | None = None
    ) -> Sequence[InboundMessage]:
        if not self.is_enabled:
            return []
        raise self._not_configured_error(
            "list_messages",
            detail=(f"conversation={conversation_external_id}, since={since.isoformat() if since else '—'}"),
        )

    # ------------------------------------------------------------ запись

    async def send_message(
        self,
        conversation_external_id: str,
        text: str,
        *,
        idempotency_key: str,
        kind_hint: str | None = None,
    ) -> SentMessage:
        """Отправка сообщения: при выключенном канале — всегда ошибка конфигурации."""
        logger.info(
            "avito_send_message_blocked",
            conversation=conversation_external_id,
            idempotency_key=idempotency_key,
            kind=kind_hint or "n/a",
            chars=len(text),
        )
        raise self._not_configured_error("send_message", detail=f"conversation={conversation_external_id}")

    async def perform_action(
        self,
        conversation_external_id: str,
        action_key: str,
        *,
        message: str | None = None,
        idempotency_key: str,
        extra_args: dict[str, str] | None = None,
    ) -> None:
        logger.info(
            "avito_perform_action_blocked",
            conversation=conversation_external_id,
            action=action_key,
            idempotency_key=idempotency_key,
            has_message=message is not None,
            extra_args=sorted((extra_args or {}).keys()),
        )
        raise self._not_configured_error(
            "perform_action",
            detail=f"conversation={conversation_external_id}, action={action_key}",
        )
