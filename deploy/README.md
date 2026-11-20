# Продакшен-развёртывание hirebot

Каталог содержит всё, что нужно для эксплуатации: compose-файл с PostgreSQL,
Redis, API, воркером и one-shot миграциями, пример reverse-proxy с TLS, шаблон
переменных окружения и пример скрейпа Prometheus.

Инструкция рассчитана на человека, который проект не писал. Инциденты
(истёк токен, 429, растущий outbox, недоступная БД) разобраны отдельно — в
[`../docs/RUNBOOK.md`](../docs/RUNBOOK.md).

## 1. Что где лежит

| Файл | Назначение |
|---|---|
| `docker-compose.prod.yml` | сервисы, healthcheck, лимиты, `read_only` |
| `.env.example` | шаблон переменных: `cp .env.example .env` |
| `nginx.conf` | TLS, security-заголовки, `/metrics` только из внутренней сети |
| `prometheus.example.yml` | пример скрейпа `/metrics` и набор алертов |
| `tls/` | сертификаты (создаётся на сервере, в git не попадает) |

## 2. Что нужно на сервере

* Docker Engine 24+ и Docker Compose v2 (`docker compose version`);
* 2 vCPU / 4 ГБ RAM минимум (API + воркер + PostgreSQL + Redis);
* домен, указывающий на сервер (для TLS);
* nginx на хосте (или профиль `proxy` в compose).

Снаружи открываются только 80 и 443. PostgreSQL и Redis не публикуются, API
доступен только на `127.0.0.1:8000`.

## 3. Секреты: как получить значения

```bash
# PII_KEY (32 байта Fernet; смена ключа делает прежние ПДн нечитаемыми)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# WEBHOOK_SECRET: он же X-API-Key для защищённых эндпоинтов и /metrics
openssl rand -hex 32

# пароли PostgreSQL/Redis (только hex: попадают в DSN и URL)
openssl rand -hex 32
```

Записываются в `deploy/.env` (права `600`, владелец — пользователь деплоя;
файл в `.gitignore`). Никогда не переиспользуйте значения из `.env.example`.

## 4. Переменные окружения

Обязательный минимум (compose падает при старте, если их нет):

| Переменная | Зачем |
|---|---|
| `HIREBOT_SECURITY__PII_KEY` | шифрование персональных данных (152-ФЗ) |
| `HIREBOT_SECURITY__WEBHOOK_SECRET` | подпись вебхуков и `X-API-Key` для API и `/metrics` |
| `POSTGRES_PASSWORD`, `REDIS_PASSWORD` | доступ к БД и брокеру задач |

Задаются compose-файлом жёстко, менять не нужно: `HIREBOT_ENV=production`,
`HIREBOT_QUEUE__BACKEND=arq`, DSN PostgreSQL и Redis. Остальное (HH, Avito,
Talantix, LLM, политики) — в `deploy/.env`.

`Settings` проверяет production сам и падает при старте, если DSN не
PostgreSQL, нет `PII_KEY` или очередь не `arq`. Отсутствие
`WEBHOOK_SECRET` `Settings` **не** ловит — поэтому в compose оно обязательно,
иначе защищённые эндпоинты молча отвечают `503`.

Лимиты площадок задаются вложенными именами:

```dotenv
HIREBOT_HH__RATE_LIMIT__REQUESTS_PER_SECOND=3
HIREBOT_HH__RATE_LIMIT__BURST=5
```

> `HIREBOT_HH__RPS` (встречается в старом `.env.example` в корне) **не
> читается**: у `HHSettings` нет поля `rps`, лишние ключи игнорируются.

## 5. Порядок первого выката

```bash
cd deploy
cp .env.example .env && chmod 600 .env && $EDITOR .env   # заполнить секреты

# 1. Сборка образа (тот же Dockerfile, что и для разработки)
docker compose -f docker-compose.prod.yml build

# 2. Инфраструктура
docker compose -f docker-compose.prod.yml up -d postgres redis
docker compose -f docker-compose.prod.yml ps        # дождаться (healthy)

# 3. Миграции (one-shot; api/worker ждут его завершения)
docker compose -f docker-compose.prod.yml run --rm migrate
docker compose -f docker-compose.prod.yml logs migrate | tail -20

# 4. Приложение
docker compose -f docker-compose.prod.yml up -d api worker

# 5. Проверки (см. раздел 6)
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/health/ready | python -m json.tool
```

`up -d api worker` сам подождёт `migrate` (`service_completed_successfully`),
но при первом выкате полезно увидеть лог миграций отдельно.

Обратный прокси (вариант «nginx на хосте»):

```bash
sudo cp nginx.conf /etc/nginx/conf.d/hirebot.conf   # server-блоки, без events/http
# в файле заменить hirebot.example.com и пути к сертификатам
sudo certbot --nginx -d hirebot.example.com
sudo nginx -t && sudo systemctl reload nginx
```

Вариант «nginx в compose»: положите сертификаты в `deploy/tls/live/<домен>/` и
запустите `docker compose -f docker-compose.prod.yml --profile proxy up -d proxy`.

