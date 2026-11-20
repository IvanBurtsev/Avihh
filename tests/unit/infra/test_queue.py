"""Тесты очереди: inline-режим (честный await), dedupe, сериализация payload."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from hirebot.config import Settings
from hirebot.domain.errors import ConfigError, NotFound
from hirebot.infra.clock import FakeClock
from hirebot.infra.queue import (
    ArqJobQueue,
    InlineJobQueue,
    create_queue,
    decode_payload,
    encode_payload,
)
from hirebot.ports.infra import JobQueue


class _Recorder:
    """Обработчик, запоминающий вызовы."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, payload: dict[str, Any]) -> None:
        self.calls.append(payload)


async def test_inline_queue_awaits_handler() -> None:
    queue = InlineJobQueue()
    recorder = _Recorder()
    queue.register("process_conversation", recorder)

    job_id = await queue.enqueue("process_conversation", {"conversation_id": "c-1"})

    assert job_id is not None
    assert job_id.startswith("inline-")
    # Задача выполнена до возврата из enqueue — в этом смысл inline-режима.
    assert recorder.calls == [{"conversation_id": "c-1"}]


async def test_inline_queue_handler_error_propagates() -> None:
    queue = InlineJobQueue()

    async def failing(payload: dict[str, Any]) -> None:
        msg = f"boom: {payload['x']}"
        raise RuntimeError(msg)

    queue.register("job", failing)

    with pytest.raises(RuntimeError, match="boom"):
        await queue.enqueue("job", {"x": 1})


async def test_inline_queue_deduplicates() -> None:
    queue = InlineJobQueue()
    recorder = _Recorder()
    queue.register("job", recorder)

    first = await queue.enqueue("job", {"n": 1}, dedupe_key="step:1")
    second = await queue.enqueue("job", {"n": 2}, dedupe_key="step:1")
    third = await queue.enqueue("job", {"n": 3}, dedupe_key="step:2")

    assert first is not None
    assert second is None
    assert third is not None
    assert recorder.calls == [{"n": 1}, {"n": 3}]


async def test_inline_queue_releases_dedupe_key_on_error() -> None:
    """Упавший обработчик не должен навсегда блокировать повтор задачи."""
    queue = InlineJobQueue()
    attempts: list[int] = []

    async def flaky(payload: dict[str, Any]) -> None:
        attempts.append(payload["n"])
        if payload["n"] == 1:
            msg = "temporary failure"
            raise RuntimeError(msg)

    queue.register("job", flaky)

    with pytest.raises(RuntimeError):
        await queue.enqueue("job", {"n": 1}, dedupe_key="step")

    job_id = await queue.enqueue("job", {"n": 2}, dedupe_key="step")

    assert job_id is not None
    assert attempts == [1, 2]


async def test_inline_queue_applies_delay_virtually(fake_clock: FakeClock) -> None:
    queue = InlineJobQueue(clock=fake_clock)
    recorder = _Recorder()
    queue.register("job", recorder)

    await queue.enqueue("job", {}, delay_s=30)

    assert fake_clock.slept == pytest.approx(30.0)
    assert len(recorder.calls) == 1


async def test_inline_queue_unknown_job_raises() -> None:
    queue = InlineJobQueue()

    with pytest.raises(NotFound, match="не зарегистрирован"):
        await queue.enqueue("unknown", {})


async def test_inline_queue_close_stops_accepting() -> None:
    queue = InlineJobQueue()
    queue.register("job", _Recorder())

    await queue.close()

    with pytest.raises(RuntimeError, match="остановлена"):
        await queue.enqueue("job", {})
    assert queue.registered == ()

    await queue.start()
    recorder = _Recorder()
    queue.register("job", recorder)
    assert await queue.enqueue("job", {"n": 1}) is not None
    assert recorder.calls == [{"n": 1}]


def test_payload_json_roundtrip() -> None:
    payload = {"conversation_id": "c-1", "text": "Здравствуйте, Иван!", "score": 87.5}

    assert decode_payload(encode_payload(payload)) == payload


def test_decode_payload_rejects_non_object() -> None:
    with pytest.raises(ValueError, match="JSON-объектом"):
        decode_payload("[1, 2, 3]")


async def test_arq_queue_requires_registered_handler(settings: Settings) -> None:
    queue = ArqJobQueue(redis_dsn=settings.redis.dsn)

    # Проверяем до обращения к Redis: незарегистрированная задача — ошибка кода.
    with pytest.raises(NotFound, match="не зарегистрирован"):
        await queue.enqueue("job", {})


async def test_arq_queue_registers_handlers(settings: Settings) -> None:
    queue = ArqJobQueue(redis_dsn=settings.redis.dsn)

    queue.register("job", _Recorder())

    assert queue.registered == ("job",)


def test_arq_queue_satisfies_port(settings: Settings) -> None:
    assert isinstance(ArqJobQueue(redis_dsn=settings.redis.dsn), JobQueue)


async def test_create_queue_returns_inline_by_default(settings: Settings) -> None:
    queue = await create_queue(settings)

    assert isinstance(queue, InlineJobQueue)
    assert isinstance(queue, JobQueue)


async def test_create_queue_returns_arq_when_configured(
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(queue={"backend": "arq"})

    queue = await create_queue(settings)

    assert isinstance(queue, ArqJobQueue)
    # Конструктор не подключается к Redis: падение Redis не должно ломать старт.
    await queue.close()


async def test_arq_queue_start_fails_fast_without_redis(
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(
        redis={"dsn": "redis://127.0.0.1:1/0", "required": False},
        queue={"backend": "arq"},
    )
    queue = await create_queue(settings)

    with pytest.raises(ConfigError, match="inline"):
        await queue.start()
