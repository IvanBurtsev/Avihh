"""Каталог критериев подбора: справочник, из которого выбирает человек.

Зачем это отдельный модуль.

Критерии скоринга — не техническая деталь, а решение бизнеса: по каким
признакам мы считаем человека подходящим. Система не должна их выдумывать.
Поэтому каталог хранится данными (``data/criteria/catalog.yaml``), а код умеет
только читать, проверять и показывать его.

Что проверяется при загрузке:

* уникальность ключей и существование категории;
* корректность служебных полей (``measurable``, ``weight_hint``);
* **отсутствие запрещённых признаков** — каталог прогоняется через тот же
  ``PolicyGuard``, что и рабочая настройка вакансии. Если кто-то добавит
  критерий «возраст до 40», загрузка упадёт, а не попадёт в интерфейс.

Итоговые веса и обязательность утверждает рекрутер под конкретную вакансию:
поля ``weight_hint`` и ``must_have_hint`` — это подсказка, а не решение.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from hirebot.config import PolicySettings
from hirebot.domain.errors import DomainError
from hirebot.services.policy import PolicyGuard

#: Где лежит каталог по умолчанию (относительно корня проекта).
DEFAULT_CATALOG_PATH = Path("data/criteria/catalog.yaml")

#: Допустимые значения поля ``measurable``.
MEASURABLE_LEVELS = frozenset({"high", "medium", "low"})

#: Допустимые значения поля ``kind``.
CRITERION_KINDS = frozenset(
    {"skill", "experience", "education", "language", "condition", "document", "soft", "location"}
)

#: Подписи уровней проверяемости — для интерфейса и документа.
MEASURABLE_LABELS: dict[str, str] = {
    "high": "видно из резюме",
    "medium": "видно частично",
    "low": "только вопросом",
}


class CatalogError(DomainError):
    """Каталог критериев некорректен."""


@dataclass(slots=True, frozen=True)
class Criterion:
    """Один критерий справочника."""

    key: str
    title: str
    category: str
    kind: str
    question: str
    evidence_hint: str
    measurable: str
    weight_hint: int
    must_have_hint: bool
    legal_note: str = ""

    @property
    def measurable_label(self) -> str:
        return MEASURABLE_LABELS.get(self.measurable, self.measurable)

    @property
    def needs_lawyer(self) -> bool:
        return bool(self.legal_note)

    def as_criteria_dict(self) -> dict[str, Any]:
        """Представление в формате ``vacancies.criteria``.

        Служебные поля каталога (``measurable``, ``legal_note``) сюда не попадают:
        в вакансии хранится только то, по чему считается балл.
        """
        return {
            "key": self.key,
            "title": self.title,
            "weight": float(self.weight_hint),
            "must_have": self.must_have_hint,
        }


@dataclass(slots=True, frozen=True)
class Category:
    key: str
    title: str
    description: str


@dataclass(slots=True, frozen=True)
class Forbidden:
    """Признак, по которому отбирать нельзя."""

    group: str
    reason: str
    aliases: tuple[str, ...] = ()


@dataclass(slots=True)
class Catalog:
    categories: dict[str, Category]
    criteria: list[Criterion]
    forbidden: list[Forbidden] = field(default_factory=list)

    def by_category(self) -> dict[str, list[Criterion]]:
        grouped: dict[str, list[Criterion]] = {key: [] for key in self.categories}
        for criterion in self.criteria:
            grouped.setdefault(criterion.category, []).append(criterion)
        return grouped

    def get(self, key: str) -> Criterion | None:
        return next((item for item in self.criteria if item.key == key), None)

    def with_legal_notes(self) -> list[Criterion]:
        return [item for item in self.criteria if item.needs_lawyer]


def _require(mapping: dict[str, Any], key: str, *, where: str) -> Any:
    if key not in mapping:
        msg = f"В критерии {where!r} нет обязательного поля {key!r}"
        raise CatalogError(msg)
    return mapping[key]


def load_catalog(path: str | Path | None = None) -> Catalog:
    """Прочитать каталог и проверить его.

    Ошибки загрузки — ``CatalogError`` с указанием места: каталог редактируется
    руками (или вместе с руководителем), и сообщение должно быть понятным без
    чтения кода.
    """
    catalog_path = Path(path) if path is not None else DEFAULT_CATALOG_PATH
    if not catalog_path.is_file():
        msg = f"Каталог критериев не найден: {catalog_path}"
        raise CatalogError(msg)

    try:
        raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        msg = f"Каталог критериев не является корректным YAML: {exc}"
        raise CatalogError(msg) from exc

    if not isinstance(raw, dict):
        msg = "Каталог критериев должен быть словарём с ключами categories и criteria"
        raise CatalogError(msg)

    categories: dict[str, Category] = {}
    raw_categories = raw.get("categories") or {}
    if not isinstance(raw_categories, dict) or not raw_categories:
        msg = "В каталоге нет секции categories"
        raise CatalogError(msg)
    for key, value in raw_categories.items():
        if not isinstance(value, dict):
            msg = f"Категория {key!r} должна быть словарём"
            raise CatalogError(msg)
        categories[key] = Category(
            key=key,
            title=str(value.get("title") or key),
            description=str(value.get("description") or ""),
        )

    guard = PolicyGuard(PolicySettings())
    criteria: list[Criterion] = []
    seen: set[str] = set()

    raw_criteria = raw.get("criteria") or []
    if not isinstance(raw_criteria, list) or not raw_criteria:
        msg = "В каталоге нет секции criteria"
        raise CatalogError(msg)

    for index, item in enumerate(raw_criteria):
        if not isinstance(item, dict):
            msg = f"Критерий #{index + 1} должен быть словарём"
            raise CatalogError(msg)
        key = str(_require(item, "key", where=f"#{index + 1}"))
        if key in seen:
            msg = f"Ключ критерия {key!r} повторяется"
            raise CatalogError(msg)
        seen.add(key)

        category = str(_require(item, "category", where=key))
        if category not in categories:
            msg = f"Критерий {key!r} ссылается на неизвестную категорию {category!r}"
            raise CatalogError(msg)

        kind = str(item.get("kind") or "skill")
        if kind not in CRITERION_KINDS:
            msg = f"Критерий {key!r}: недопустимый kind {kind!r}"
            raise CatalogError(msg)

        measurable = str(item.get("measurable") or "medium")
        if measurable not in MEASURABLE_LEVELS:
            msg = f"Критерий {key!r}: недопустимый measurable {measurable!r}"
            raise CatalogError(msg)

        weight = item.get("weight_hint", 1)
        if not isinstance(weight, int | float) or not 1 <= float(weight) <= 3:
            msg = f"Критерий {key!r}: weight_hint должен быть числом от 1 до 3"
            raise CatalogError(msg)

        criterion = Criterion(
            key=key,
            title=str(_require(item, "title", where=key)),
            category=category,
            kind=kind,
            question=str(item.get("question") or ""),
            evidence_hint=str(item.get("evidence_hint") or ""),
            measurable=measurable,
            weight_hint=round(float(weight)),
            must_have_hint=bool(item.get("must_have_hint")),
            legal_note=str(item.get("legal_note") or "").strip(),
        )
        # Тот же guard, что и у рабочей настройки вакансии: каталог не может
        # содержать дискриминационный критерий.
        guard.assert_criteria_allowed([criterion.as_criteria_dict()])
        criteria.append(criterion)

    forbidden = [
        Forbidden(
            group=str(item.get("group", "")),
            reason=str(item.get("reason", "")),
            aliases=tuple(str(alias) for alias in (item.get("aliases") or [])),
        )
        for item in (raw.get("forbidden") or [])
        if isinstance(item, dict)
    ]

    return Catalog(categories=categories, criteria=criteria, forbidden=forbidden)


def selected_criteria(catalog: Catalog, keys: list[str]) -> list[dict[str, Any]]:
    """Собрать набор критериев вакансии из выбранных ключей.

    Используется, когда критерии утверждены: на вход — список ключей, на выходе —
    готовое содержимое ``vacancies.criteria``. Неизвестный ключ — ошибка, а не
    молчаливый пропуск: опечатка не должна превращаться в «критерий не выбран».
    """
    unknown = [key for key in keys if catalog.get(key) is None]
    if unknown:
        msg = f"В каталоге нет критериев: {', '.join(unknown)}"
        raise CatalogError(msg)

    result: list[dict[str, Any]] = []
    for key in keys:
        found = catalog.get(key)
        if found is None:  # pragma: no cover - проверено выше
            continue
        result.append(found.as_criteria_dict())
    return result


def render_markdown(catalog: Catalog) -> str:
    """Собрать markdown-справочник из каталога.

    Документ генерируется, а не пишется руками: иначе при правке каталога он
    разошёлся бы с данными.
    """
    lines: list[str] = [
        "# Каталог критериев подбора",
        "",
        "> Справочник для выбора критериев под конкретную вакансию. Система ничего",
        "> не выбирает сама: итоговые веса и обязательность утверждает рекрутер.",
        ">",
        "> Файл сгенерирован из `data/criteria/catalog.yaml`:",
        "> `hirebot criteria --markdown > docs/CRITERIA-CATALOG.md`",
        "",
        "## Как читать",
        "",
        "| Поле | Значение |",
        "|---|---|",
        "| **Проверяемость** | насколько надёжно критерий виден из резюме |",
        "| **Вес** | предлагаемая важность: 1 (низкая) — 3 (высокая) |",
        "| **Обязательный** | провал даёт жёсткий потолок балла |",
        "| ⚖ | требует решения юриста до использования |",
        "",
        f"Всего критериев: **{len(catalog.criteria)}** в {len(catalog.categories)} категориях.",
        "",
    ]

    grouped = catalog.by_category()
    for category_key, category in catalog.categories.items():
        items = grouped.get(category_key, [])
        if not items:
            continue
        lines.append(f"## {category.title}")
        if category.description:
            lines.append("")
            lines.append(category.description)
        lines.append("")
        lines.append("| Ключ | Критерий | Что проверяем | Проверяемость | Вес | Обязат. |")
        lines.append("|---|---|---|---|:--:|:--:|")
        for item in items:
            flag = " ⚖" if item.needs_lawyer else ""
            must = "да" if item.must_have_hint else "—"
            lines.append(
                f"| `{item.key}` | {item.title}{flag} | {item.question} "
                f"| {item.measurable_label} | {item.weight_hint} | {must} |"
            )
        lines.append("")

    legal = catalog.with_legal_notes()
    if legal:
        lines.append("## Требуют решения юриста")
        lines.append("")
        lines.append(
            "Эти критерии законны только при определённых условиях. До подтверждения юристом не использовать."
        )
        lines.append("")
        for item in legal:
            lines.append(f"### {item.title} (`{item.key}`)")
            lines.append("")
            lines.append(item.legal_note)
            lines.append("")

    if catalog.forbidden:
        lines.append("## Чего в каталоге нет и почему")
        lines.append("")
        lines.append("| Признак | Почему нельзя |")
        lines.append("|---|---|")
        for forbidden_item in catalog.forbidden:
            lines.append(f"| {forbidden_item.group} | {forbidden_item.reason} |")
        lines.append("")

    return "\n".join(lines)


__all__ = [
    "CRITERION_KINDS",
    "DEFAULT_CATALOG_PATH",
    "MEASURABLE_LABELS",
    "Catalog",
    "CatalogError",
    "Category",
    "Criterion",
    "Forbidden",
    "load_catalog",
    "render_markdown",
    "selected_criteria",
]
