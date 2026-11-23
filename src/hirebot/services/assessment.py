"""Планировщик вопросов и решение по кандидату.

Два связанных механизма, которые нельзя разделять:

1. **Какие вопросы задать.** Лимит — не больше пяти, но пять — это максимум,
   а не цель. Вопрос имеет смысл только если его ответ **может изменить
   решение**. Это проверяется интервалом неопределённости: если даже при
   худшем ответе процент остаётся выше порога приглашения, спрашивать нечего.
2. **Что делать по итогу.** Три условия автоматического приглашения: процент
   выше порога, нет блокеров, по обязательным критериям есть данные. Иначе —
   человеку.

Отдельно — **shadow-режим**: профиль считается, решение записывается и
показывается, но кандидату ничего не отправляется. Это режим первого запуска:
сравниваем решения бота с решениями человека, не рискуя кандидатами.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hirebot.domain.enums import DecisionKind, ProfileStatus, StopReason
from hirebot.domain.scoring import (
    DecisionOutcome,
    PlannedQuestion,
    QuestionPlan,
    ScoringBreakdown,
    ScoringProfile,
    question_value,
)
from hirebot.logging_setup import get_logger
from hirebot.services.base import ServiceBase

logger = get_logger(__name__)

#: Сколько раз спрашивать «уточните», если ответ непонятен. После второго
#: непонятного ответа диалог уходит человеку: продолжать бессмысленно.
MAX_UNCLEAR_ROUNDS = 2


@dataclass(slots=True)
class AnswerRecord:
    """Ответ кандидата, привязанный к критерию."""

    criterion_key: str
    score: float
    evidence: str = ""
    text: str = ""
    is_unclear: bool = False
    contradicts_resume: bool = False


@dataclass(slots=True)
class PlanningState:
    """Состояние диалога, важное для планирования."""

    asked: list[str] = field(default_factory=list)
    answers: list[AnswerRecord] = field(default_factory=list)
    unclear_rounds: int = 0
    candidate_asked_to_stop: bool = False


class AssessmentService(ServiceBase):
    """Планирование вопросов и итоговое решение."""

    # ------------------------------------------------------------ вопросы

    def plan_questions(
        self,
        *,
        profile: ScoringProfile,
        breakdown: ScoringBreakdown,
        state: PlanningState | None = None,
    ) -> QuestionPlan:
        """Выбрать вопросы, которые стоит задать.

        Порядок проверок — от самого дешёвого отказа к самому дорогому:
        лимит → просьба кандидата → стоп-правила → стабильность решения →
        выбор по ценности.
        """
        state = state or PlanningState()
        limit = profile.thresholds.max_questions

        if state.candidate_asked_to_stop:
            return QuestionPlan(
                stop_reason=StopReason.CANDIDATE_ASKED.value,
                detail="Кандидат попросил не задавать вопросы — диалог уходит рекрутеру",
            )
        if len(state.asked) >= limit:
            return QuestionPlan(
                stop_reason=StopReason.LIMIT_REACHED.value,
                detail=f"Задано {len(state.asked)} из {limit} вопросов",
            )
        if state.unclear_rounds >= MAX_UNCLEAR_ROUNDS:
            return QuestionPlan(
                stop_reason=StopReason.UNCLEAR_TWICE.value,
                detail="Два непонятных ответа подряд — решает человек",
            )

        low, high = decision_interval(profile, breakdown)
        thresholds = profile.thresholds

        # Ответы уже не могут изменить решение — спрашивать незачем.
        if low >= thresholds.invite_at and not breakdown.has_blocker:
            return QuestionPlan(
                stop_reason=StopReason.DECISION_STABLE.value,
                detail=(
                    f"Даже при худших ответах процент не ниже {low:.0f}%: "
                    f"порог приглашения {thresholds.invite_at:.0f}%"
                ),
            )
        if high < thresholds.reject_at:
            return QuestionPlan(
                stop_reason=StopReason.DECISION_STABLE.value,
                detail=(
                    f"Даже при лучших ответах процент не выше {high:.0f}%: "
                    f"порог отказа {thresholds.reject_at:.0f}%"
                ),
            )
        if breakdown.has_blocker and not any(
            item.must_have for item in profile.criteria if item.key not in _answered_keys(state)
        ):
            return QuestionPlan(
                stop_reason=StopReason.BLOCKER_FOUND.value,
                detail="Обязательный критерий провален — приглашение невозможно",
            )

        candidates = self._candidate_questions(profile, breakdown, state)
        if not candidates:
            return QuestionPlan(
                stop_reason=StopReason.NO_QUESTIONS.value,
                detail="Не осталось вопросов по непокрытым критериям",
            )

        candidates.sort(key=lambda item: item.value, reverse=True)
        remaining = limit - len(state.asked)
        selected = candidates[:remaining]
        return QuestionPlan(
            questions=selected,
            stop_reason=StopReason.LIMIT_REACHED.value if len(selected) >= remaining else "",
            detail=f"Выбрано {len(selected)} из {len(candidates)} доступных вопросов",
        )

    def _candidate_questions(
        self,
        profile: ScoringProfile,
        breakdown: ScoringBreakdown,
        state: PlanningState,
    ) -> list[PlannedQuestion]:
        """Вопросы по критериям, где данных мало, отсортированные по ценности."""
        asked_texts = {text.strip().casefold() for text in state.asked}
        answered = _answered_keys(state)
        by_key = {item.key: item for item in breakdown.criteria}
        coverage_by_key = {item.key: (1.0 if item.evidence.strip() else 0.0) for item in breakdown.criteria}

        candidates: list[PlannedQuestion] = []
        for question in profile.questions:
            if question.text.strip().casefold() in asked_texts:
                continue
            criterion = profile.criterion(question.criterion_key)
            if criterion is None:
                continue
            # Критерий уже подтверждён ответом или резюме — вопрос не нужен.
            if question.criterion_key in answered:
                continue
            score = by_key.get(criterion.key)
            if score is not None and score.evidence.strip() and score.score >= 70.0:
                continue
            value = question_value(
                question,
                criterion,
                coverage=coverage_by_key.get(criterion.key, 0.0),
                weights=breakdown.weights,
            )
            candidates.append(
                PlannedQuestion(
                    question=question,
                    criterion_key=criterion.key,
                    value=value,
                    reason=(
                        f"критерий «{criterion.title}», "
                        f"{'обязательный, ' if criterion.must_have else ''}"
                        f"данных {'нет' if value > 0 else 'достаточно'}"
                    ),
                )
            )
        return candidates

    # ------------------------------------------------------------ решение

    def decide(self, *, profile: ScoringProfile, breakdown: ScoringBreakdown) -> DecisionOutcome:
        """Итоговое решение по проценту, уверенности и блокерам.

        В shadow-режиме решение **не отправляется** кандидату: профиль ещё не
        проверен на реальных данных, поэтому итогом всегда будет «человеку»,
        а рекомендация сохраняется отдельно.
        """
        thresholds = profile.thresholds
        decision = thresholds.decide(
            percent=breakdown.percent,
            coverage=breakdown.coverage,
            has_blocker=breakdown.has_blocker,
        )
        explanation = _explain(breakdown, thresholds, decision)

        if profile.status is ProfileStatus.SHADOW:
            return DecisionOutcome(
                decision=DecisionKind.HUMAN,
                percent=breakdown.percent,
                coverage=breakdown.coverage,
                blockers=breakdown.blockers,
                stop_reason="shadow",
                explanation=(
                    f"[shadow] рекомендация системы: {decision.value}. "
                    f"{explanation} Отправка выключена: профиль в режиме проверки."
                ),
            )

        return DecisionOutcome(
            decision=decision,
            percent=breakdown.percent,
            coverage=breakdown.coverage,
            blockers=breakdown.blockers,
            explanation=explanation,
        )


def decision_interval(profile: ScoringProfile, breakdown: ScoringBreakdown) -> tuple[float, float]:
    """Диапазон, в котором может оказаться процент после ответов.

    Нижняя граница — неизвестные критерии остались неподтверждёнными (0),
    верхняя — все они закрыты на максимум. Если интервал не пересекает пороги,
    дальнейшие вопросы не изменят решение.
    """
    weights = breakdown.weights
    known = 0.0
    unknown_weight = 0.0
    scored = {item.key: item for item in breakdown.criteria}
    for criterion in profile.criteria:
        share = weights.get(criterion.key, 0.0)
        score = scored.get(criterion.key)
        if score is not None and score.evidence.strip():
            known += share * score.score
        else:
            unknown_weight += share
    low = max(0.0, min(100.0, known))
    high = max(0.0, min(100.0, known + 100.0 * unknown_weight))
    return low, high


def _answered_keys(state: PlanningState) -> set[str]:
    return {item.criterion_key for item in state.answers}


def _explain(breakdown: ScoringBreakdown, thresholds: Any, decision: DecisionKind) -> str:
    """Человеческое объяснение решения — для карточки и лога."""
    parts = [f"соответствие {breakdown.percent:.0f}%", f"уверенность {breakdown.coverage:.0%}"]
    if breakdown.blockers:
        parts.append("провал обязательных: " + ", ".join(breakdown.blockers))
    if decision is DecisionKind.INVITE:
        parts.append(f"выше порога {thresholds.invite_at:.0f}%")
    elif decision is DecisionKind.REJECT:
        parts.append(f"ниже порога {thresholds.reject_at:.0f}%")
    elif decision is DecisionKind.HUMAN:
        if breakdown.coverage < thresholds.min_coverage_for_auto:
            parts.append(
                f"данных по обязательным мало (уверенность ниже {thresholds.min_coverage_for_auto:.0%})"
            )
        elif breakdown.has_blocker:
            parts.append("есть блокер — решение за человеком")
        else:
            parts.append("пограничный случай")
    return "; ".join(parts)


__all__ = [
    "MAX_UNCLEAR_ROUNDS",
    "AnswerRecord",
    "AssessmentService",
    "PlanningState",
    "decision_interval",
]
