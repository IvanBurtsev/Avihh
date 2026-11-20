"""Удаление и выгрузка персональных данных (152-ФЗ).

Зачем это отдельный модуль, а не «просто DELETE в кроне».

Оператор персональных данных обязан (152-ФЗ):

* **не хранить ПДн дольше, чем нужно** — сроки заданы в ``PrivacySettings`` и
  применяются автоматической задачей;
* **выдать субъекту его данные** по запросу — отсюда экспорт в JSON;
* **удалить данные по требованию** — отсюда полное удаление кандидата.

Тонкость, из-за которой нельзя просто удалить строку кандидата: на неё
ссылаются диалоги, и удаление сломает аналитику и журнал. Поэтому «удаление»
здесь означает: удалить тексты резюме и переписку, а саму учётную запись
кандидата обезличить. Воронка остаётся целой, ПДн — нет.

Все действия логируются: сам факт удаления — тоже событие, которое нужно уметь
подтвердить проверяющему.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from hirebot.config import PrivacySettings
from hirebot.logging_setup import get_logger
from hirebot.ports.repositories import PurgeStats
from hirebot.services.base import ServiceBase

logger = get_logger(__name__)

#: Что подставить вместо персональных данных при обезличивании.
ANONYMIZED_NAME = "Кандидат (данные удалены)"
ANONYMIZED_CONTACT = None


@dataclass(slots=True)
class PurgeReport:
    """Итог прохода по срокам хранения."""

    stats: PurgeStats
    cutoff_date: datetime
    finished_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    @property
    def total(self) -> int:
        return self.stats.total


class PrivacyService(ServiceBase):
    """Сроки хранения, экспорт и удаление персональных данных."""

    @property
    def settings_privacy(self) -> PrivacySettings:
        return self.settings.privacy

    async def purge_expired(self) -> PurgeReport:
        """Удалить данные, срок хранения которых истёк.

        Сроки считаются от текущего момента; для каждой категории свой.
        Задача рассчитана на регулярный запуск (крон воркера или cron в ОС) и
        идемпотентна: повторный запуск удалит только то, что успело «состариться».
        """
        now = self.now()
        privacy = self.settings_privacy
        resumes_before = now - timedelta(days=privacy.resume_retention_days)
        messages_before = now - timedelta(days=privacy.message_retention_days)
        screenings_before = now - timedelta(days=privacy.screening_retention_days)
        drafts_before = now - timedelta(days=privacy.draft_retention_days)
        audit_before = now - timedelta(days=privacy.audit_retention_days)

        async with self.uow() as uow:
            stats = await uow.privacy.purge_before(
                resumes_before=resumes_before,
                messages_before=messages_before,
                screenings_before=screenings_before,
                drafts_before=drafts_before,
                audit_before=audit_before,
                batch_size=privacy.purge_batch_size,
            )
            await uow.commit()

        logger.info("privacy_purge_done", **stats.as_dict())
        return PurgeReport(stats=stats, cutoff_date=resumes_before)

    async def export_candidate(self, candidate_id: UUID) -> dict[str, Any] | None:
        """Выгрузить все данные кандидата: право на доступ к своим данным."""
        async with self.uow() as uow:
            payload = await uow.privacy.export_candidate(candidate_id)
        if payload is None:
            logger.warning("privacy_export_missing", candidate_id=str(candidate_id))
            return None
        logger.info("privacy_export_done", candidate_id=str(candidate_id))
        return payload

    async def erase_candidate(self, candidate_id: UUID) -> bool:
        """Удалить персональные данные кандидата по его требованию.

        Воронка и журнал остаются, ПДн — нет. Факт удаления фиксируется в логе и
        в ``audit_log``: оператор обязан доказать, что требование исполнено.
        """
        async with self.uow() as uow:
            erased = await uow.privacy.erase_candidate(candidate_id)
            if erased:
                await uow.commit()
        if erased:
            logger.warning("privacy_erase_done", candidate_id=str(candidate_id))
        else:
            logger.info("privacy_erase_missing", candidate_id=str(candidate_id))
        return erased


__all__ = ["ANONYMIZED_CONTACT", "ANONYMIZED_NAME", "PrivacyService", "PurgeReport"]
