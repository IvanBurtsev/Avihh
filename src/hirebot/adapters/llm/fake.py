"""Детерминированная LLM-заглушка (без сети).

Зачем: демо-прогон и тесты должны проходить **без ключей и без интернета**, но
выглядеть осмысленно. Поэтому заглушка не возвращает «заглушечный» текст, а
действительно разбирает резюме:

* считает стаж только из конструкций опыта и закрытых периодов работы — возраст
  («34 года») и год рождения («1989 г.р.») стажем не считаются;
* различает коммерческий опыт, учебный (курсы, вуз, преподавание, pet-проекты) и
  простое упоминание навыка; «знаком с SQL» не закрывает критерий;
* привязывает годы к конкретному навыку, если связка есть в тексте
  («Python (5 years)», «5 лет коммерческой разработки на Python»);
* находит навыки в английских и смешанных формах, с допуском на опечатки и
  разрядку («PostgreSQL databases», «Postgre SQL», «Fast Api», «Pythom»);
* игнорирует пол, возраст и другие защищённые признаки;
* вытаскивает ключевые навыки из словаря синонимов;
* определяет грейд по опыту и маркерам («senior», «руководитель», «стажёр»);
* оценивает каждый критерий вакансии и приводит цитату-подтверждение
  (``evidence``) — как и настоящая модель, без цитаты критерий не зачитывается;
* пишет человеческие сообщения по шаблонам, не выдумывая фактов о вакансии.

Детерминированность обязательна: одинаковый вход даёт одинаковый выход, иначе
``content_hash``-кэш и тесты теряют смысл. Никакого ``random`` и ``datetime.now``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hirebot.adapters.llm.prompts import PROMPT_VERSION
from hirebot.adapters.llm.redaction import extract_contacts
from hirebot.domain.entities import CandidateReply, ScreeningCriterion
from hirebot.domain.enums import CandidateIntent, MessageKind
from hirebot.logging_setup import get_logger
from hirebot.ports.llm import (
    AnswerAssessment,
    AnswerAssessmentRequest,
    LlmPort,
    MessageDraft,
    MessageRequest,
    ReplyAnalysisRequest,
    ScreeningDraft,
    ScreeningRequest,
)

__all__ = ["FakeLlmAdapter"]

logger = get_logger(__name__)

_MODEL_NAME = "fake-llm-v2"

#: Порог «критерий не подтверждён» внутри заглушки. Совпадает по смыслу с
#: ``MUST_HAVE_GAP_THRESHOLD`` сервиса, но заглушка обязана сама честно ставить
#: ``is_gap``: сервис не должен угадывать пробел по низкому баллу.
_GAP_THRESHOLD = 55.0

#: Оценки по типу доказательства. Числа подобраны под правило «упоминание не
#: равно опыту»: коммерческое подтверждение закрывает критерий с запасом,
#: учебный опыт и «голое» перечисление технологий — нет.
_SCORE_NOT_CONFIRMED = 15.0
_SCORE_MENTION = 35.0
_SCORE_ACADEMIC_WITH_YEARS = 45.0
_SCORE_ACADEMIC = 65.0
_SCORE_COMMERCIAL = 92.0

#: База оценки критерия со стажем: требуемые годы закрыты.
_SCORE_YEARS_BASE = 92.0

#: Канонические навыки и варианты их записи (включая английские формы).
#: Проверка идёт по вхождению, уплотнённой записи («Fast Api» -> «fastapi») и
#: с допуском на опечатки, а не по точному совпадению слова.
_SKILLS: Mapping[str, tuple[str, ...]] = {
    "python": ("python", "питон", "пайтон", "pandas", "numpy", "django", "fastapi", "flask", "asyncio"),
    "sql": (
        "sql",
        "postgres",
        "postgresql",
        "postgre",
        "mysql",
        "sqlite",
        "субд",
        "database",
        "базы данных",
        "постгрес",
        "посгрес",
        "plpgsql",
    ),
    "api": ("rest api", "api", "микросервис", "microservice"),
    "docker": ("docker", "kubernetes", "k8s", "контейнер", "container", "ci/cd"),
    "english": ("english", "английск"),
}

#: Регулярные выражения для связки «годы <-> навык» в одной строке.
_SKILL_LINK_PATTERNS: Mapping[str, str] = {
    "python": r"(?:python|питон|пайтон)",
    "sql": r"(?:\bsql\b|postgres(?:ql)?|postgre|mysql|sqlite|субд|постгрес\w*|посгрес\w*)",
    "api": r"(?:rest[\s-]*api|api|микросервис\w*)",
    "docker": r"(?:docker|kubernetes|k8s|контейнер\w*|container\w*)",
}

_API_RE = re.compile(
    r"rest[\s-]*api|(?<![a-z])api(?![a-z])|микросервис|microservice|fastapi|django|flask",
    re.IGNORECASE,
)
_ENGLISH_RE = re.compile(r"english|английск", re.IGNORECASE)
_CEFR_RE = re.compile(r"\b(c2|c1|b2|b1|a2|a1)\b", re.IGNORECASE)
_COMPACT_RE = re.compile(r"[^a-zа-яё0-9+#]+")
_LATIN_TOKEN_RE = re.compile(r"[a-z][a-z0-9+#]{3,}")

#: Визуально похожие кириллические буквы: нужны только для распознавания
#: смешанных записей вроде «Pуthon», на отображение текста не влияют.
_HOMOGLYPHS = str.maketrans(
    {
        "а": "a",
        "в": "b",
        "е": "e",
        "к": "k",
        "м": "m",
        "н": "h",
        "о": "o",
        "р": "p",
        "с": "c",
        "т": "t",
        "у": "y",
        "х": "x",
    }
)

#: Учебный/некоммерческий контекст. Такие строки не закрывают критерий по
#: коммерческому стажу, даже если в них есть название технологии и слово «лет».
_ACADEMIC_MARKERS: tuple[str, ...] = (
    "курс",
    "учебн",
    "преподав",
    "школ",
    "универ",
    "образован",
    "стажир",
    "стажёр",
    "тренинг",
    "pet-проект",
    "pet проект",
    "домашн",
    "самоуч",
    "course",
    "university",
    "school",
    "student",
    "tutorial",
    "learning",
    "training",
    "bootcamp",
)
_ACADEMIC_INTERN_RE = re.compile(r"\bintern(ship)?\b", re.IGNORECASE)

#: Признаки реальной работы (в том числе английские глаголы и обороты).
_COMMERCIAL_MARKERS: tuple[str, ...] = (
    "опыт работы",
    "коммерческ",
    "работал",
    "работала",
    "работаю",
    "разрабатывал",
    "разрабатывала",
    "проектировал",
    "проектировала",
    "писал",
    "писала",
    "запускал",
    "запускала",
    "настраивал",
    "настраивала",
    "собирал",
    "собирала",
    "внедрял",
    "внедряла",
    "поддерживал",
    "поддерживала",
    "разработчик",
    "backend",
    "бэкенд",
    "developer",
    "engineer",
    "built",
    "maintained",
    "designed",
    "developed",
    "worked",
    "packaged",
    "deployed",
    "configured",
    "implemented",
    "using",
    "использ",
    "для ",
    "for ",
)

#: Уровень CEFR -> оценка критерия. Порог B1 задан ожиданием вакансии.
_ENGLISH_SCORES: Mapping[str, float] = {
    "c2": 95.0,
    "c1": 90.0,
    "b2": 85.0,
    "b1": 80.0,
    "a2": 40.0,
    "a1": 35.0,
}
_CEFR_RANK: Mapping[str, int] = {"a1": 1, "a2": 2, "b1": 3, "b2": 4, "c1": 5, "c2": 6}
_BELOW_REQUIRED_ENGLISH_SCORE = 40.0
_ENGLISH_WITHOUT_LEVEL_SCORE = 55.0

_SENIOR_MARKERS = ("senior", "ведущий", "руководител", "head", "team lead", "тимлид", "директор")
_JUNIOR_MARKERS = ("junior", "стажер", "стажёр", "начинающ", "intern", "без опыта")

_YEARS_RE = re.compile(r"(\d{1,2})\s*(?:год|года|лет|years?|yrs?)", re.IGNORECASE)
#: «Опыт работы: 6 лет», «общий стаж — 10 лет». Оборот требует слова «опыт/стаж»,
#: поэтому «34 года» само по себе стажем не становится.
_EXPERIENCE_AFTER_RE = re.compile(
    r"(?:опыт|стаж)[^.\n]{0,30}?(\d{1,2})\s*(?:год|года|лет|years?)",
    re.IGNORECASE,
)
#: «6 лет опыта», «6 years of commercial software development».
_EXPERIENCE_BEFORE_RE = re.compile(
    r"(\d{1,2})\s*(?:год|года|лет|years?)\s+"
    r"(?:опыта|стажа|работы|коммерческ\w*|"
    r"(?:of\s+)?(?:commercial\s+)?(?:software\s+)?(?:development|experience))",
    re.IGNORECASE,
)
#: Закрытые периоды работы: «2018 — 2024», «2019.05 — 2024.02».
_PERIOD_RE = re.compile(
    r"((?:19|20)\d{2}(?:[./]\d{1,2})?)\s*(?:[—–-]|по|to|until)\s*"
    r"((?:19|20)\d{2}(?:[./]\d{1,2})?)",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9+#.]{4,}")

_NOT_INTERESTED = (
    "не интерес",
    "неинтерес",
    "отказ",
    "не подходит",
    "не актуально",
    "уже нашел",
    "уже нашёл",
    "уже трудоустро",
    "передумал",
    "снял",
)
_RESCHEDULE = ("перенес", "другое время", "другой день", "не смогу", "не удобно", "другие слоты")
_QUESTION = ("?", "вопрос", "подскажите", "расскажите", "уточните", "а сколько", "а как", "а что")
_INTERESTED = ("интерес", "готов", "согласен", "согласна", "давайте", "подходит", "да,", "конечно")
_ORDINALS = {
    "перв": 1,
    "втор": 2,
    "трет": 3,
    "четверт": 4,
    "пят": 5,
}


class FakeLlmAdapter(LlmPort):
    """Заглушка LLM: детерминированная, без сети.

    Модель «читает» резюме общими правилами, а не словарём под конкретные
    случаи выборки:

    * стаж берётся только из конструкций опыта («опыт работы: 5 лет»,
      «6 years of experience», закрытые периоды), а не из возраста и года рождения;
    * годы привязываются к навыку, если связка есть в тексте («Python (5 years)»);
      иначе общий стаж применяется только при коммерческом подтверждении навыка;
    * учебные проекты, курсы, преподавание и «знаком с ...» не считаются
      коммерческой разработкой;
    * навыки ищутся и в английских/смешанных формах, с допуском на опечатки и
      разрядку («Postgre SQL», «Fast Api», «PostgreSQL databases»);
    * защищённые признаки (пол, возраст, гражданство и т.п.) не участвуют в оценке.
    """

    model_name = _MODEL_NAME

    def __init__(self, *, prompt_version: str = PROMPT_VERSION) -> None:
        self._prompt_version = prompt_version
        #: Журнал вызовов: тесты проверяют, что quality-gate не тратит вызов модели.
        self.screen_calls: list[ScreeningRequest] = []
        self.compose_calls: list[MessageRequest] = []
        self.reply_calls: list[ReplyAnalysisRequest] = []
        self.answer_calls: list[AnswerAssessmentRequest] = []

    async def screen_resume(self, request: ScreeningRequest) -> ScreeningDraft:
        """Оценить резюме по критериям вакансии детерминированным алгоритмом."""
        self.screen_calls.append(request)
        text = request.resume_text or ""
        lowered = text.casefold()
        lines = _text_lines(text)
        total_years = _extract_total_years(text)
        months = total_years * 12
        skills = _extract_skills(lines)
        grade = _detect_grade(lowered, months)
        criteria = [
            _score_criterion(
                criterion,
                lines=lines,
                total_years=total_years,
            )
            for criterion in request.criteria
        ]
        total = _weighted_total(criteria)
        gaps = [f"Не подтверждено в резюме: {item.title}" for item in criteria if item.is_gap]
        strengths = [
            f"{item.title}: подтверждено («{item.evidence[:80]}»)"
            for item in criteria
            if not item.is_gap and item.score >= 70.0 and item.evidence
        ]
        questions = _questions_for(criteria)
        return ScreeningDraft(
            total_score=round(total, 1),
            summary=_summary(months, grade, skills, criteria),
            criteria=criteria,
            gaps=tuple(gaps[:5]),
            strengths=tuple(strengths[:5]),
            questions=tuple(questions),
            model=_MODEL_NAME,
        )

    async def compose_message(self, request: MessageRequest) -> MessageDraft:
        """Сгенерировать сообщение по шаблону (без выдуманных фактов о вакансии)."""
        self.compose_calls.append(request)
        text = _render_message(request)
        return MessageDraft(
            text=text,
            model=_MODEL_NAME,
            prompt_version=self._prompt_version,
            cached=False,
        )

    async def assess_answer(self, request: AnswerAssessmentRequest) -> AnswerAssessment:
        """Оценить ответ по критерию без сети.

        Правила намеренно простые и объяснимые: заглушка не должна изображать
        понимание, которого у неё нет. Она проверяет три вещи — уклончивость,
        наличие ключевых слов критерия и объём ответа.
        """
        self.answer_calls.append(request)
        text = (request.answer or "").strip()
        lowered = text.casefold()

        unclear_markers = (
            "не знаю",
            "не помню",
            "не уверен",
            "затрудняюсь",
            "без комментариев",
            "не хочу",
            "пропустить",
            "skip",
            "idk",
            "-",
            "—",
        )
        if len(text) < 3 or any(marker in lowered for marker in unclear_markers):
            return AnswerAssessment(score=0.0, evidence="", is_unclear=True)

        expected = _answer_tokens(request.expectation, request.evidence_hint)
        actual = _answer_tokens(text)
        matched = expected & actual
        if not expected:
            # Нечего сравнивать: ответ содержательный, но оценивать не по чему.
            return AnswerAssessment(score=50.0, evidence=text[:200], is_unclear=False)

        share = len(matched) / len(expected)
        score = round(min(100.0, 30.0 + share * 70.0), 1)
        return AnswerAssessment(
            score=score,
            evidence=text[:200],
            is_unclear=False,
            contradicts_resume=False,
        )

    async def analyze_reply(self, request: ReplyAnalysisRequest) -> CandidateReply:
        """Разобрать ответ кандидата: намерение, выбранный слот, контакты."""
        self.reply_calls.append(request)
        text = request.reply_text or ""
        lowered = text.casefold()
        contacts = extract_contacts(text)
        chosen = _detect_slot_choice(lowered, len(request.proposed_slots))
        intent = _detect_intent(lowered, chosen)
        confidence = _confidence(intent, chosen)
        extracted: dict[str, Any] = dict(contacts)
        if chosen is not None:
            extracted["chosen_slot"] = chosen
        return CandidateReply(
            intent=intent,
            summary=_reply_summary(text),
            extracted=extracted,
            chosen_slot_index=chosen,
            confidence=confidence,
            raw_text=text,
        )


# --------------------------------------------------------------------- скрининг


def _text_lines(text: str) -> list[str]:
    """Непустые строки резюме: evidence всегда берётся из конкретной строки."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def _extract_total_years(text: str) -> int:
    """Общий стаж в годах — только из конструкций опыта, а не из возраста.

    Источники: «опыт работы: 5 лет», «5 лет опыта», «6 years of experience»,
    суммы закрытых периодов («2018 — 2024»). Возраст («34 года»), год рождения
    («1989 г.р.») и дата в вакууме стажем не считаются. Если найдено несколько
    оценок, берём максимальную, а не сумму: иначе один и тот же стаж посчитается
    дважды (явно указанный и по периодам).
    """
    lowered = text.casefold()
    explicit = [
        int(value)
        for pattern in (_EXPERIENCE_AFTER_RE, _EXPERIENCE_BEFORE_RE)
        for value in pattern.findall(lowered)
    ]
    candidates = [value for value in explicit if 0 < value <= 45]
    period_years = _closed_periods_years(text)
    if period_years:
        candidates.append(period_years)
    return max(candidates) if candidates else 0


