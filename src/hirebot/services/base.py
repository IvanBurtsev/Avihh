"""Общая часть сервисов воронки.

Сервисы получают собранный ``Container`` и работают только через порты.
Здесь собрано то, что иначе дублировалось бы в каждом сервисе: доступ к
настройкам, часам и транзакционной границе.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from uuid import UUID

from hirebot.config import Settings
from hirebot.container import Container
from hirebot.ports.infra import ClockPort
from hirebot.ports.repositories import UnitOfWork

#: Идентификатор компании-арендатора по умолчанию.
#:
#: Мультитенантность заложена в схему (у всех сущностей есть ``company_id``),
#: но реестра компаний в портах пока нет: ``CompanyRepository`` не объявлен,
#: а ``Company`` нигде не читается. Пока реестр не появился, все новые
#: сущности создаются в этом единственном тенанте. Когда появится порт
#: компаний, значение заменится на реальный ``company_id`` из токена/настроек,
#: и менять придётся только этот модуль и сборку контейнера.
DEFAULT_COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")

#: Часовой пояс по умолчанию, если у кандидата он неизвестен.
DEFAULT_TIMEZONE = "Europe/Moscow"


class ServiceBase:
    """База сервисов: контейнер, настройки, часы, Unit of Work.

    Наследники не создают сессии БД и не знают, Redis у нас или память:
    всё общение с внешним миром идёт через порты контейнера.
    """

    def __init__(self, container: Container) -> None:
        self._container = container

    @property
    def container(self) -> Container:
        return self._container

    @property
    def settings(self) -> Settings:
        return self._container.settings

    @property
    def clock(self) -> ClockPort:
        return self._container.clock

    def now(self) -> datetime:
        """Текущее время (всегда timezone-aware, UTC) — только через ``ClockPort``."""
        return self._container.clock.now()

    def uow(self) -> AbstractAsyncContextManager[UnitOfWork]:
        """Открыть новую транзакционную границу.

        Каждый вызов — отдельная транзакция: сервисы сознательно не держат
        одну длинную транзакцию на время сетевых вызовов, иначе блокировка
        строки диалога висит, пока отвечает LLM или календарь.
        """
        return self._container.uow_factory()
