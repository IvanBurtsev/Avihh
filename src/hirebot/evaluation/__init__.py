"""Подсистема измерения качества скрининга.

Пакет отвечает на вопрос, который нельзя закрыть примерами в README: насколько
хорошо скрининг принимает решения на **размеченной** выборке и где именно он
ошибается.

Состав:

* :mod:`hirebot.evaluation.dataset` — формат и загрузка эталонной выборки;
* :mod:`hirebot.evaluation.metrics` — матрица ошибок, метрики и отчёт;
* :mod:`hirebot.evaluation.runner` — прогон выборки через настоящий
  ``ScreeningService`` с точкой подмены LLM-контейнера.

Базовый прогон идёт на fake-провайдере: без сети, ключей и Docker. Это проверка
самого harness и **нижняя граница** качества, а не точность продукта: реальная
модель может ошибаться иначе, но не может «обойти» пересчёт балла и правила
вердикта, потому что они живут на нашей стороне.
"""

from hirebot.evaluation.dataset import GoldenCase, GoldenDataset, GoldenDatasetError, load_dataset
from hirebot.evaluation.metrics import (
    BinaryMetrics,
    CaseOutcome,
    EvaluationReport,
    binary_metrics,
)
from hirebot.evaluation.runner import (
    ContainerFactory,
    EvaluationError,
    build_case_container,
    case_conversation_external_id,
    run_evaluation,
)

__all__ = [
    "BinaryMetrics",
    "CaseOutcome",
    "ContainerFactory",
    "EvaluationError",
    "EvaluationReport",
    "GoldenCase",
    "GoldenDataset",
    "GoldenDatasetError",
    "binary_metrics",
    "build_case_container",
    "case_conversation_external_id",
    "load_dataset",
    "run_evaluation",
]