def _closed_periods_years(text: str) -> int:
    """Сумма закрытых периодов работы в годах (открытые периоды не считаем)."""
    months = 0
    for match in _PERIOD_RE.finditer(text):
        start = _period_months(match.group(1))
        end = _period_months(match.group(2))
        if start is not None and end is not None and end > start:
            months += end - start
    return min(months // 12, 45)


def _period_months(value: str) -> int | None:
    normalized = value.replace("/", ".")
    if "." in normalized:
        year, month = normalized.split(".", 1)
        if year.isdigit() and month.isdigit() and 1 <= int(month) <= 12:
            return int(year) * 12 + int(month)
        return None
    return int(value) * 12 if value.isdigit() else None


def _extract_skills(lines: Sequence[str]) -> list[str]:
    """Навыки, реально встречающиеся в резюме (включая английские формы)."""
    return [skill for skill in _SKILLS if any(_contains_skill(line, skill) for line in lines)]


def _contains_skill(line: str, skill: str) -> bool:
    """Есть ли в строке навык — с учётом языка, разрядки и опечаток."""
    lowered = line.casefold()
    if skill == "english":
        return _ENGLISH_RE.search(lowered) is not None
    if skill == "api":
        return _API_RE.search(lowered) is not None
    compact = _COMPACT_RE.sub("", lowered)
    variants = _SKILLS[skill]
    for variant in variants:
        if variant in lowered:
            return True
        squashed = variant.replace(" ", "")
        if len(squashed) >= 4 and squashed in compact:
            # «Fast Api» / «Postgre SQL» -> «fastapi» / «postgresql».
            return True
    return _fuzzy_skill_match(lowered, variants)


def _fuzzy_skill_match(lowered: str, variants: Sequence[str]) -> bool:
    """Допуск на опечатки: «Pythom», «Pyhton», «Pуthon» -> python."""
    latinized = lowered.translate(_HOMOGLYPHS)
    tokens = _LATIN_TOKEN_RE.findall(latinized)
    return any(
        len(variant) >= 5
        and variant.isascii()
        and any(
            abs(len(token) - len(variant)) <= 2 and _edit_distance(token, variant) <= 2 for token in tokens
        )
        for variant in variants
    )


def _edit_distance(left: str, right: str, limit: int = 2) -> int:
    """Расстояние Левенштейна с ранним выходом за пределом ``limit``."""
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        for column, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            current.append(min(previous[column] + 1, current[column - 1] + 1, previous[column - 1] + cost))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _detect_grade(lowered: str, months: int) -> str:
    if any(marker in lowered for marker in _SENIOR_MARKERS) or months >= 72:
        return "senior"
    if any(marker in lowered for marker in _JUNIOR_MARKERS) or months < 12:
        return "junior"
    if months >= 36:
        return "middle+"
    return "middle"


def _tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(value.casefold()):
        if raw not in tokens:
            tokens.append(raw)
    return tokens[:12]


def _years_required(expectation: str) -> float | None:
    match = _YEARS_RE.search(expectation)
    if match is None:
        return None
    return float(match.group(1))


def _linked_years(line: str, skill: str) -> int:
    """Годы, явно привязанные к навыку в этой строке («Python (5 years)»)."""
    pattern = _SKILL_LINK_PATTERNS.get(skill)
    if pattern is None:
        return 0
    lowered = line.casefold()
    after = re.search(rf"{pattern}[^\n.,]{{0,20}}?(\d{{1,2}})\s*(?:год|года|лет|years?)", lowered)
    if after is not None:
        return min(int(after.group(1)), 45)
    before = re.search(
        rf"(\d{{1,2}})\s*(?:год|года|лет|years?)[^\n.,]{{0,45}}?{pattern}",
        lowered,
    )
    if before is not None:
        return min(int(before.group(1)), 45)
    return 0


def _is_academic(lowered: str) -> bool:
    if any(marker in lowered for marker in _ACADEMIC_MARKERS):
        return True
    return _ACADEMIC_INTERN_RE.search(lowered) is not None


def _classify_line(line: str, linked_years: int) -> str:
    """Тип доказательства: commercial / academic / mention.

    Учебный контекст проверяется первым: «курсы Python-разработки» — это не
    коммерческая разработка, хотя в строке есть слово «разработки».
    """
    lowered = line.casefold()
    if _is_academic(lowered):
        return "academic"
    if linked_years > 0:
        # Явная связка «5 лет Python» — сильное доказательство стажа по навыку.
        return "commercial"
    if any(marker in lowered for marker in _COMMERCIAL_MARKERS):
        return "commercial"
    return "mention"


@dataclass(slots=True, frozen=True)
class _SkillEvidence:
    """Найденное в резюме доказательство по навыку и его качество."""

    line: str
    kind: str
    years: int


def _collect_skill_evidence(lines: Sequence[str], skill: str) -> _SkillEvidence | None:
    """Выбрать лучшее доказательство: коммерческое важнее учебного и упоминания."""
    rank = {"mention": 0, "academic": 1, "commercial": 2}
    best: _SkillEvidence | None = None
    for line in lines:
        if not _contains_skill(line, skill):
            continue
        years = _linked_years(line, skill)
        kind = _classify_line(line, years)
        candidate = _SkillEvidence(line=line, kind=kind, years=years)
        if (
            best is None
            or rank[kind] > rank[best.kind]
            or (rank[kind] == rank[best.kind] and years > best.years)
        ):
            best = candidate
    return best


def _score_criterion(
    criterion: Mapping[str, Any],
    *,
    lines: Sequence[str],
    total_years: int,
) -> ScreeningCriterion:
    """Оценка критерия: смысловое доказательство, а не совпадение слова."""
    key = str(criterion.get("key") or criterion.get("title") or "criterion")
    title = str(criterion.get("title") or key)
    weight = _to_float(criterion.get("weight"), default=1.0)
    must_have = bool(criterion.get("must_have", False))
    expectation = str(criterion.get("expectation") or title)
    skill = _infer_skill(key, title, expectation)
    if skill == "english":
        score, evidence = _score_english(expectation, lines)
    elif skill is None:
        score, evidence = _score_by_expectation_tokens(expectation, lines)
    else:
        score, evidence = _score_skill(skill, expectation, lines, total_years)
    score = _clamp(score, 0.0, 100.0)
    return ScreeningCriterion(
        key=key,
        title=title,
        weight=weight,
        score=round(score, 1),
        must_have=must_have,
        evidence=evidence[:200],
        is_gap=score < _GAP_THRESHOLD,
    )


def _infer_skill(key: str, title: str, expectation: str) -> str | None:
    """Определить, какой навык проверяет критерий (по ключу и формулировке)."""
    text = f"{key} {title} {expectation}".casefold()
    if "python" in text or "питон" in text:
        return "python"
    if re.search(r"sql|postgres|субд|баз[аы]?\s+данных|database", text):
        return "sql"
    if "docker" in text or "контейнер" in text:
        return "docker"
    if re.search(r"\brest\b|\bapi\b|микросервис", text):
        return "api"
    if re.search(r"англ|english", text):
        return "english"
    return None


def _score_skill(
    skill: str,
    expectation: str,
    lines: Sequence[str],
    total_years: int,
) -> tuple[float, str]:
    """Оценка по навыку: коммерческий опыт, учебный опыт и упоминание различаются."""
    evidence = _collect_skill_evidence(lines, skill)
    if evidence is None:
        return _SCORE_NOT_CONFIRMED, ""
    required = _years_required(expectation)
    if required is None or required <= 0:
        if evidence.kind == "commercial":
            return _SCORE_COMMERCIAL, evidence.line
        if evidence.kind == "academic":
            return _SCORE_ACADEMIC, evidence.line
        return _SCORE_MENTION, evidence.line

    # Критерий требует коммерческого стажа: учебные проекты и курсы его не дают.
    if evidence.kind == "academic":
        return _SCORE_ACADEMIC_WITH_YEARS, evidence.line
    if evidence.kind == "mention":
        return _SCORE_MENTION, evidence.line
    years = evidence.years or total_years
    if years <= 0:
        # Работа упомянута, но длительность не подтверждена — осторожная оценка.
        return _SCORE_ACADEMIC_WITH_YEARS, evidence.line
    if years >= required:
        return min(_SCORE_YEARS_BASE + 2.0 * (years - required), 100.0), evidence.line
    # «Не менее N лет» — порог, а не пропорция: недобор остаётся пробелом.
    return 30.0 + 15.0 * (years / required), evidence.line


def _cefr_level(text: str) -> str | None:
    match = _CEFR_RE.search(text)
    return match.group(1).casefold() if match is not None else None


def _score_english(expectation: str, lines: Sequence[str]) -> tuple[float, str]:
    """Оценка английского по уровню CEFR, а не по слову «английский»."""
    evidence_line = next((line for line in lines if _ENGLISH_RE.search(line)), None)
    if evidence_line is None:
        return _SCORE_NOT_CONFIRMED, ""
    required = _cefr_level(expectation) or "b1"
    found = _cefr_level(evidence_line)
    if found is None:
        return _ENGLISH_WITHOUT_LEVEL_SCORE, evidence_line
    if _CEFR_RANK[found] >= _CEFR_RANK[required]:
        return _ENGLISH_SCORES[found], evidence_line
    return _BELOW_REQUIRED_ENGLISH_SCORE, evidence_line


def _score_by_expectation_tokens(expectation: str, lines: Sequence[str]) -> tuple[float, str]:
    """Запасной путь для критериев без распознанного навыка: покрытие слов."""
    stopwords = {
        "менее",
        "более",
        "опыт",
        "опыта",
        "работы",
        "года",
        "лет",
        "язык",
        "уровень",
        "знание",
        "навык",
        "умение",
    }
    tokens = [token for token in _tokens(expectation) if token not in stopwords]
    matched = [token for token in tokens if any(token in line.casefold() for line in lines)]
    if not matched:
        return _SCORE_NOT_CONFIRMED, ""
    evidence = _first_line_with(lines, matched)
    return min(90.0, 45.0 + 15.0 * len(matched)), evidence


def _first_line_with(lines: Sequence[str], tokens: Sequence[str]) -> str:
    for line in lines:
        lowered = line.casefold()
        if any(token in lowered for token in tokens):
            return line
    return ""


def _weighted_total(criteria: Sequence[ScreeningCriterion]) -> float:
    if not criteria:
        return 0.0
    weight_sum = sum(item.weight for item in criteria) or float(len(criteria))
    return sum(item.score * item.weight for item in criteria) / weight_sum


def _summary(months: int, grade: str, skills: Sequence[str], criteria: Sequence[ScreeningCriterion]) -> str:
    years = months // 12
    rest_months = months % 12
    experience = f"{years} г. {rest_months} мес." if years else f"{rest_months} мес."
    skills_text = ", ".join(skills[:6]) if skills else "ключевые навыки в резюме не выделены"
    confirmed = sum(1 for item in criteria if not item.is_gap)
    return (
        f"Опыт около {experience}, грейд: {grade}. "
        f"Подтверждённые навыки: {skills_text}. "
        f"По критериям вакансии подтверждено {confirmed} из {len(criteria) or 1}."
    )


def _questions_for(criteria: Sequence[ScreeningCriterion]) -> list[str]:
    """Уточняющие вопросы кандидату — только по его опыту, без фактов о вакансии."""
    questions: list[str] = []
    for item in criteria:
        if item.is_gap:
            questions.append(
                f"Расскажите, пожалуйста, подробнее про опыт по направлению «{item.title}»: "
                "какие задачи решали и какими инструментами пользовались?"
            )
        if len(questions) >= 4:
            break
    if not questions:
        questions.append(
            "Расскажите, пожалуйста, о самом интересном проекте за последние годы: ваша роль и результат?"
        )
    return questions[:4]


def _to_float(value: object, *, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------- сообщения


def _render_message(request: MessageRequest) -> str:
    """Собрать текст сообщения по шаблону, обрезав по ``max_chars``."""
    name = (request.candidate_name or "").strip().split(" ")[0] or "кандидат"
    vacancy = request.vacancy_title or "вакансию"
    company_suffix = f" в компании {request.company_name}" if request.company_name else ""
    context = request.context or {}
    questions = _as_lines(context.get("questions"))
    slots = _as_lines(context.get("slots"))
    reason = str(context.get("reason") or "").strip()

    kind = request.kind
    if kind == MessageKind.GREETING:
        body = (
            f"Здравствуйте, {name}!\n\n"
            f"Спасибо за отклик на вакансию «{vacancy}»{company_suffix}. "
            "Резюме получили, изучаем его. Если по опыту всё совпадает, "
            "предложим удобное время для разговора."
        )
    elif kind == MessageKind.CLARIFYING_QUESTION:
        bullet = "\n".join(f"{index}. {text}" for index, text in enumerate(questions, start=1))
        body = (
            f"Здравствуйте, {name}!\n\n"
            f"Спасибо за отклик на вакансию «{vacancy}»{company_suffix}. "
            "Чтобы понять, насколько совпадает опыт, уточните, пожалуйста:\n"
            f"{bullet or '1. Расскажите подробнее о последнем месте работы.'}"
        )
    elif kind == MessageKind.INVITATION:
        body = (
            f"Здравствуйте, {name}!\n\n"
            f"По итогам знакомства с резюме хотим пригласить вас на собеседование "
            f"по вакансии «{vacancy}»{company_suffix}. "
            "Подберём удобное время: напишите, пожалуйста, когда вам комфортно."
        )
    elif kind == MessageKind.SLOT_PROPOSAL:
        slot_line = "\n".join(f"{index}. {text}" for index, text in enumerate(slots, start=1))
        body = (
            f"Здравствуйте, {name}!\n\n"
            f"Готовы обсудить вакансию «{vacancy}»{company_suffix}. "
            "Есть несколько вариантов времени (конфликта в календаре нет):\n"
            f"{slot_line or '1. Уточним время по вашей доступности.'}\n\n"
            "Напишите номер удобного варианта — забронируем слот."
        )
    elif kind == MessageKind.CONFIRMATION:
        body = (
            f"Здравствуйте, {name}!\n\n"
            "Спасибо, время подтверждено: встреча запланирована. "
            "Если планы изменятся — напишите, пожалуйста, здесь же."
        )
    elif kind == MessageKind.REJECTION:
        body = (
            f"Здравствуйте, {name}!\n\n"
            f"Спасибо, что откликнулись на вакансию «{vacancy}»{company_suffix}. "
            "Сейчас готовы продолжить с другими кандидатами, чей опыт ближе к задачам роли. "
            "Будем рады видеть ваши отклики на другие наши вакансии."
        )
    elif kind == MessageKind.REMINDER:
        body = (
            f"Здравствуйте, {name}!\n\n"
            "Напоминаем о нашей договорённости по собеседованию. "
            "Если возникли сложности со временем — напишите, подберём другой вариант."
        )
    else:
        body = (
            f"Здравствуйте, {name}!\n\n"
            f"Спасибо за отклик на вакансию «{vacancy}»{company_suffix}. "
            f"{reason or 'Свяжемся с вами по следующему шагу.'}"
        )
    return _fit(body.strip(), request.max_chars)


def _as_lines(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list | tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _fit(text: str, max_chars: int) -> str:
    """Обрезать текст до ``max_chars`` по границе предложения или слова."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    for separator in (". ", "! ", "? ", "\n"):
        position = cut.rfind(separator)
        if position > max_chars // 2:
            return cut[: position + 1].strip()
    position = cut.rfind(" ")
    return (cut[:position] if position > 0 else cut).strip()


# --------------------------------------------------------------------- ответы


def _detect_intent(lowered: str, chosen: int | None) -> CandidateIntent:
    if any(marker in lowered for marker in _NOT_INTERESTED):
        return CandidateIntent.NOT_INTERESTED
    if chosen is not None:
        return CandidateIntent.INTERESTED
    if any(marker in lowered for marker in _RESCHEDULE):
        return CandidateIntent.RESCHEDULE_REQUEST
    if any(marker in lowered for marker in _QUESTION):
        return CandidateIntent.QUESTION
    if any(marker in lowered for marker in _INTERESTED):
        return CandidateIntent.INTERESTED
    if extract_contacts(lowered) or any(ch.isdigit() for ch in lowered):
        return CandidateIntent.PROVIDED_INFO
    return CandidateIntent.UNCLEAR


def _answer_tokens(*texts: str) -> set[str]:
    """Значимые слова для сравнения ответа с ожиданием критерия."""
    stop = {
        "и",
        "в",
        "на",
        "с",
        "по",
        "для",
        "не",
        "что",
        "как",
        "это",
        "или",
        "есть",
        "был",
        "была",
        "было",
        "очень",
        "тоже",
        "также",
        "мне",
        "мы",
        "the",
        "and",
        "with",
        "for",
        "from",
    }
    tokens: set[str] = set()
    for text in texts:
        for word in re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9+#.]+", (text or "").casefold()):
            if len(word) >= 4 and word not in stop:
                tokens.add(word)
    return tokens


def _detect_slot_choice(lowered: str, slots_count: int) -> int | None:
    if slots_count <= 0:
        return None
    for marker, index in _ORDINALS.items():
        if marker in lowered and index <= slots_count:
            return index
    for match in re.finditer(r"(?:вариант|слот|номер)\s*(\d{1,2})", lowered):
        index = int(match.group(1))
        if 1 <= index <= slots_count:
            return index
    stripped = lowered.strip()
    if stripped.isdigit():
        index = int(stripped)
        if 1 <= index <= slots_count:
            return index
    return None


def _confidence(intent: CandidateIntent, chosen: int | None) -> float:
    if chosen is not None:
        return 0.9
    if intent is CandidateIntent.UNCLEAR:
        return 0.2
    if intent is CandidateIntent.QUESTION:
        return 0.6
    return 0.75


def _reply_summary(text: str) -> str:
    cleaned = " ".join(text.split())
    return cleaned[:160]
