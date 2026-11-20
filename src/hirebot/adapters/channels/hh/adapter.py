"""Адаптер канала hh.ru (официальный Employer API).

Реализует ``ChannelPort`` и полностью скрывает особенности площадки от сервисов:

* поллинг откликов через коллекции + дешёвый индикатор ``counters.with_updates``;
* действия (пригласить/отказать/отложить) выполняются строго по ``actions[]``
  из ответа сервера — пути не хардкодятся;
* переписка только после приглашения (``messaging_status`` проверяется перед
  отправкой, чтобы не получить 403 и не считать это сбоем транспорта);
* резюме запрашивается по ``resume.url`` ответа (требование hh.ru: иначе запрос
  не учитывается в истории просмотров и в списании услуг).

Про хранение: логины и пароли пользователей hh.ru не используются и не хранятся
(п. 3.6 Условий API) — только OAuth-токены, и они лежат в зашифрованном виде.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from hirebot.adapters.channels.hh.auth import HhTokenManager
from hirebot.adapters.channels.hh.client import HhClient
from hirebot.adapters.channels.hh.mapper import (
    parse_conversation,
    parse_inbound_response,
    parse_message,
    parse_resume,
    parse_vacancy,
)
from hirebot.adapters.parsing import as_dict, as_list, dig, int_or_zero, str_or_empty, str_or_none
from hirebot.config import HHSettings
from hirebot.domain.enums import Channel, MessageDirection
from hirebot.domain.errors import (
    ChannelNotConfigured,
    DomainError,
    NotFound,
    PolicyViolation,
)
from hirebot.logging_setup import get_logger
from hirebot.ports.channels import ChannelPort
from hirebot.ports.dto import (
    ChannelAction,
    ConversationSnapshot,
    InboundMessage,
    InboundResponse,
    ResumePayload,
    SentMessage,
    VacancyPayload,
)
from hirebot.ports.infra import CachePort, ClockPort, HttpClientPort, HttpResponse, LockPort

__all__ = ["HhChannelAdapter"]

logger = get_logger(__name__)

_VACANCIES_TTL_S = 300.0  # 5 минут: поллинг не должен бить API каждый цикл
_ME_TTL_S = 86_400.0
_URL_REGISTRY_TTL_S = 60 * 60 * 24 * 30.0
_MAX_VACANCY_PAGES = 5


class HhChannelAdapter(ChannelPort):
    """Площадка hh.ru."""

    channel = Channel.HH

    def __init__(
        self,
        settings: HHSettings,
        *,
        http: HttpClientPort,
        clock: ClockPort,
        cache: CachePort,
        cipher: object | None = None,
        locks: LockPort | None = None,
        oauth_url: str | None = None,
    ) -> None:
        self._settings = settings
        self._cache = cache
        self._clock = clock
        self._tokens = HhTokenManager(
            settings,
            http=http,
            clock=clock,
            cache=cache,
            locks=locks,
            cipher=cipher,
            oauth_url=oauth_url,
        )
        self._client = HhClient(settings, http=http, tokens=self._tokens)

    @property
    def tokens(self) -> HhTokenManager:
        """Доступ к менеджеру токенов (нужен API-слою для OAuth-редиректа)."""
        return self._tokens

    # ------------------------------------------------------------ служебное

    def _require_enabled(self) -> None:
        if not self._settings.enabled:
            msg = (
                "Канал hh.ru выключен: задайте HIREBOT_HH__ENABLED=true "
                "и учётные данные приложения (client_id/client_secret)"
            )
            raise ChannelNotConfigured(msg)

    async def healthcheck(self) -> bool:
        """Проверить доступность площадки текущими токенами (``GET /me``)."""
        if not self._settings.enabled:
            return False
        try:
            await self._client.get_me()
        except DomainError as exc:
            logger.warning("hh_healthcheck_failed", error=str(exc))
            return False
        return True

    # ------------------------------------------------------------ вакансии

    async def list_vacancies(self) -> Sequence[VacancyPayload]:
        """Активные вакансии работодателя (с кэшем 5 минут)."""
        self._require_enabled()
        cache_key = "vacancies:hh:self"
        cached = await self._cache.get_json(cache_key)
        if isinstance(cached, list) and cached:
            return [parse_vacancy(as_dict(item)) for item in cached]

        employer_id = await self._employer_id()
        vacancies: list[VacancyPayload] = []
        for page in range(_MAX_VACANCY_PAGES):
            payload = await self._client.list_active_vacancies(employer_id, page=page, per_page=100)
            items = as_list(payload.get("items"))
            vacancies.extend(parse_vacancy(as_dict(item)) for item in items)
            if len(items) < 100:
                break
        await self._cache.set_json(
            cache_key,
            [vacancy.raw for vacancy in vacancies],
            ttl_s=_VACANCIES_TTL_S,
        )
        return vacancies

    async def _employer_id(self) -> str:
        me = as_dict(await self._cache.get_json("hh:me"))
        if not me:
            me = await self._client.get_me()
            await self._cache.set_json("hh:me", me, ttl_s=_ME_TTL_S)
        employer_id = str_or_none(dig(me, "employer", "id"))
        if employer_id is None:
            msg = (
                "hh.ru не вернул employer.id: токен принадлежит не менеджеру работодателя. "
                "Нужна авторизация менеджера (authorization_code)"
            )
            raise ChannelNotConfigured(msg)
        return employer_id

    # ------------------------------------------------------------ отклики

    async def fetch_new_responses(
        self, *, vacancy_external_id: str | None = None, limit: int = 50
    ) -> Sequence[InboundResponse]:
        """Свежие (непросмотренные) отклики по вакансиям.

        Схема дешёвая: сначала ``GET /negotiations?vacancy_id=...`` — там лежат
        счётчики ``counters.with_updates``; только если они не нулевые, тянем
        коллекцию с ``show_only_new_responses=true``.
        """
        self._require_enabled()
        if limit <= 0:
            return []
        if vacancy_external_id:
            vacancy_ids: list[str] = [vacancy_external_id]
        else:
            vacancy_ids = [vacancy.external_id for vacancy in await self.list_vacancies()]
        collected: list[InboundResponse] = []
        for vacancy_id in vacancy_ids:
            if len(collected) >= limit:
                break
            collections = await self._client.list_collections(vacancy_id)
            picked = self._pick_collection(collections)
            if picked is None:
                continue
            collection_url, _updates = picked
            payload = await self._client.list_negotiations(
                collection_url,
                vacancy_external_id=vacancy_id,
                per_page=min(50, limit - len(collected)),
                show_only_new_responses=True,
            )
            for raw_item in as_list(payload.get("items")):
                item = as_dict(raw_item)
                collected.append(parse_inbound_response(item, vacancy_external_id=vacancy_id))
                await self._remember_negotiation(item)
        return collected[:limit]

    @staticmethod
    def _pick_collection(collections: Mapping[str, Any]) -> tuple[str, int] | None:
        """Выбрать коллекцию для чтения новых откликов.

        ``show_only_new_responses`` работает только у коллекции «Все неразобранные»
        (``id == "response"``). Если в ней нет откликов, требующих внимания
        (``counters.with_updates == 0``), запросы к ней не делаем вовсе.
        """
        fallback: tuple[str, int] | None = None
        for raw_collection in as_list(collections.get("collections")):
            collection = as_dict(raw_collection)
            url = str_or_empty(collection.get("url"))
            if not url:
                continue
            updates = int_or_zero(dig(collection, "counters", "with_updates"))
            if str_or_empty(collection.get("id")) == "response":
                return (url, updates) if updates > 0 else None
            if updates > 0 and fallback is None:
                fallback = (url, updates)
        return fallback

    async def _remember_negotiation(self, item: Mapping[str, Any]) -> None:
        """Запомнить URL отклика и переписки: площадка отдаёт их в ответе.

        Свои URL мы не конструируем, поэтому запоминаем серверные (в кэше, с TTL):
        иначе после рестарта процесса пришлось бы угадывать путь.
        """
        negotiation_id = str_or_empty(item.get("id"))
        if not negotiation_id:
            return
        url = str_or_none(item.get("url"))
        if url:
            await self._cache.set_json(f"hh:negotiation_url:{negotiation_id}", url, ttl_s=_URL_REGISTRY_TTL_S)
        messages_url = str_or_none(item.get("messages_url"))
        if messages_url:
            await self._cache.set_json(
                f"hh:messages_url:{negotiation_id}", messages_url, ttl_s=_URL_REGISTRY_TTL_S
            )
        resume = as_dict(item.get("resume"))
        resume_id = str_or_none(resume.get("id"))
        resume_url = str_or_none(resume.get("url"))
        if resume_id and resume_url:
            await self._cache.set_json(f"hh:resume_url:{resume_id}", resume_url, ttl_s=_URL_REGISTRY_TTL_S)

    # ------------------------------------------------------------ резюме

    async def fetch_resume(self, resume_external_id: str) -> ResumePayload:
        """Полное резюме по идентификатору.

        URL берём из реестра (он наполняется при чтении откликов). Если реестр
        пуст (например, кэш сброшен), используем документированный путь
        ``/resumes/{id}`` — но помечаем это в логе: запрос «не по URL из ответа»
        может не попасть в историю просмотров.
        """
        self._require_enabled()
        registered = await self._cache.get_json(f"hh:resume_url:{resume_external_id}")
        url = str_or_none(registered)
        if url is None:
            url = self._client.url(f"/resumes/{resume_external_id}")
            logger.warning("hh_resume_url_not_registered", resume_id=resume_external_id)
        raw = await self._client.get_resume(url)
        return parse_resume(raw, external_id=resume_external_id, source_url=url)

    # ------------------------------------------------------------ диалог

    async def get_conversation(self, conversation_external_id: str) -> ConversationSnapshot:
        """Состояние отклика: доступна ли переписка и какие действия разрешены."""
        self._require_enabled()
        raw = await self._client.get_negotiation(await self._negotiation_url(conversation_external_id))
        await self._remember_negotiation(raw)
        return parse_conversation(raw)

    async def list_messages(
        self, conversation_external_id: str, *, since: datetime | None = None
    ) -> Sequence[InboundMessage]:
        """История переписки (кэш не применяется: устаревшие данные дают неверные ответы)."""
        self._require_enabled()
        registered = await self._cache.get_json(f"hh:messages_url:{conversation_external_id}")
        url = str_or_none(registered) or self._client.url(
            f"/negotiations/{conversation_external_id}/messages"
        )
        payload = await self._client.list_messages(url, per_page=50)
        messages = [parse_message(as_dict(item)) for item in as_list(payload.get("items"))]
        if since is not None:
            messages = [message for message in messages if message.created_at >= since]
        return messages

    async def send_message(
        self,
        conversation_external_id: str,
        text: str,
        *,
        idempotency_key: str,
        kind_hint: str | None = None,
    ) -> SentMessage:
        """Отправить сообщение в чат отклика.

        Перед отправкой проверяем ``messaging_status``: hh.ru отдаёт 403
        ``no_invitation``/``in_a_row_limit``, и это не сбой транспорта, а
        бизнес-правило — сервис должен принять решение (пригласить или позвать
        человека), а не ретраить.
        """
        self._require_enabled()
        snapshot = await self.get_conversation(conversation_external_id)
        if not snapshot.messaging_available:
            reason = snapshot.reason_unavailable or "переписка недоступна"
            raise PolicyViolation(
                f"hh.ru: {reason} (отклик {conversation_external_id}, kind={kind_hint or 'n/a'})"
            )
        registered = await self._cache.get_json(f"hh:messages_url:{conversation_external_id}")
        url = str_or_none(registered) or self._client.url(
            f"/negotiations/{conversation_external_id}/messages"
        )
        logger.info(
            "hh_send_message",
            conversation=conversation_external_id,
            idempotency_key=idempotency_key,
            kind=kind_hint or "n/a",
        )
        response = await self._client.send_message(url, text)
        external_id = await self._best_effort_message_id(response, url, text)
        return SentMessage(external_id=external_id, sent_at=self._clock.now())

    async def _best_effort_message_id(
        self, response: HttpResponse, messages_url: str, text: str
    ) -> str | None:
        """Определить ``external_id`` отправленного сообщения.

        ``POST /negotiations/{id}/messages`` отвечает ``201`` с пустым телом и не
        возвращает идентификатор. Идемпотентность в нашей БД держится на
        ``idempotency_key``, но DTO требует ``external_id``, если он есть. Поэтому:
        сначала пробуем заголовок ``Location``, затем — один дешёвый запрос
        переписки и поиск последнего исходящего сообщения с тем же текстом.
        """
        for key, value in response.headers.items():
            if key.lower() == "location" and value:
                return value.rstrip("/").rsplit("/", 1)[-1]
        try:
            payload = await self._client.list_messages(messages_url, per_page=10)
        except DomainError as exc:
            logger.warning("hh_message_id_lookup_failed", error=str(exc))
            return None
        normalized = " ".join(text.split()).casefold()
        for raw_item in as_list(payload.get("items")):
            item = as_dict(raw_item)
            candidate = parse_message(item)
            if candidate.direction is not MessageDirection.OUTBOUND:
                continue
            if " ".join(candidate.text.split()).casefold() == normalized:
                return candidate.external_id
        return None

    async def perform_action(
        self,
        conversation_external_id: str,
        action_key: str,
        *,
        message: str | None = None,
        idempotency_key: str,
        extra_args: dict[str, str] | None = None,
    ) -> None:
        """Выполнить действие площадки, найденное в ``actions[]``.

        URL и метод берутся из ответа сервера (никакого хардкода путей). Состав
        аргументов тоже приходит от сервера: если обязательный аргумент заполнить
        нечем — это ошибка вызывающего кода (``ValueError``), а не сбой площадки.
        """
        self._require_enabled()
        snapshot = await self.get_conversation(conversation_external_id)
        action = _find_action(snapshot.actions, action_key)
        if action is None:
            available = ", ".join(sorted(item.key for item in snapshot.actions)) or "нет"
            msg = (
                f"hh.ru не разрешает действие {action_key!r} для отклика "
                f"{conversation_external_id}. Доступные действия: {available}"
            )
            raise NotFound(msg)
        if not bool(action.extra_args.get("enabled", True)):
            msg = f"Действие {action_key!r} сейчас недоступно в интерфейсе hh.ru"
            raise PolicyViolation(msg)

        arguments = as_dict(action.extra_args.get("arguments"))
        form: dict[str, str] = {}
        if message is not None:
            if action.requires_message or "message" in arguments:
                form["message"] = message
            else:
                logger.warning(
                    "hh_action_message_ignored",
                    action=action.key,
                    reason="площадка не принимает аргумент message для этого действия",
                )
        for key, value in (extra_args or {}).items():
            form[key] = value

        missing = [name for name, required in arguments.items() if bool(required) and name not in form]
        if action.requires_message and "message" not in form:
            missing.append("message")
        if missing:
            msg = f"Для действия {action.key!r} обязательны аргументы: {', '.join(sorted(set(missing)))}"
            raise ValueError(msg)

        logger.info(
            "hh_perform_action",
            action=action.key,
            conversation=conversation_external_id,
            idempotency_key=idempotency_key,
        )
        await self._client.execute_action(action.url, action.method, form)

    async def _negotiation_url(self, conversation_external_id: str) -> str:
        registered = await self._cache.get_json(f"hh:negotiation_url:{conversation_external_id}")
        return str_or_none(registered) or self._client.url(f"/negotiations/{conversation_external_id}")


def _find_action(actions: Sequence[ChannelAction], action_key: str) -> ChannelAction | None:
    """Найти действие по ключу: сверяем ``id``, затем заголовок (регистр не важен)."""
    wanted = action_key.strip().casefold()
    for action in actions:
        if action.key.casefold() == wanted:
            return action
    for action in actions:
        if action.title.strip().casefold() == wanted:
            return action
    return None
