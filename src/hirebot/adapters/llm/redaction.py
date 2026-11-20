"""Редакция персональных данных перед отправкой текста во внешнюю модель.

Зачем отдельный модуль: 152-ФЗ (трансграничная передача) и внутренняя политика
``HIREBOT_LLM__SEND_PII=false`` требуют, чтобы в модель уходил обезличенный текст.
Редакция — чистая функция без сети и без состояния, поэтому её легко проверить
и невозможно «забыть применить» в одном из трёх вызовов LLM.

Что редактируется: email, телефоны, ФИО, названия компаний-работодателей,
ссылки и мессенджеры. Что **не** редактируется: грейд, навыки, стек, города,
длительность опыта — без них скоринг теряет смысл.

Важная деталь: контакты кандидата **извлекаются локально** (``extract_contacts``)
до редакции. Сервису нужны телефон и email для связи, но отправлять их в модель
не требуется — их находит наш код, а не LLM.
"""

from __future__ import annotations

import re

__all__ = [
    "CANDIDATE_PLACEHOLDER",
    "extract_contacts",
    "redact_text",
]

#: Плейсхолдер имени кандидата в промптах.
#: Модель обязана сохранить его дословно, а реальное имя подставляем локально —
#: так имя не покидает наш контур, но сообщение остаётся человеческим.
CANDIDATE_PLACEHOLDER = "{{CANDIDATE}}"

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*[\w]")
_PHONE_RU_RE = re.compile(r"(?:\+7|8|7)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
_PHONE_INTL_RE = re.compile(r"\+\d[\d\s\-()]{7,}\d")
_NAME_RE = re.compile(r"\b[А-ЯЁ][а-яё]{1,20}(?:\s+[А-ЯЁ][а-яё]{1,20}){1,2}\b")
_COMPANY_RE = re.compile(
    r"""(?:\b(?:ООО|ОАО|ЗАО|ПАО|АО|ИП|LLC|Ltd|Inc)\b\.?\s*«?[^»\n,;]{2,40}»?)"""
    r"""|(?:«[^»\n]{2,40}»)|(?:"[^"\n]{2,40}")"""
)
_MESSENGER_RE = re.compile(
    r"(?:t\.me/|telegram|телеграм|whatsapp|ватсап|wa\.me/|vk\.com/|@[A-Za-z][\w.]{3,})",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S+|www\.\S+")

#: Слова, которые формально подходят под шаблон ФИО, но именами не являются:
#: заголовки разделов резюме из двух слов с заглавных букв.
_NAME_STOPWORDS = frozenset(
    {
        "Опыт Работы",
        "Ключевые Навыки",
        "Высшее Образование",
        "Английский Язык",
        "Желаемая Зарплата",
        "Российская Федерация",
        "Московская Область",
        "Профессиональные Навыки",
        "Личные Качества",
    }
)


def redact_text(
    text: str,
    *,
    redact_names: bool = True,
    redact_companies: bool = True,
) -> str:
    """Обезличить текст резюме/письма.

    Порядок замен важен: сначала email и телефоны (они могут содержать цифры,
    которые иначе «склеятся» с другими шаблонами), затем ссылки, компании и ФИО.
    """
    if not text:
        return text
    result = _EMAIL_RE.sub("[EMAIL]", text)
    result = _PHONE_RU_RE.sub("[ТЕЛЕФОН]", result)
    result = _PHONE_INTL_RE.sub("[ТЕЛЕФОН]", result)
    result = _URL_RE.sub("[ССЫЛКА]", result)
    result = _MESSENGER_RE.sub("[МЕССЕНДЖЕР]", result)
    if redact_companies:
        result = _COMPANY_RE.sub("[КОМПАНИЯ]", result)
    if redact_names:
        result = _NAME_RE.sub(_replace_name, result)
    return result


def _replace_name(match: re.Match[str]) -> str:
    value = match.group(0)
    if value in _NAME_STOPWORDS:
        return value
    return "[ФИО]"


def extract_contacts(text: str) -> dict[str, str]:
    """Локально извлечь контакты кандидата из текста (до редакции).

    Возвращаются только реально найденные поля: пустая строка в ``extracted``
    выглядела бы как «контакт есть, но пустой» и мешала бы разбору инцидентов.
    """
    contacts: dict[str, str] = {}
    email = _EMAIL_RE.search(text or "")
    if email:
        contacts["email"] = email.group(0)
    phone = _PHONE_RU_RE.search(text or "") or _PHONE_INTL_RE.search(text or "")
    if phone:
        contacts["phone"] = phone.group(0).strip()
    return contacts
