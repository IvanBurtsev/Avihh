"""Эндпоинт метрик Prometheus: ``GET /metrics``.

Доступ
------
Метрики — **внутренние данные**: имена маршрутов, размер очереди, доля
вердиктов. По ним можно понять нагрузку и слабые места системы, поэтому
публично они не отдаются никогда. Эндпоинт закрыт тем же ``X-API-Key``, что и
административное API (``hirebot.api.deps.require_api_key``):

* ключа нет или он неверный → ``401``;
* секрет не сконфигурирован в нелокальном окружении → ``503`` (как у
  остальных защищённых эндпоинтов: небезопасная конфигурация не должна
  молчаливо открывать API);
* сбор метрик выключен (``setup_metrics(enabled=False)``) → ``503``.

Снаружи эндпоинт всё равно должен быть закрыт reverse-proxy: см.
``deploy/nginx.conf`` (локейшн ``/metrics`` разрешён только из внутренней сети
и по ``X-API-Key``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from hirebot.api.deps import ContainerDep, require_api_key
from hirebot.observability.gauges import refresh_business_gauges
from hirebot.observability.metrics import metrics_enabled, metrics_endpoint

router = APIRouter(tags=["observability"])


@router.get(
    "/metrics",
    summary="Метрики Prometheus (только для внутреннего доступа)",
    include_in_schema=False,
    dependencies=[Depends(require_api_key)],
    responses={
        401: {"description": "Неверный или отсутствующий заголовок X-API-Key"},
        503: {"description": "Сбор метрик выключен в этой конфигурации"},
    },
)
async def metrics(container: ContainerDep) -> Response:
    """Отдать метрики в формате Prometheus (``text/plain``).

    Перед отдачей обновляются бизнес-gauge'и (диалоги по состояниям) прямо из
    БД. Это осознанное решение: реестр метрик живёт в процессе, и воркер,
    который меняет состояние диалогов, не сможет «прислать» их в API. Снимать
    состояние при scrape (pull-модель Prometheus) — единственный способ показать
    реальную картину без Pushgateway.

    ``Cache-Control: no-store`` обязателен: метрики меняются на каждом
    запросе, и прокси не должен отдавать закэшированный снимок.
    """
    if not metrics_enabled():
        return Response(
            content="Сбор метрик выключен в этой конфигурации (setup_metrics(enabled=False))\n",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            media_type="text/plain; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    await refresh_business_gauges(container)
    payload, content_type = metrics_endpoint()
    return Response(
        content=payload,
        media_type=content_type,
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["router"]
