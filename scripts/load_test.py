"""Нагрузочная проверка: сколько откликов система обрабатывает и как быстро.

Зачем отдельный скрипт, а не тест: нагрузка требует сотен кандидатов и секунд
времени, а обычный прогон тестов должен оставаться быстрым. Здесь — честный
замер на детерминированной заглушке модели (без сети), чтобы отделить скорость
нашей логики от задержек провайдера LLM.

Запуск:

    python scripts/load_test.py --count 300 --concurrency 8

Что проверяется:

* пропускная способность (откликов в секунду);
* ошибки при обработке (их не должно быть);
* рост числа диалогов и сообщений — состояние консистентно;
* поведение при разной степени параллелизма.

Важно: заглушка модели отвечает мгновенно, поэтому цифры показывают **потолок
нашей логики**. С реальной моделью добавьте её задержку: при 2 секундах на
вызов и параллелизме 8 пропускная способность ограничена ~4 отклика/сек.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from hirebot.config import Settings
from hirebot.container import build_container
from hirebot.domain.enums import Channel, ConversationState
from hirebot.ports.dto import InboundResponse, ResumePayload
from hirebot.services import ConversationService, IngestService


def _build_responses(count: int, *, vacancy_external_id: str) -> list[InboundResponse]:
    """Сгенерировать отклики с разными сопроводительными письмами."""
    strong = (
        "Пять лет коммерческой разработки на Python, Django и FastAPI. "
        "Уверенно работаю с PostgreSQL и SQL, проектирую API."
    )
    medium = "Два года на Python, немного SQL, есть pet-проекты."
    weak = "Ищу работу водителем, есть права категории C."
    now = datetime.now(tz=UTC)
    letters = (strong, medium, weak)
    result: list[InboundResponse] = []
    for index in range(count):
        result.append(
            InboundResponse(
                channel=Channel.HH,
                conversation_external_id=f"load-conv-{index}",
                candidate_external_id=f"load-cand-{index}",
                vacancy_external_id=vacancy_external_id,
                candidate_name=f"Кандидат {index}",
                resume_external_id=f"load-resume-{index}",
                cover_letter=letters[index % len(letters)],
                received_at=now - timedelta(minutes=index),
            )
        )
    return result


async def _process_one(
    container: Any, conversation_id: Any, semaphore: asyncio.Semaphore
) -> tuple[bool, str]:
    """Обработать один диалог под ограничением параллелизма."""
    async with semaphore:
        try:
            await ConversationService(container).process(conversation_id)
            return True, ""
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"


async def run(*, count: int, concurrency: int) -> int:
    settings = Settings(
        env="local",
        log_level="ERROR",
        log_json=False,
        _env_file=None,
        database={"dsn": "sqlite+aiosqlite:///./loadtest.db"},
        llm={"provider": "fake"},
    )
    # Замер на настоящей БД: in-memory контур делает снимок всего хранилища на
    # каждую транзакцию, и его цифры не имеют отношения к продакшену.
    container = await build_container(settings)
    from hirebot.testing.fakes import FakeChannelAdapter

    fake_channel = FakeChannelAdapter(Channel.HH, clock=container.clock)
    container.channels = {Channel.HH: fake_channel}
    started = time.perf_counter()

    # 1. Отклики приходят на площадку.
    from hirebot.services.base import DEFAULT_COMPANY_ID

    async with container.uow_factory() as uow:
        vacancies = await uow.vacancies.list_active(DEFAULT_COMPANY_ID)
    if not vacancies:
        print("нет вакансий: сначала выполните hirebot seed")
        return 1
    vacancy = vacancies[0]

    channel = container.channels[Channel.HH]
    assert isinstance(channel, FakeChannelAdapter)
    for response in _build_responses(count, vacancy_external_id=vacancy.external_id):
        channel.add_response(response)
        # Резюме должно существовать, иначе канал откажет в доступе и скрининг
        # уйдёт в «нужен человек», а замер покажет не нашу скорость.
        channel.add_resume(
            ResumePayload(
                external_id=response.resume_external_id or "",
                text=(
                    f"{response.candidate_name}. {response.cover_letter} "
                    "Опыт: коммерческая разработка, PostgreSQL, проектирование API."
                ),
            )
        )

    # Площадка отдаёт отклики страницами, поэтому опрашиваем, пока приходят
    # новые: один вызов ограничен лимитом и не заберёт всё сразу.
    ingest_started = time.perf_counter()
    ingest_service = IngestService(container)
    total_found = 0
    ingest_errors: list[str] = []
    while total_found < count:
        page = await ingest_service.poll(Channel.HH, limit=min(50, count - total_found))
        total_found += page.found
        ingest_errors.extend(page.errors)
        if page.found == 0:
            break
    ingested = time.perf_counter() - ingest_started
    print(f"принято откликов: {total_found} за {ingested:.2f} с, ошибок приёма: {len(ingest_errors)}")

    async with container.uow_factory() as uow:
        conversations = list(
            await uow.conversations.list_by_state(
                [
                    ConversationState.NEW,
                    ConversationState.RESUME_FETCHED,
                ],
                limit=count + 50,
            )
        )
    print(f"диалогов к обработке: {len(conversations)}")

    # 2. Обработка с ограниченным параллелизмом.
    semaphore = asyncio.Semaphore(max(1, concurrency))
    process_started = time.perf_counter()
    results = await asyncio.gather(*(_process_one(container, item.id, semaphore) for item in conversations))
    processed = time.perf_counter() - process_started

    ok = sum(1 for success, _ in results if success)
    failures = [error for success, error in results if not success]
    total = time.perf_counter() - started

    print()
    print(f"обработано: {ok} из {len(conversations)}")
    print(f"время обработки: {processed:.2f} с")
    if processed > 0:
        print(f"пропускная способность: {ok / processed:.1f} откликов/с")
    print(f"полное время (приём + обработка): {total:.2f} с")
    if failures:
        print(f"ОШИБКИ ({len(failures)}):")
        for error in failures[:5]:
            print(f"  - {error}")
    else:
        print("ошибок нет")

    # 3. Консистентность состояния.
    async with container.uow_factory() as uow:
        remaining = await uow.conversations.list_by_state([ConversationState.NEW], limit=count + 50)
        messages_total = 0
        for conversation in conversations[: min(len(conversations), 50)]:
            messages_total += len(await uow.messages.list_for_conversation(conversation.id, limit=100))
    print(f"осталось в состоянии NEW: {len(remaining)}")
    print(f"сообщений у первых 50 диалогов: {messages_total}")

    return 0 if not failures else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Нагрузочная проверка обработки откликов")
    parser.add_argument("--count", type=int, default=200, help="сколько откликов создать")
    parser.add_argument(
        "--concurrency", type=int, default=8, help="сколько диалогов обрабатывать параллельно"
    )
    args = parser.parse_args()
    return asyncio.run(run(count=args.count, concurrency=args.concurrency))


if __name__ == "__main__":
    raise SystemExit(main())
