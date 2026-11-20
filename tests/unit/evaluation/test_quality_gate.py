"""Quality-gate: пустой и почти пустой текст резюме уходит человеку без модели.

Это одновременно и экономия денег, и защита от «изобретённого» балла: если
оценивать нечего, балл 0 или 15 — это не решение, а шум.
"""

from __future__ import annotations

from hirebot.adapters.llm.fake import FakeLlmAdapter
from hirebot.container import Container
from hirebot.domain.enums import ScreeningVerdict
from hirebot.evaluation import GoldenCase, GoldenDataset, build_case_container, run_evaluation
from hirebot.evaluation.runner import EVALUATION_CHANNEL, case_conversation_external_id
from hirebot.services.screening import QUALITY_GATE_MODEL, ScreeningService

#: Случаи, которые обязаны быть отсечены до вызова модели.
GATED = frozenset({"practically-empty-resume", "short-resume", "almost-empty-resume"})


async def test_quality_gate_skips_model_for_empty_resumes(dataset: GoldenDataset) -> None:
    """Счётчик вызовов fake-модели для пустых резюме должен остаться нулевым."""
    calls: dict[str, FakeLlmAdapter] = {}

    async def factory(current: GoldenDataset, case: GoldenCase) -> Container:
        llm = FakeLlmAdapter()
        calls[case.id] = llm
        return await build_case_container(current, case, llm=llm)

    report = await run_evaluation(dataset, container_factory=factory)
    outcomes = {outcome.case_id: outcome for outcome in report.outcomes}

    for case_id in GATED:
        assert outcomes[case_id].actual_verdict is ScreeningVerdict.NEEDS_REVIEW
        assert outcomes[case_id].actual_should_invite is False
        assert outcomes[case_id].score == 0.0
        assert calls[case_id].screen_calls == [], f"{case_id}: модель вызывалась на мусоре"
        # Пустое резюме не подтверждает ни один обязательный критерий.
        assert set(outcomes[case_id].must_have_gaps) == {"python", "sql"}

    for case_id, llm in calls.items():
        if case_id not in GATED:
            assert len(llm.screen_calls) == 1, f"{case_id}: модель должна вызываться один раз"


async def test_quality_gate_explains_reason_and_records_provenance(dataset: GoldenDataset) -> None:
    """Причина должна быть понятна человеку, а в аудите — видно, что модели не было."""
    case = next(case for case in dataset.cases if case.id == "short-resume")
    llm = FakeLlmAdapter()
    container = await build_case_container(dataset, case, llm=llm)
    try:
        async with container.uow_factory() as uow:
            conversation = await uow.conversations.get_by_external(
                EVALUATION_CHANNEL,
                case_conversation_external_id(case.id),
            )
        assert conversation is not None
        result = await ScreeningService(container).screen(conversation_id=conversation.id)
    finally:
        await container.aclose()

    assert result.verdict is ScreeningVerdict.NEEDS_REVIEW
    assert "слишком короткое" in result.summary
    assert str(60) in result.summary
    assert result.model == QUALITY_GATE_MODEL
    assert llm.screen_calls == []
