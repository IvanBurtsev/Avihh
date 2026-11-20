"""Низкоуровневые вызовы HH Employer API через ``HttpClientPort``.

Здесь нет ни одного собственного ``httpx.AsyncClient``: ретраи, лимиты, таймауты
и редакция секретов уже реализованы в инфраструктурном HTTP-клиенте. Задача
этого модуля — правильно собрать запрос, добавить обязательные заголовки и
перевести HTTP-ответ в доменную ошибку.

Что важно и неочевидно:

* заголовок ``HH-User-Agent`` обязателен, иначе ``400 bad_user_agent``;
* пути действий (``actions[].url``) не конструируются, а приходят от сервера —
  клиент лишь проверяет, что URL принадлежит тому же хосту, что и API;
* ``403`` и ``404`` у резюме означают разное: 404 — «нет **или** недоступно»,
  403 — права/квота/услуги. Оба случая — ``ResumeAccessDenied``, но для разных
  решений сервиса (повтор запроса бессмысленен в обоих).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from hirebot.adapters.channels.hh.auth import HhTokenManager
from hirebot.adapters.http_utils import error_code, error_summary, retry_after_s
from hirebot.adapters.parsing import as_dict
from hirebot.config import HHSettings
from hirebot.domain.errors import (
    AuthExpired,
    ChannelUnavailable,
    ConfigError,
    DuplicateAction,
    NotFound,
    PolicyViolation,
    RateLimited,
    ResumeAccessDenied,
)
from hirebot.ports.infra import HttpClientPort, HttpResponse

__all__ = ["HhClient", "ensure_same_host", "raise_for_hh_error"]

#: Коды hh.ru, означающие «нужен новый access-токен» (а не бизнес-отказ).
_AUTH_CODES = frozenset({"token_expired", "token_revoked", "bad_authorization"})

#: Коды hh.ru, означающие «резюме недоступно»: нет прав, квоты или оплаченных услуг.
_RESUME_DENIED_CODES = frozenset(
    {
        "quota_exceeded",
        "cant_view_contacts",
        "no_available_service",
        "action_must_be_payed",
        "api_access_payment",
    }
)

#: Бизнес-ограничения площадки: повтор запроса не поможет, нужен человек или ожидание.
_POLICY_CODES = frozenset(
    {
        "in_a_row_limit",
        "overall_limit",
        "no_invitation",
        "disabled_by_employer",
        "archived",
        "chat_archived",
        "invalid_vacancy",
        "resume_deleted",
        "prohibited",
        "not_enough_purchased_services",
        "empty_message",
        "too_long_message",
        "edit_forbidden",
        "test_required",
        "limit_exceeded",
        "resume_visibility_conflict",
    }
)


def raise_for_hh_error(response: HttpResponse, *, context: str, resume: bool = False) -> None:
    """Перевести HTTP-ответ hh.ru в доменную ошибку (no-op для 2xx).

    Разделение важно для сервисов: ``RateLimited`` ретраится, ``AuthExpired``
    требует переавторизации, ``PolicyViolation`` — решения человека,
    ``ResumeAccessDenied`` — пропуска кандидата без падения цикла.
    """
    status = int(response.status_code)
    if 200 <= status < 300:
        return
    code = (error_code(response) or "").lower()
    message = f"{context}: {code or status} — {error_summary(response)}"

    if status == 429:
        raise RateLimited(message, retry_after_s=retry_after_s(response))
    if status == 401 or code in _AUTH_CODES:
        raise AuthExpired(message)
    if status == 404:
        if resume:
            # 404 у резюме означает «не существует ИЛИ недоступно пользователю».
            raise ResumeAccessDenied(message)
        raise NotFound(message)
    if status == 403:
        if resume and code in _RESUME_DENIED_CODES:
            raise ResumeAccessDenied(message)
        if code in _RESUME_DENIED_CODES:
            raise ResumeAccessDenied(message)
        raise PolicyViolation(message)
    if status == 400:
        if code == "bad_user_agent":
            msg = (
                "hh.ru отклонил запрос: заголовок HH-User-Agent отсутствует или в чёрном списке. "
                "Проверьте HIREBOT_HH__USER_AGENT (формат «приложение/версия (email)»)"
            )
            raise ConfigError(msg)
        raise PolicyViolation(message)
    if status == 409:
        raise DuplicateAction(message)
    if 400 <= status < 500:
        raise PolicyViolation(message)
    raise ChannelUnavailable(message, retry_after_s=retry_after_s(response))


def ensure_same_host(url: str, base_url: str) -> None:
    """Убедиться, что URL действия ведёт на хост API hh.ru.

    URL берётся из ответа сервера (так требует документация), но слепо выполнять
    любую ссылку из JSON нельзя: подмена хоста превратила бы адаптер в средство
    отправки наших токенов на чужой сервер.
    """
    action_host = urlparse(url).netloc.lower()
    api_host = urlparse(base_url).netloc.lower()
    if action_host != api_host:
        msg = f"URL действия ведёт на чужой хост ({action_host or 'пусто'}), ожидался {api_host}"
        raise ChannelUnavailable(msg)


class HhClient:
    """Вызовы ``api.hh.ru`` (отклики, сообщения, действия, резюме, вакансии)."""

    def __init__(
        self,
        settings: HHSettings,
        *,
        http: HttpClientPort,
        tokens: HhTokenManager,
    ) -> None:
        self._settings = settings
        self._http = http
        self._tokens = tokens
        self._base_url = settings.base_url.rstrip("/")

    @property
    def base_url(self) -> str:
        return self._base_url

    def url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    # ------------------------------------------------------------- транспорт

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        form: Mapping[str, Any] | None = None,
        idempotent: bool = True,
        retry_on_auth: bool = True,
    ) -> HttpResponse:
        token = await self._tokens.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "HH-User-Agent": self._settings.user_agent,
            "Accept": "application/json",
        }
        response = await self._http.request(
            method,
            url,
            params=params,
            form=form,
            headers=headers,
            idempotent=idempotent,
            rate_limit_key="hh",
            rate_limit_rps=self._settings.rate_limit.requests_per_second,
            bucket="hh",
        )
        if retry_on_auth and _is_auth_expired(response):
            # Повтор безопасен: сервер явно отверг запрос по авторизации,
            # значит действие не выполнено.
            await self._tokens.force_refresh()
            return await self._request(
                method,
                url,
                params=params,
                form=form,
                idempotent=idempotent,
                retry_on_auth=False,
            )
        return response

    # ------------------------------------------------------------- чтение

    async def get_me(self) -> dict[str, Any]:
        """``GET /me`` — кто мы и какой у нас работодатель."""
        response = await self._request("GET", self.url("/me"))
        raise_for_hh_error(response, context="GET /me")
        return _as_object(response)

    async def list_active_vacancies(
        self, employer_id: str, *, page: int = 0, per_page: int = 100
    ) -> dict[str, Any]:
        """Активные вакансии работодателя.

        Путь ``/employers/{employer_id}/vacancies/active`` — каноническая
        работодательская форма списка вакансий; в репозитории hhru/api раздел
        ``employer_vacancies.md`` заменён заглушкой со ссылкой на OpenAPI, поэтому
        поведение подтверждается на тестовом аккаунте (см. отчёт по адаптерам).
        """
        response = await self._request(
            "GET",
            self.url(f"/employers/{employer_id}/vacancies/active"),
            params={"page": page, "per_page": per_page},
        )
        raise_for_hh_error(response, context="GET /employers/{id}/vacancies/active")
        return _as_object(response)

    async def list_collections(self, vacancy_external_id: str) -> dict[str, Any]:
        """``GET /negotiations?vacancy_id=...`` — коллекции откликов и счётчики.

        Самый дешёвый индикатор «есть новое» — ``collections[].counters.with_updates``.
        """
        response = await self._request(
            "GET",
            self.url("/negotiations"),
            params={"vacancy_id": vacancy_external_id, "with_generated_collections": "false"},
        )
        raise_for_hh_error(response, context="GET /negotiations")
        return _as_object(response)

    async def list_negotiations(
        self,
        url: str,
        *,
        vacancy_external_id: str,
        per_page: int = 50,
        page: int = 0,
        show_only_new_responses: bool = True,
    ) -> dict[str, Any]:
        """Список откликов внутри конкретной коллекции (URL берём из ответа)."""
        ensure_same_host(url, self._base_url)
        response = await self._request(
            "GET",
            url,
            params={
                "vacancy_id": vacancy_external_id,
                "page": page,
                "per_page": max(1, min(50, per_page)),
                "show_only_new_responses": str(show_only_new_responses).lower(),
            },
        )
        raise_for_hh_error(response, context="GET negotiations collection")
        return _as_object(response)

    async def get_negotiation(self, url: str) -> dict[str, Any]:
        """Полный отклик/приглашение (``GET /negotiations/{nid}``)."""
        ensure_same_host(url, self._base_url)
        response = await self._request("GET", url)
        raise_for_hh_error(response, context="GET negotiation")
        return _as_object(response)

    async def get_resume(self, url: str) -> dict[str, Any]:
        """Полное резюме по URL из ответа площадки.

        Документация hh.ru требует запрашивать резюме именно по ``resume.url``:
        только тогда запрос корректно учитывается в истории просмотров и в
        списании услуг.
        """
        ensure_same_host(url, self._base_url)
        response = await self._request("GET", url)
        raise_for_hh_error(response, context="GET resume", resume=True)
        return _as_object(response)

    async def list_messages(
        self, url: str, *, page: int = 0, per_page: int = 50, with_text_only: bool = False
    ) -> dict[str, Any]:
        """Переписка по отклику (``messages_url`` из ответа сервера)."""
        ensure_same_host(url, self._base_url)
        response = await self._request(
            "GET",
            url,
            params={
                "page": page,
                "per_page": per_page,
                "with_text_only": str(with_text_only).lower(),
            },
        )
        raise_for_hh_error(response, context="GET negotiation messages")
        return _as_object(response)

    # ------------------------------------------------------------- запись

    async def send_message(self, url: str, text: str) -> HttpResponse:
        """``POST {messages_url}`` — тело ``form-urlencoded``, поле ``message``.

        ``idempotency_key`` в этот метод hh.ru не принимает (он есть только в
        Chats API), поэтому защита от дублей — на стороне сервиса (outbox +
        уникальный ключ в БД), а транспорт не ретраит запрос автоматически.
        """
        ensure_same_host(url, self._base_url)
        response = await self._request("POST", url, form={"message": text}, idempotent=False)
        raise_for_hh_error(response, context="POST negotiation message")
        return response

    async def execute_action(self, url: str, method: str, form: Mapping[str, Any]) -> HttpResponse:
        """Выполнить действие ровно так, как его описала площадка.

        ``url`` и ``method`` приходят из ``actions[]``; аргументы — из
        ``actions[].arguments``. Ничего не конструируем сами: hh.ru прямо
        предупреждает, что состав аргументов может отличаться для каждого отклика.
        """
        ensure_same_host(url, self._base_url)
        response = await self._request(method.upper(), url, form=form or None, idempotent=False)
        raise_for_hh_error(response, context=f"{method.upper()} negotiation action")
        return response


def _is_auth_expired(response: HttpResponse) -> bool:
    if int(response.status_code) == 401:
        return True
    if int(response.status_code) != 403:
        return False
    return (error_code(response) or "").lower() in _AUTH_CODES


def _as_object(response: HttpResponse) -> dict[str, Any]:
    return as_dict(response.body)
