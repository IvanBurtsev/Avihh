"""Тестовые и демонстрационные реализации портов.

Импорт этого пакета не требует ни БД, ни сети: ``hirebot.testing.fakes``
собирает ``Container``, на котором воронка проходит целиком.
"""

from hirebot.testing.fakes import (
    FakeCalendarAdapter,
    FakeChannelAdapter,
    FakeClock,
    FakeLlmAdapter,
    FakeMeeting,
    InMemoryJobQueue,
    InMemoryStore,
    InMemoryUnitOfWork,
    build_fake_container,
    demo_responses,
    fake_calendar,
    fake_channel,
    fake_llm,
    fake_queue,
    opened_store,
    seed_demo_data,
)

__all__ = [
    "FakeCalendarAdapter",
    "FakeChannelAdapter",
    "FakeClock",
    "FakeLlmAdapter",
    "FakeMeeting",
    "InMemoryJobQueue",
    "InMemoryStore",
    "InMemoryUnitOfWork",
    "build_fake_container",
    "demo_responses",
    "fake_calendar",
    "fake_channel",
    "fake_llm",
    "fake_queue",
    "opened_store",
    "seed_demo_data",
]
