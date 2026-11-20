"""Имена задач очереди и топиков outbox.

Строки вынесены в отдельный модуль, потому что их используют три стороны:
сервисы (кладут задачи), воркер (исполняет) и API (может инициировать вручную).
Опечатка в литерале тут стоила бы «молча ничего не происходит».
"""

from __future__ import annotations

#: Опросить площадку и разложить отклики.
POLL_CHANNEL_TOPIC = "poll_channel"

#: Обработать шаг воронки по диалогу.
PROCESS_CONVERSATION_TOPIC = "process_conversation"

#: Забронировать слот собеседования.
BOOK_INTERVIEW_TOPIC = "book_interview"

#: Разослать накопленный outbox.
DISPATCH_OUTBOX_TOPIC = "dispatch_outbox"

#: Отправить уже сохранённое сообщение (используется, когда запись в ``messages``
#: и отправка разнесены по разным шагам).
SEND_MESSAGE_TOPIC = "send_message"

JOB_NAMES: tuple[str, ...] = (
    POLL_CHANNEL_TOPIC,
    PROCESS_CONVERSATION_TOPIC,
    BOOK_INTERVIEW_TOPIC,
    DISPATCH_OUTBOX_TOPIC,
)
