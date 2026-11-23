"""LLM через любой OpenAI-совместимый эндпоинт (``{base_url}/chat/completions``).

Ключевые решения:

* **защита от prompt injection**: текст резюме уходит в отдельном блоке
  ``<resume>`` как данные, в системном промпте прямо сказано игнорировать любые
  инструкции из него, а закрывающие теги экранируются (см. ``prompts.py``);
* **валидация ответа схемой**: ответ модели разбирается Pydantic-схемой; невалидный
  ответ — ``LlmError``, а не «сырой текст в бизнес-логику»;
* **итоговый балл не берётся из ответа модели**: модель даёт оценки по критериям,
  адаптер нормализует их, а взвешивание делает вызывающая сторона (здесь оно
  вычисляется лишь как значение-заглушка для ``ScreeningDraft.total_score``);
* **кэш по ``content_hash``**: второй вызов с тем же входом не идёт в сеть
  (в кэш попадает уже провалидированный и нормализованный результат, включая
  версию промпта, модель и состав критериев);
* **редакция ПДн**: при ``HIREBOT_LLM__SEND_PII=false`` ФИО, телефоны, email и
  названия компаний вырезаются до отправки, а контакты кандидата извлекаются
  локально — модель их не видит, но бизнес-данные не теряются.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from hirebot.adapters.http_utils import error_code, error_summary, json_object, retry_after_s
from hirebot.adapters.llm.prompts import (
    ANSWER_SYSTEM_PROMPT,
    MESSAGE_SYSTEM_PROMPT,
    PROMPT_VERSION,
    REPLY_SYSTEM_PROMPT,
    SCREENING_SYSTEM_PROMPT,
    answer_user_prompt,
    message_user_prompt,
    reply_user_prompt,
    screening_user_prompt,
)
from hirebot.adapters.llm.redaction import (
    CANDIDATE_PLACEHOLDER,
    extract_contacts,
    redact_text,
)
from hirebot.adapters.llm.schemas import (
    AnswerAssessmentResponseSchema,
    MessageResponseSchema,
    ReplyAnalysisResponseSchema,
    ScreeningResponseSchema,
)
from hirebot.adapters.parsing import as_dict, as_list, str_or_none
from hirebot.config import LlmSettings
from hirebot.domain.entities import CandidateReply, ScreeningCriterion
from hirebot.domain.enums import CandidateIntent
from hirebot.domain.errors import (
    AuthExpired,
    ChannelUnavailable,
    GuardrailViolation,
    LlmError,
    RateLimited,
)
from hirebot.logging_setup import get_logger
from hirebot.observability.metrics import record_llm_call
from hirebot.ports.infra import CachePort, HttpClientPort, HttpResponse
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

__all__ = ["OpenAiCompatAdapter"]

logger = get_logger(__name__)

#: Порог, ниже которого критерий считается «не подтверждённым».
_GAP_THRESHOLD = 55.0

#: Минимальная длина осмысленной цитаты. «—», «нет» и пустая строка — не evidence.
_MIN_EVIDENCE_CHARS = 3

#: Служебные слова, которые не доказывают, что цитата взята из резюме.
_EVIDENCE_STOPWORDS = frozenset(
    {"и", "в", "во", "на", "с", "со", "по", "для", "от", "до", "не", "the", "and", "of", "with", "for", "a"}
)
_EVIDENCE_TOKEN_RE = re.compile(r"[a-zа-яё0-9+#]{3,}")

#: Разрешённые «пустые» ответы модели, которые нужно считать мусором.
_EMPTY_MARKERS = {"", "null", "none", "нет"}

#: Схема ответа модели (для типобезопасной валидации).
_ModelT = TypeVar("_ModelT", bound=BaseModel)


class OpenAiCompatAdapter(LlmPort):
    """Провайдер модели через ``/chat/completions``."""

    def __init__(
        self,
        settings: LlmSettings,
        *,
        http: HttpClientPort,
        cache: CachePort,
    ) -> None:
        self._settings = settings
        self._http = http
        self._cache = cache
        self._endpoint = f"{settings.base_url.rstrip('/')}/chat/completions"

    # ------------------------------------------------------------ скрининг

    async def screen_resume(self, request: ScreeningRequest) -> ScreeningDraft:
        """Оценить резюме по критериям вакансии (структурированный ответ)."""
        criteria_spec = list(request.criteria)
        resume_text = request.resume_text
        if not self._settings.send_pii:
            resume_text = redact_text(resume_text)
        user_prompt = screening_user_prompt(request, resume_text=resume_text, criteria=criteria_spec)
        cache_key = self._cache_key(
            "screen",
            request.content_hash,
            extra=_criteria_fingerprint(criteria_spec) + f":{int(self._settings.send_pii)}",
        )
        payload, _ = await self._chat(system=SCREENING_SYSTEM_PROMPT, user=user_prompt, cache_key=cache_key)
        parsed = self._validate(ScreeningResponseSchema, payload, context="скрининг")
        criteria = self._normalize_criteria(criteria_spec, parsed, resume_text=resume_text)
        returned = {item.key for item in parsed.criteria}
        missing = [item.key for item in criteria if item.key not in returned]
        if missing:
            logger.warning("llm_screening_missing_criteria", keys=missing[:5])
        return ScreeningDraft(
            total_score=round(_weighted_total(criteria), 1),
            summary=parsed.summary,
            criteria=tuple(criteria),
            gaps=tuple(parsed.gaps[:5]),
            strengths=tuple(parsed.strengths[:5]),
            questions=tuple(parsed.questions[:5]),
            model=self._settings.model,
        )

    def _normalize_criteria(
        self,
        spec: Sequence[Mapping[str, Any]],
        parsed: ScreeningResponseSchema,
        *,
        resume_text: str,
    ) -> list[ScreeningCriterion]:
        """Собрать оценки строго по критериям вакансии (и нормализовать их).

        Оценки, которых нет в ответе модели, получают 0 и помечаются как пробел:
        «модель забыла критерий» нельзя трактовать как «критерий выполнен».
        Evidence проверяется дважды: оно должно быть непустым и содержать хотя бы
        одно значимое слово из резюме. Иначе модель «подтвердила» критерий
        выдуманной цитатой — такому ответу верить нельзя.
        """
        by_key = {item.key: item for item in parsed.criteria}
        criteria: list[ScreeningCriterion] = []
        for raw in spec:
            key = str(raw.get("key") or raw.get("title") or "criterion")
            title = str(raw.get("title") or key)
            weight = _as_float(raw.get("weight"), default=1.0)
            must_have = bool(raw.get("must_have", False))
            found = by_key.get(key)
            score = found.score if found is not None else 0.0
            evidence = found.evidence if found is not None else ""
            if not _evidence_is_usable(evidence, resume_text):
                # Требование к модели: нет цитаты — критерий не зачитывается.
                evidence = ""
                score = min(score, 30.0)
            is_gap = score < _GAP_THRESHOLD or (must_have and score < 60.0) or not evidence
            criteria.append(
                ScreeningCriterion(
                    key=key,
                    title=title,
                    weight=weight,
                    score=round(score, 1),
                    must_have=must_have,
                    evidence=evidence[:300],
                    is_gap=is_gap,
                )
            )
        return criteria

    # ------------------------------------------------------------ сообщения

    async def assess_answer(self, request: AnswerAssessmentRequest) -> AnswerAssessment:
        """Оценить ответ кандидата по критерию профиля."""
        answer = request.answer
        resume_text = request.resume_text
        if not self._settings.send_pii:
            answer = redact_text(answer)
            resume_text = redact_text(resume_text)
        safe_request = AnswerAssessmentRequest(
            criterion_key=request.criterion_key,
            criterion_title=request.criterion_title,
            expectation=request.expectation,
            evidence_hint=request.evidence_hint,
            question=request.question,
            answer=answer,
            resume_text=resume_text,
        )
        user_prompt = answer_user_prompt(safe_request)
        cache_key = self._cache_key(
            "answer",
            _hash_json(
                {
                    "answer": answer,
                    "criterion": request.criterion_key,
                    "question": request.question,
                    "pii": self._settings.send_pii,
                }
            ),
        )
        payload, _ = await self._chat(system=ANSWER_SYSTEM_PROMPT, user=user_prompt, cache_key=cache_key)
        parsed = self._validate(AnswerAssessmentResponseSchema, payload, context="оценка ответа")
        return AnswerAssessment(
            score=float(parsed.score),
            evidence=parsed.evidence,
            is_unclear=bool(parsed.is_unclear),
            contradicts_resume=bool(parsed.contradicts_resume),
        )

    async def compose_message(self, request: MessageRequest) -> MessageDraft:
        """Сгенерировать сообщение кандидату."""
        candidate_name = request.candidate_name
        send_pii = self._settings.send_pii
        if not send_pii:
            candidate_name = CANDIDATE_PLACEHOLDER
        user_prompt = message_user_prompt(request, candidate_name=candidate_name)
        cache_key = self._cache_key(
            "message",
            _hash_json(
                {
                    "kind": request.kind,
                    "name": candidate_name,
                    "vacancy": request.vacancy_title,
                    "company": request.company_name,
                    "context": request.context,
                    "history": list(request.history),
                    "max_chars": request.max_chars,
                    "tone": request.tone,
                    "pii": send_pii,
                }
            ),
        )
        payload, cached = await self._chat(
            system=MESSAGE_SYSTEM_PROMPT, user=user_prompt, cache_key=cache_key
        )
        parsed = self._validate(MessageResponseSchema, payload, context="сообщение")
        text = _fit(parsed.text, request.max_chars)
        if not send_pii:
            # Реальное имя подставляем локально: модель его не видела.
            text = text.replace(CANDIDATE_PLACEHOLDER, _first_name(request.candidate_name))
        elif CANDIDATE_PLACEHOLDER in text:
            text = text.replace(CANDIDATE_PLACEHOLDER, _first_name(request.candidate_name))
        self._guard_outgoing(text)
        return MessageDraft(
            text=text,
            model=self._settings.model,
            prompt_version=PROMPT_VERSION,
            cached=cached,
        )

    # ------------------------------------------------------------ разбор ответа

    async def analyze_reply(self, request: ReplyAnalysisRequest) -> CandidateReply:
        """Разобрать ответ кандидата: намерение, слот, факты."""
        reply_text = request.reply_text
        if not self._settings.send_pii:
            reply_text = redact_text(reply_text)
        user_prompt = reply_user_prompt(
            ReplyAnalysisRequest(
                candidate_name=request.candidate_name,
                reply_text=reply_text,
                question_asked=request.question_asked,
                proposed_slots=request.proposed_slots,
            )
        )
        cache_key = self._cache_key(
            "reply",
            _hash_json(
                {
                    "reply": reply_text,
                    "question": request.question_asked,
                    "slots": list(request.proposed_slots),
                    "pii": self._settings.send_pii,
                }
            ),
        )
        payload, _ = await self._chat(system=REPLY_SYSTEM_PROMPT, user=user_prompt, cache_key=cache_key)
        parsed = self._validate(ReplyAnalysisResponseSchema, payload, context="разбор ответа")
        extracted: dict[str, Any] = dict(parsed.extracted)
        # Контакты ищем сами, по исходному тексту: при SEND_PII=false модель их
        # вообще не видит, а сервису телефон и email нужны.
        for key, value in extract_contacts(request.reply_text).items():
            extracted.setdefault(key, value)
        chosen = parsed.chosen_slot_index
        if chosen is not None and chosen > len(request.proposed_slots):
            logger.warning("llm_slot_index_out_of_range", index=chosen, slots=len(request.proposed_slots))
            chosen = None
        return CandidateReply(
            intent=CandidateIntent(parsed.intent),
            summary=parsed.summary,
            extracted=extracted,
            chosen_slot_index=chosen,
            confidence=parsed.confidence,
            raw_text=request.reply_text,
        )

    # ------------------------------------------------------------ внутреннее

    async def _chat(self, *, system: str, user: str, cache_key: str) -> tuple[dict[str, Any], bool]:
        """Вызвать модель и вернуть (JSON-объект, признак попадания в кэш)."""
        cached = await self._cache.get_json(cache_key)
        cached_payload = as_dict(cached)
        if cached_payload:
            logger.info("llm_cache_hit", key=cache_key)
            record_llm_call(self._provider_label(), "ok", 0.0, cached=True)
            return cached_payload, True

        api_key = self._settings.api_key
        if api_key is None:
            msg = "HIREBOT_LLM__API_KEY не задан: вызов модели невозможен"
            record_llm_call(self._provider_label(), "config_error", 0.0, cached=False)
            raise LlmError(msg)
        request_body: dict[str, Any] = {
            "model": self._settings.model,
            "temperature": self._settings.temperature,
            "max_tokens": self._settings.max_tokens,
            # json_object поддерживают все распространённые провайдеры
            # (OpenAI, vLLM, OpenRouter, прокси GigaChat/YandexGPT), а строгую
            # JSON-схему — не все. Сама схема задана в промпте, а ответ всё равно
            # валидируется Pydantic, поэтому «мягкий» режим безопаснее.
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        started = time.perf_counter()
        try:
            response = await self._http.request(
                "POST",
                self._endpoint,
                json_body=request_body,
                headers={
                    "Authorization": f"Bearer {api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                idempotent=True,
                bucket="llm",
            )
            self._raise_for_status(response)
            payload = self._extract_payload(response)
        except Exception:
            # Ошибки модели важны сами по себе: по ним видно деградацию провайдера.
            record_llm_call(self._provider_label(), "error", time.perf_counter() - started, cached=False)
            raise
        record_llm_call(self._provider_label(), "ok", time.perf_counter() - started, cached=False)
        if self._settings.cache_ttl_s > 0:
            await self._cache.set_json(cache_key, payload, ttl_s=float(self._settings.cache_ttl_s))
        return payload, False

    def _provider_label(self) -> str:
        """Метка провайдера для метрик.

        Модель в метку не попадает: она меняется, а кардинальность метрик должна
        оставаться ограниченной.
        """
        return "openai_compat"

    def _raise_for_status(self, response: HttpResponse) -> None:
        status = int(response.status_code)
        if 200 <= status < 300:
            return
        detail = error_summary(response)
        code = error_code(response) or str(status)
        message = f"LLM вернула ошибку: {code} — {detail}"
        if status == 429:
            raise RateLimited(message, retry_after_s=retry_after_s(response))
        if status in {401, 403}:
            raise AuthExpired(message)
        if status >= 500:
            raise ChannelUnavailable(message)
        raise LlmError(message)

    def _extract_payload(self, response: HttpResponse) -> dict[str, Any]:
        """Достать JSON-объект из ответа ``chat/completions``."""
        body = json_object(response, context="LLM chat/completions")
        choices = as_list(body.get("choices"))
        if not choices:
            msg = "LLM вернула ответ без choices"
            raise LlmError(msg)
        message = as_dict(as_dict(choices[0]).get("message"))
        content = str_or_none(message.get("content"))
        if content is None:
            msg = "LLM вернула пустое содержимое сообщения"
            raise LlmError(msg)
        return _parse_json_object(content)

    def _validate(self, schema: type[_ModelT], payload: Mapping[str, Any], *, context: str) -> _ModelT:
        try:
            return schema.model_validate(dict(payload))
        except ValidationError as exc:
            msg = f"Ответ модели не соответствует схеме ({context}): {exc.errors()[:2]}"
            raise LlmError(msg) from exc

    def _guard_outgoing(self, text: str) -> None:
        """Проверить исходящий текст перед отправкой кандидату.

        Заглушек и мусора быть не должно: «нет», «null» и пустой текст — это
        ошибка модели, которую нельзя показывать человеку.
        """
        if text.strip().casefold() in _EMPTY_MARKERS:
            msg = f"Модель вернула непригодный текст сообщения: {text!r}"
            raise GuardrailViolation(msg)

    def _cache_key(self, kind: str, content_hash: str, *, extra: str = "") -> str:
        parts = [PROMPT_VERSION, kind, self._settings.model, content_hash]
        if extra:
            parts.append(extra)
        return "llm:" + ":".join(parts)


def _evidence_is_usable(evidence: str, resume_text: str) -> bool:
    """Похоже ли evidence на реальную цитату из резюме.

    Проверка мягкая: достаточно, чтобы хотя бы одно значимое слово цитаты
    встречалось в резюме. Точное совпадение строки не требуем — модель может
    нормализовать регистр или пунктуацию. Но полностью выдуманная цитата
    («кандидат идеален») не проходит и не зачитывает критерий.
    """
    text = evidence.strip()
    if len(text) < _MIN_EVIDENCE_CHARS:
        return False
    tokens = {
        token for token in _EVIDENCE_TOKEN_RE.findall(text.casefold()) if token not in _EVIDENCE_STOPWORDS
    }
    if not tokens:
        return False
    normalized_resume = resume_text.casefold()
    return any(token in normalized_resume for token in tokens)


def _criteria_fingerprint(criteria: Sequence[Mapping[str, Any]]) -> str:
    """Отпечаток критериев: изменение весов/формулировок должно сбрасывать кэш."""
    normalized = [
        {
            "key": str(item.get("key", "")),
            "weight": _as_float(item.get("weight"), default=1.0),
            "must_have": bool(item.get("must_have", False)),
            "expectation": str(item.get("expectation", "")),
        }
        for item in criteria
    ]
    return _hash_json(normalized)


def _hash_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _parse_json_object(content: str) -> dict[str, Any]:
    """Разобрать JSON из ответа модели (в т.ч. с markdown-обёрткой).

    Модели любят оборачивать JSON в ```json ... ```. Это не повод падать, но и
    не повод доверять: результат всё равно проходит Pydantic-валидацию.
    """
    text = content.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").strip()
        if text.endswith("```"):
            text = text[: -len("```")].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"Модель вернула не JSON: {exc.msg} (позиция {exc.pos})"
        raise LlmError(msg) from exc
    if not isinstance(parsed, dict):
        msg = f"Ожидался JSON-объект, получено {type(parsed).__name__}"
        raise LlmError(msg)
    return parsed


def _weighted_total(criteria: Sequence[ScreeningCriterion]) -> float:
    if not criteria:
        return 0.0
    weight_sum = sum(item.weight for item in criteria) or float(len(criteria))
    return sum(item.score * item.weight for item in criteria) / weight_sum


def _as_float(value: object, *, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _first_name(full_name: str) -> str:
    name = (full_name or "").strip()
    return name.split()[0] if name else "кандидат"


def _fit(text: str, max_chars: int) -> str:
    """Обрезать сообщение до лимита по границе предложения."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    for separator in (". ", "! ", "? ", "\n"):
        position = cut.rfind(separator)
        if position > max_chars // 2:
            return cut[: position + 1].strip()
    position = cut.rfind(" ")
    return (cut[:position] if position > 0 else cut).strip()