## 6. Проверка готовности

| Проверка | Ожидание |
|---|---|
| `GET /health` | `200`, `{"status":"ok"}` — процесс жив, внешние системы не проверяются |
| `GET /health/ready` | `200` и `{"status":"ready"}`; `503` — БД или брокер недоступны |
| `GET /metrics` с `X-API-Key` | `200`, `text/plain`, есть `hirebot_http_requests_total` |
| `GET /metrics` без ключа | `401` |
| `docker compose ps` | `postgres`, `redis` — `healthy`; `api` — `healthy`; `worker` — `running` |

`/health/ready` различает критические и необязательные зависимости: недоступный
календарь или LLM даёт `degraded`-подробность в отчёте, но не `503`. Расширенный
отчёт подключается в `routes_health.ready` (см. раздел 9).

Логи (JSON, есть `correlation_id`):

```bash
docker compose -f docker-compose.prod.yml logs -f --tail=100 api worker
docker compose -f docker-compose.prod.yml logs api | grep '"level":"error"' | tail
```

## 7. Обновление версии

1. Соберите/получите новый образ с фиксированным тегом:
   `HIREBOT_TAG=2026.11.20-1 docker compose -f docker-compose.prod.yml build`
   (в `.env`: `HIREBOT_TAG=2026.11.20-1` — откат потом делается сменой тега).
2. `docker compose -f docker-compose.prod.yml run --rm migrate`
3. `docker compose -f docker-compose.prod.yml up -d --no-deps api worker`
4. Проверьте `/health`, `/health/ready`, `/metrics`, затем — что в outbox нет
   роста: `hirebot_outbox_entries{status="pending"}`.

Держите предыдущий тег образа до подтверждения работоспособности новой версии.

## 8. Откат

**Кода** (безопасно, данные не меняются):

```bash
# в deploy/.env: HIREBOT_TAG=<предыдущий тег>
docker compose -f docker-compose.prod.yml up -d --no-deps api worker
```

**Миграции** — только если новая миграция не удаляет и не переименовывает данные:

```bash
docker compose -f docker-compose.prod.yml run --rm migrate python -m alembic downgrade -1
docker compose -f docker-compose.prod.yml run --rm migrate python -m alembic current
```

Перед откатом миграции сделайте дамп: `docker compose exec postgres pg_dump -U hirebot hirebot > backup.sql`.
Если миграция удаляла колонку/таблицу — откат скриптом восстановит **пустую**
структуру, данные не вернутся: в этом случае откат кода делается, но миграция
остаётся, а несовместимость чинится отдельным релизом. Подробнее — RUNBOOK,
раздел «Откат миграции».

## 9. Подключение метрик и расширенной готовности в коде

В `src/hirebot/api/app.py` (делается один раз при внедрении):

```python
from hirebot.api.routes_metrics import router as metrics_router
from hirebot.observability.metrics import HttpMetricsMiddleware, setup_metrics
```

* в `lifespan`, после `configure_logging(...)`: `setup_metrics(enabled=True)`;
* в `create_app`, рядом с `app.add_middleware(RequestContextMiddleware)`:
  `app.add_middleware(HttpMetricsMiddleware)`
  (метрики ставятся **снаружи**, до контекста запроса — так учитывается и `413`);
* в `create_app`, после health-роутера: `app.include_router(metrics_router)`.

В `routes_health.ready` вместо самодельных проверок можно использовать
`hirebot.observability.health.check_dependencies(container)` — отчёт содержит
`database`, `channels`, `redis`, `calendar`, `llm` и метод `as_component()` для
каждой проверки.

## 10. Мониторинг

```bash
# Prometheus: пример конфигурации и алертов
sudo cp prometheus.example.yml /etc/prometheus/prometheus.yml
```

Минимальный набор алертов (подробнее — `prometheus.example.yml`):

* `up == 0` — API недоступен;
* `hirebot_outbox_entries{status="dead"} > 0` — задачи требуют человека;
* `hirebot_outbox_entries{status="pending"} > 100` 10 минут — очередь растёт;
* `rate(hirebot_messages_failed_total[10m]) > 0.2` — площадка не принимает сообщения;
* `increase(hirebot_domain_errors_total{error_type="AuthExpired"}[15m]) > 0` —
  истёк токен (HH/Talantix);
* `rate(hirebot_llm_calls_total{outcome="error"}[10m]) > 0.1` — LLM деградировала.

## 11. Резервное копирование

```bash
# ежедневно, вне окна выкладки
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U hirebot -Fc hirebot > /var/backups/hirebot-$(date +%F).dump

# восстановление
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_restore -U hirebot -d hirebot --clean --if-exists < /var/backups/hirebot-2026-11-20.dump
```

В `pg_dump` попадают ПДн кандидатов: бэкап хранится зашифрованным и не дольше
срока, установленного политикой хранения (152-ФЗ). Redis бэкапить не нужно:
в нём очередь и кэш, а не источник правды.
