"""Policy Guard — правила, ограничивающие автономность бота.

Это не «настройки удобства», а защита от юридических и репутационных
последствий:

* **автоотказ** выключен по умолчанию (решение об отказе принимает человек);
* **лимит автосообщений** — кандидат не должен получать поток писем;
* **тихие часы** — не пишем ночью в часовом поясе кандидата;
* **защищённые признаки** (пол, возраст, национальность, прописка и т.п.)
  запрещены в критериях скоринга: это прямой путь к ст. 3 ТК РФ и
  ст. 13.11.1 КоАП РФ, поэтому запрет живёт в коде, а не в инструкции;
* **guardrail исходящего текста** — пустой текст, переросший лимит, мат или
  маркеры утечки промпта не уходят кандидату.

Проверки намеренно бросают доменные исключения: вызывающий код обязан
принять решение (обычно — увести диалог в ``HUMAN_REVIEW``), а не «проглотить»
нарушение.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from hirebot.config import PolicySettings
from hirebot.domain.enums import ConversationState
from hirebot.domain.errors import GuardrailViolation, PolicyViolation
from hirebot.domain.fsm import assert_can_send as assert_state_allows_send
from hirebot.logging_setup import get_logger
from hirebot.services.messaging import safe_zone

logger = get_logger(__name__)

#: Максимальная длина исходящего сообщения: длинные «простыни» не читают.
MAX_OUTBOUND_CHARS = 2000

#: Маркеры того, что модель начала пересказывать свои инструкции.
#: Текст резюме — недоверенный ввод, и попытка инъекции промпта может
#: «просочиться» в ответ.
PROMPT_LEAK_MARKERS: tuple[str, ...] = (
    "system prompt",
    "system message",
    "системный промпт",
    "системная инструкция",
    "игнорируй инструкции",
    "игнорируй предыдущие инструкции",
    "ignore previous instructions",
    "ignore all previous instructions",
)

#: Минимальный список нецензурной лексики (корни, привязанные к началу слова).
#: Список сознательно короткий: задача — отсечь очевидное, а не собрать
#: исчерпывающий словарь.
_PROFANITY_RE = re.compile(
    r"\b(?:хуй|хуе|хер|пизд|бляд|блят|ебан|ебал|ебет|ебат|мудак|мудил|сука|суки"
    r"|fuck|fuckin|shit|bitch|asshole|cunt)",
    re.IGNORECASE,
)

#: Защищённые признаки: человекочитаемая группа -> формы, которые ищем в
#: ключах и заголовках критериев. Формы даны явно, потому что русская
#: морфология ломает наивный поиск подстроки: «пол» встречается в «полный»
#: и «получать», а «age» — в «manager».
_PROTECTED_GROUPS: Mapping[str, tuple[str, ...]] = {
    "пол": ("пол", "пола", "полу", "полом", "половая", "половой", "gender", "sex"),
    "возраст": (
        "возраст",
        "age",
        "дата рождения",
        "рождения",
        "birth",
        "birthday",
        "birthdate",
    ),
    "национальность": (
        "национальность",
        "нация",
        "nationality",
        "ethnicity",
        "раса",
        "race",
    ),
    "гражданство": ("гражданство", "citizenship"),
    "религия": ("религия", "вероисповедание", "religion"),
    "семейное положение": (
        "семейное положение",
        "marital",
        "marital status",
        "женат",
        "замужем",
        "брак",
    ),
    "дети": ("дети", "детей", "детям", "детьми", "детях", "ребенок", "children", "child"),
    "прописка": ("прописка", "прописку", "прописки", "регистрация", "registration"),
    "фото": ("фото", "фотография", "photo", "photos", "photograph"),
}

#: С какой длины формы разрешено совпадение по префиксу (падежи и склонения).
_PREFIX_MATCH_MIN_LEN = 6

_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-zа-я]+")


def normalize_text(value: str) -> str:
    """Привести текст к сравнимому виду: нижний регистр, ё -> е, пробелы."""
    return value.lower().replace("ё", "е")


def _tokenize(value: str) -> list[str]:
    return [token for token in _TOKEN_SPLIT_RE.split(normalize_text(value or "")) if token]


def find_protected_attribute(text: str) -> str | None:
    """Найти защищённый признак в тексте; вернуть название группы.

    Совпадение ищем по словам: короткие формы («пол», «age») — только точно,
    длинные («возраст», «registration») — ещё и по префиксу, чтобы поймать
    падежи.
    """
    tokens = _tokenize(text)
    if not tokens:
        return None
    token_set = set(tokens)
    joined = " ".join(tokens)

    for group, forms in _PROTECTED_GROUPS.items():
        for form in forms:
            if " " in form:
                if form in joined:
                    return group
                continue
            matched = form in token_set
            if not matched and len(form) >= _PREFIX_MATCH_MIN_LEN:
                matched = any(token.startswith(form) for token in tokens)
            if matched:
                return group
    return None


def _criterion_texts(criterion: Mapping[str, Any]) -> list[str]:
    """Все текстовые поля критерия, включая имена его ключей.

    Имена ключей тоже проверяем: ``{"age": 30}`` — такой же защищённый
    признак, как ``{"title": "Возраст"}``.
    """
    texts = [str(name) for name in criterion]
    texts.extend(str(value) for value in criterion.values() if isinstance(value, str))
    return texts


class PolicyGuard:
    """Проверки политики перед любым исходящим действием бота."""

    def __init__(self, settings: PolicySettings) -> None:
        self._settings = settings

    @property
    def settings(self) -> PolicySettings:
        return self._settings

    # ------------------------------------------------------------------ #
    # Исходящие сообщения
    # ------------------------------------------------------------------ #

    def assert_can_send(
        self,
        *,
        state: ConversationState,
        auto_messages_sent: int,
        now: datetime,
        candidate_timezone: str = "Europe/Moscow",
    ) -> None:
        """Можно ли боту отправить сообщение прямо сейчас.

        Проверяются три независимых ограничения: право состояния из FSM,
        лимит автосообщений и тихие часы в зоне кандидата.
        """
        assert_state_allows_send(state)
        self._assert_message_budget(state=state, auto_messages_sent=auto_messages_sent)
        self._assert_not_quiet_hours(now=now, candidate_timezone=candidate_timezone)

    def _assert_message_budget(self, *, state: ConversationState, auto_messages_sent: int) -> None:
        limit = self._settings.max_auto_messages_per_candidate
        if auto_messages_sent >= limit:
            msg = (
                f"Исчерпан лимит автосообщений кандидату: отправлено {auto_messages_sent} "
                f"из {limit} (состояние {state.value}). Нужен человек."
            )
            raise PolicyViolation(msg)

    def _assert_not_quiet_hours(self, *, now: datetime, candidate_timezone: str) -> None:
        if now.tzinfo is None:
            logger.warning("naive_now_in_policy", timezone=candidate_timezone)
            aware = now.replace(tzinfo=UTC)
        else:
            aware = now
        hour = aware.astimezone(safe_zone(candidate_timezone)).hour
        if _is_quiet_hour(hour, self._settings):
            start = self._settings.quiet_hours_start
            end = self._settings.quiet_hours_end
            msg = (
                f"Тихие часы: {hour:02d}:00 в поясе {candidate_timezone} "
                f"(писать нельзя с {start:02d}:00 до {end:02d}:00). Сообщение отложено."
            )
            raise PolicyViolation(msg)

    def assert_text_allowed(self, text: str) -> None:
        """Guardrail исходящего текста: пусто, слишком длинно, мат, утечка промпта."""
        stripped = (text or "").strip()
        if not stripped:
            msg = "Пустой текст исходящего сообщения: отправлять нечего"
            raise GuardrailViolation(msg)
        if len(stripped) > MAX_OUTBOUND_CHARS:
            msg = (
                f"Текст длиннее {MAX_OUTBOUND_CHARS} символов ({len(stripped)}): "
                "такое сообщение не читают, сократите или разбейте на части"
            )
            raise GuardrailViolation(msg)

        normalized = normalize_text(stripped)
        for marker in PROMPT_LEAK_MARKERS:
            if normalize_text(marker) in normalized:
                msg = (
                    f"В исходящем тексте найден маркер утечки промпта {marker!r}: "
                    "модель пересказывает инструкции вместо ответа кандидату"
                )
                raise GuardrailViolation(msg)

        found = _PROFANITY_RE.search(normalized)
        if found is not None:
            msg = (
                f"В исходящем тексте найдена нецензурная лексика {found.group(0)!r}: "
                "такое сообщение не отправляем"
            )
            raise GuardrailViolation(msg)

    # ------------------------------------------------------------------ #
    # Критерии скоринга
    # ------------------------------------------------------------------ #

    def assert_criteria_allowed(self, criteria: Sequence[dict[str, Any]]) -> None:
        """Запретить критерии по защищённым признакам.

        Проверяются ключи, заголовки, ожидания и имена полей критерия: любой
        признак, по которому кандидату можно отказать «за то, что он такой»,
        означает дискриминацию, а не подбор.
        """
        for index, criterion in enumerate(criteria):
            for text in _criterion_texts(criterion):
                group = find_protected_attribute(text)
                if group is not None:
                    msg = (
                        f"Критерий #{index + 1} использует защищённый признак "
                        f"{group!r} (поле: {text!r}). Скоринг по полу, возрасту, "
                        "национальности, гражданству, религии, семейному положению, "
                        "детям, прописке и фото запрещён (ст. 3 ТК РФ)."
                    )
                    raise PolicyViolation(msg)

    def assert_questions_allowed(self, questions: Sequence[str]) -> None:
        """Запретить вопросы по защищённым признакам.

        Критерий и вопрос — разные пути к одной дискриминации. Вопрос «планируете
        ли детей?» формально не критерий, но отсекает по полу и семейному
        положению, поэтому проверяется тем же списком.
        """
        for index, text in enumerate(questions):
            value = str(text or "")
            group = find_protected_attribute(value)
            if group is not None:
                msg = (
                    f"Вопрос #{index + 1} касается защищённого признака {group!r}: {value!r}. "
                    "Спрашивать о поле, возрасте, национальности, гражданстве, религии, "
                    "семейном положении, детях, прописке и здоровье запрещено."
                )
                raise PolicyViolation(msg)


def _is_quiet_hour(hour: int, settings: PolicySettings) -> bool:
    """Попадает ли час в тихие часы.

    Штатная конфигурация: ``quiet_hours_start=21``, ``quiet_hours_end=9``,
    то есть тишина с 21:00 до 09:00, а писать можно в ``09:00..20:59``.
    Если границы заданы «наоборот» (начало раньше конца), считаем тихим
    именно интервал ``[start, end)`` — так конфигурация остаётся осмысленной,
    а не «всегда тихо».
    """
    start = settings.quiet_hours_start
    end = settings.quiet_hours_end
    if start == end:
        return False
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end
