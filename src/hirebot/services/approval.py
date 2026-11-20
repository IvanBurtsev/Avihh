"""Очередь подтверждения: черновики сообщений кандидатам.

Зачем это нужно отдельным сервисом.

Автоматический рекрутер без человека опасен: ошибка модели уходит реальному
кандидату от имени компании. Но и полностью ручной режим бессмысленен — тогда
теряется то, ради чего продукт покупают. Компромисс: бот **решает** и **пишет
текст**, а отправка происходит после одного нажатия рекрутера.

Правила, которые здесь соблюдаются:

* **Один ожидающий черновик на диалог.** Новый вытесняет старый: пока рекрутер
  смотрел черновик, кандидат мог ответить, и старое сообщение уже неверно.
  Инвариант держится и в коде, и частичным уникальным индексом в БД.
* **Идемпотентная отправка.** Ключ совпадает с тем, что был бы при прямой
  отправке, поэтому двойное нажатие не отправит кандидату два сообщения.
* **Провенанс.** В черновике сохраняется модель и версия промпта: по истории
  видно, чем сгенерирован текст, который ушёл человеку.
* **Рекрутер может править.** Одобрение принимает отредактированный текст —
  это его сообщение, и ответственность на нём.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from hirebot.domain.entities import DraftMessage, Message, new_id
from hirebot.domain.enums import ConversationState, DraftStatus, MessageDirection, MessageKind
from hirebot.domain.errors import NotFound, PolicyViolation
from hirebot.domain.fsm import assert_transition
from hirebot.logging_setup import get_logger
from hirebot.observability.metrics import record_message_sent
from hirebot.services.base import ServiceBase
from hirebot.services.messaging import build_idempotency_key, count_outbound_kind

logger = get_logger(__name__)

#: Сколько черновиков отдавать в очередь за один раз.
DEFAULT_PENDING_LIMIT = 100

#: Максимальная длина одобряемого текста — та же, что у ручного сообщения.
MAX_DRAFT_CHARS = 2000


@dataclass(slots=True, frozen=True)
class PendingDraft:
    """Черновик с контекстом для интерфейса рекрутера."""

    draft_id: UUID
    conversation_id: UUID
    candidate_name: str
    vacancy_title: str
    channel: str
    body: str
    kind: str
    reason: str
    model: str | None
    created_at: datetime
    state: ConversationState
    score: float | None


class ApprovalService(ServiceBase):
    """Создание черновиков и их одобрение или отклонение."""

    # -------------------------------------------------------------- создание

    async def prepare(
        self,
        conversation_id: UUID,
        *,
        body: str,
        kind: MessageKind,
        target_state: ConversationState,
        reason: str = "",
        model: str | None = None,
        prompt_version: str | None = None,
        generated_by_llm: bool = False,
    ) -> DraftMessage:
        """Положить готовый текст в очередь подтверждения.

        Вызывается из шага воронки, когда включён режим обязательного одобрения:
        бот уже принял решение, но отправка ждёт человека.
        """
        draft = DraftMessage(
            id=new_id(),
            conversation_id=conversation_id,
            body=body,
            kind=kind,
            target_state=target_state,
            reason=reason,
            model=model,
            prompt_version=prompt_version,
            generated_by_llm=generated_by_llm,
            created_at=self.now(),
        )
        async with self.uow() as uow:
            saved = await uow.drafts.add(draft)
            await uow.commit()
        logger.info(
            "draft_prepared",
            conversation_id=str(conversation_id),
            draft_id=str(saved.id),
            kind=kind.value,
            target_state=target_state.value,
        )
        return saved

    # -------------------------------------------------------------- чтение

    async def list_pending(self, *, limit: int = DEFAULT_PENDING_LIMIT) -> list[PendingDraft]:
        """Очередь подтверждения с контекстом: кто, куда, что и почему."""
        async with self.uow() as uow:
            drafts = list(await uow.drafts.list_pending(limit=limit))
            result: list[PendingDraft] = []
            for draft in drafts:
                conversation = await uow.conversations.get(draft.conversation_id)
                if conversation is None:
                    continue
                candidate = await uow.candidates.get(conversation.candidate_id)
                vacancy = await uow.vacancies.get(conversation.vacancy_id)
                screening = await uow.screenings.get_latest(
                    conversation.candidate_id, conversation.vacancy_id
                )
                result.append(
                    PendingDraft(
                        draft_id=draft.id,
                        conversation_id=draft.conversation_id,
                        candidate_name=(candidate.full_name if candidate is not None else "Без имени"),
                        vacancy_title=vacancy.title if vacancy is not None else "—",
                        channel=conversation.channel.value,
                        body=draft.body,
                        kind=draft.kind.value,
                        reason=draft.reason,
                        model=draft.model,
                        created_at=draft.created_at,
                        state=conversation.state,
                        score=screening.total_score if screening is not None else None,
                    )
                )
        return result

    async def count_pending(self) -> int:
        async with self.uow() as uow:
            return len(await uow.drafts.list_pending(limit=1000))

    # -------------------------------------------------------------- решения

    async def approve(
        self,
        draft_id: UUID,
        *,
        reviewer: str = "recruiter",
        text: str | None = None,
    ) -> Message:
        """Одобрить черновик: отправить сообщение и завершить шаг воронки.

        Транзакция одна на всё: отправка, запись сообщения, смена состояния и
        статус черновика. Если отправка упала, черновик остаётся ожидающим —
        рекрутер увидит ошибку и сможет повторить.
        """
        async with self.uow() as uow:
            draft = await uow.drafts.get(draft_id)
            if draft is None:
                raise NotFound(f"Черновик {draft_id} не найден")
            if draft.status is not DraftStatus.PENDING:
                raise PolicyViolation(f"Черновик уже обработан (статус {draft.status.value})")

            conversation = await uow.conversations.get_with_lock(draft.conversation_id)
            if conversation is None:
                raise NotFound(f"Диалог {draft.conversation_id} не найден")

            body = (text or draft.body).strip()
            if not body:
                raise PolicyViolation("Одобренный текст не может быть пустым")
            if len(body) > MAX_DRAFT_CHARS:
                raise PolicyViolation(f"Текст длиннее {MAX_DRAFT_CHARS} символов")

            channel = self.container.channel(conversation.channel)
            stored = list(await uow.messages.list_for_conversation(conversation.id, limit=200))
            # Ключ тот же, что был бы при прямой отправке: повторное одобрение
            # (или повтор после таймаута) не создаст второе сообщение.
            ordinal = 1 + count_outbound_kind(stored, draft.kind)
            idempotency_key = build_idempotency_key(conversation.id, draft.kind, ordinal=ordinal)

            sent = await channel.send_message(
                conversation.external_id,
                body,
                idempotency_key=idempotency_key,
                kind_hint=draft.kind.value,
            )
            record_message_sent(conversation.channel.value, draft.kind.value)

            now = self.now()
            message = Message(
                id=new_id(),
                conversation_id=conversation.id,
                direction=MessageDirection.OUTBOUND,
                body=body,
                kind=draft.kind,
                external_id=sent.external_id,
                generated_by_llm=draft.generated_by_llm,
                model=draft.model,
                prompt_version=draft.prompt_version,
                created_at=now,
                sent_at=sent.sent_at,
            )
            await uow.messages.add(message)

            # Состояние меняем только если переход допустим: пока черновик ждал,
            # диалог мог уйти дальше (например, кандидат отказался).
            if conversation.state is not draft.target_state:
                assert_transition(conversation.state, draft.target_state)
                conversation.state = draft.target_state
            conversation.auto_messages_sent += 1
            conversation.last_outbound_at = sent.sent_at or now
            conversation.updated_at = now
            await uow.conversations.upsert(conversation)

            draft.approve(reviewer, text=body, now=now)
            await uow.drafts.save(draft)
            await uow.commit()

        logger.info(
            "draft_approved",
            draft_id=str(draft_id),
            conversation_id=str(draft.conversation_id),
            reviewer=reviewer,
            edited=bool(text and text.strip() != draft.body.strip()),
        )
        return message

    async def reject(
        self,
        draft_id: UUID,
        *,
        reviewer: str = "recruiter",
        reason: str | None = None,
    ) -> None:
        """Отклонить черновик: сообщение не отправляется, диалог остаётся человеку.

        Диалог сознательно остаётся в ``HUMAN_REVIEW``: отклонение черновика —
        решение человека не писать сейчас, а не закрытие кандидата.
        """
        async with self.uow() as uow:
            draft = await uow.drafts.get(draft_id)
            if draft is None:
                raise NotFound(f"Черновик {draft_id} не найден")
            if draft.status is not DraftStatus.PENDING:
                raise PolicyViolation(f"Черновик уже обработан (статус {draft.status.value})")
            draft.reject(reviewer, reason=reason, now=self.now())
            await uow.drafts.save(draft)
            await uow.commit()

        logger.info(
            "draft_rejected",
            draft_id=str(draft_id),
            conversation_id=str(draft.conversation_id),
            reviewer=reviewer,
            has_reason=bool(reason),
        )


__all__ = ["DEFAULT_PENDING_LIMIT", "MAX_DRAFT_CHARS", "ApprovalService", "PendingDraft"]
