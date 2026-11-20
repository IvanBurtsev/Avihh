"""Прогон эталонной выборки через настоящий harness скрининга.

Замер намеренно идёт не через прямой вызов ``LlmPort.screen_resume``, а через
``ScreeningService.screen``: именно сервис пересчитывает балл по весам,
применяет порог, проверяет must-have и политику. Если мерить «сырой» ответ
модели, можно получить красивые цифры у компонента, который в продукте не
принимает решение.

Точка расширения — ``container_factory``. По умолчанию собирается fake-контейнер
без сети и без ключей. Чтобы прогнать тот же набор на реальной модели, фабрика
должна вернуть контейнер с другим ``llm``, но с тем же диалогом, вакансией и
резюме. Проще всего вызвать :func:`build_case_container` и передать туда
нужный ``LlmPort``::

    async def factory(dataset: GoldenDataset, case: GoldenCase) -> Container:
        return await build_case_container(dataset, case, llm=my_real_llm)

    report = await run_evaluation(dataset, container_factory=factory)

Соглашение о диалоге: фабрика обязана создать диалог с внешним идентификатором
``case_conversation_external_id(case.id)``. Это позволяет harness найти диалог,
не зная внутренних UUID, и не навязывает фабрике способ хранения.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from hirebot.adapters.llm.fake import FakeLlmAdapter
from hirebot.config import Settings
from hirebot.container import Container
from hirebot.domain.entities import Candidate, Conversation, Resume, ScreeningResult, Vacancy, new_id
from hirebot.domain.enums import Channel, ConversationState, ScreeningVerdict
from hirebot.evaluation.dataset import GoldenCase, GoldenDataset
from hirebot.evaluation.metrics import CaseOutcome, EvaluationReport, binary_metrics
from hirebot.ports.llm import LlmPort
from hirebot.services.base import DEFAULT_COMPANY_ID
from hirebot.services.screening import ScreeningService
from hirebot.testing.fakes import build_fake_container

__all__ = [
    "ContainerFactory",
    "EvaluationError",
    "build_case_container",
    "case_conversation_external_id",
    "run_evaluation",
]

#: Канал, который использует замер. Скрининг от канала не зависит, но диалог
#: и резюме в домене к нему привязаны.
EVALUATION_CHANNEL = Channel.HH

#: Вердикты, которые означают автоматическое приглашение. ``NEEDS_REVIEW`` и
#: ``WEAK`` — это «решает человек», поэтому в бинарном решении они не приглашение.
_INVITE_VERDICTS = frozenset({ScreeningVerdict.STRONG_MATCH, ScreeningVerdict.MATCH})

ContainerFactory = Callable[[GoldenDataset, GoldenCase], Awaitable[Container]]


class EvaluationError(RuntimeError):
    """Ошибка организации замера (не ошибка скрининга конкретного резюме)."""


def case_conversation_external_id(case_id: str) -> str:
    """Внешний идентификатор диалога, по которому harness находит случай."""
    return f"evaluation:{case_id}"


async def run_evaluation(
    dataset: GoldenDataset,
    *,
    container_factory: ContainerFactory | None = None,
) -> EvaluationReport:
    """Прогнать все случаи выборки и вернуть отчёт с метриками.

    Каждый случай получает собственный контейнер: кэш скрининга и состояние
    репозиториев не должны протекать между кандидатами. Контейнер закрывается
    в ``finally``, чтобы прогон не оставлял открытых ресурсов.
    """
    factory: ContainerFactory = container_factory or _default_container_factory
    outcomes: list[CaseOutcome] = []
    model = "unknown"

    for case in dataset.cases:
        container = await factory(dataset, case)
        try:
            conversation_id = await _find_conversation_id(container, case)
            result = await ScreeningService(container).screen(conversation_id=conversation_id)
            model = _model_name(container)
            outcomes.append(_build_outcome(case, result))
        finally:
            await container.aclose()

    return EvaluationReport(
        dataset_version=dataset.version,
        model=model,
        outcomes=tuple(outcomes),
        decision_metrics=binary_metrics(
            predicted=[outcome.actual_should_invite for outcome in outcomes],
            expected=[outcome.expected_should_invite for outcome in outcomes],
        ),
    )


async def build_case_container(
    dataset: GoldenDataset,
    case: GoldenCase,
    *,
    llm: LlmPort | None = None,
    settings: Settings | None = None,
) -> Container:
    """Собрать fake-контейнер, подготовленный под один случай выборки.

    Вакансия берёт критерии из случая, резюме — текст из случая. Если передан
    ``llm``, он подменяет fake-провайдера: остальной harness (вакансия, диалог,
    резюме, пересчёт балла, политика) остаётся ровно тем же, что и в продукте.

    ``settings`` нужен, чтобы замер можно было провести с другим порогом
    ``policy.min_score_to_invite``: порог — часть проверяемой политики, а не
    константа harness.
    """
    container = build_fake_container(settings, seed_demo=False)
    if llm is not None:
        container.llm = llm

    async with container.uow_factory() as uow:
        vacancy = Vacancy(
            id=new_id(),
            channel=EVALUATION_CHANNEL,
            external_id=f"evaluation-vacancy:{dataset.version}",
            title=dataset.vacancy_title,
            company_id=DEFAULT_COMPANY_ID,
            description=dataset.vacancy_description,
            criteria=[dict(criterion) for criterion in case.criteria],
        )
        await uow.vacancies.upsert(vacancy)

        candidate = Candidate(
            id=new_id(),
            channel=EVALUATION_CHANNEL,
            external_id=f"evaluation-candidate:{case.id}",
            full_name=case.title,
            company_id=DEFAULT_COMPANY_ID,
        )
        await uow.candidates.upsert(candidate)

        conversation = Conversation(
            id=new_id(),
            company_id=DEFAULT_COMPANY_ID,
            candidate_id=candidate.id,
            vacancy_id=vacancy.id,
            channel=EVALUATION_CHANNEL,
            external_id=case_conversation_external_id(case.id),
            state=ConversationState.RESUME_FETCHED,
        )
        await uow.conversations.upsert(conversation)

        if case.resume_text.strip():
            resume = Resume(
                id=new_id(),
                candidate_id=candidate.id,
                channel=EVALUATION_CHANNEL,
                external_id=f"evaluation-resume:{case.id}",
                raw_text=case.resume_text,
            )
            await uow.resumes.save(resume)

        await uow.commit()

    return container


async def _default_container_factory(dataset: GoldenDataset, case: GoldenCase) -> Container:
    """Контейнер замера на **продуктовой** fake-модели (``provider=fake``).

    ``build_fake_container`` собирает тестовую заглушку из ``hirebot.testing``,
    а у неё своя, исторически разошедшаяся эвристика. Замер должен мерить тот
    провайдер, который реально включается настройкой ``llm.provider=fake``,
    иначе цифры в ``docs/EVALUATION.md`` относятся к коду, которого нет в
    продуктовой сборке. Поэтому по умолчанию подставляем адаптер из
    ``hirebot.adapters.llm.fake``; тестовая заглушка остаётся для тестов
    сервисов, а подмена реальной LLM — через ``container_factory``.
    """
    return await build_case_container(dataset, case, llm=FakeLlmAdapter())


async def _find_conversation_id(container: Container, case: GoldenCase) -> UUID:
    external_id = case_conversation_external_id(case.id)
    async with container.uow_factory() as uow:
        conversation = await uow.conversations.get_by_external(EVALUATION_CHANNEL, external_id)
    if conversation is None:
        msg = (
            f"container_factory не подготовил диалог с внешним id {external_id!r}. "
            "Вызовите build_case_container(...) или создайте диалог с этим id."
        )
        raise EvaluationError(msg)
    return conversation.id


def _build_outcome(case: GoldenCase, result: ScreeningResult) -> CaseOutcome:
    return CaseOutcome(
        case_id=case.id,
        title=case.title,
        expected_verdict=case.expected_verdict,
        actual_verdict=result.verdict,
        expected_should_invite=case.expected_should_invite,
        actual_should_invite=result.verdict in _INVITE_VERDICTS,
        score=result.total_score,
        must_have_gaps=tuple(criterion.key for criterion in result.must_have_failures),
        expected_must_have_gaps=case.expected_must_have_gaps,
        notes=case.notes,
    )


def _model_name(container: Container) -> str:
    """Имя провайдера для отчёта.

    ``ScreeningDraft`` не содержит поля ``model``, а ``ScreeningResult.model``
    берётся из настроек и для fake-провайдера показывал бы имя боевой модели из
    конфига. Поэтому имя берём у самого адаптера, если он его объявляет.
    """
    adapter_name = getattr(container.llm, "model_name", None)
    if isinstance(adapter_name, str) and adapter_name:
        return adapter_name
    provider = container.settings.llm.provider
    return f"{provider}/{container.settings.llm.model}"
