# RUNBOOK: эксплуатация hirebot

Документ для дежурного. Это не описание архитектуры (оно в
[`ARCHITECTURE.md`](ARCHITECTURE.md)), а последовательность действий: **симптом →
проверка → действие → чего не делать**.

Все команды — из каталога `deploy/` на сервере. Сокращение для compose:

```bash
alias hb='docker compose -f docker-compose.prod.yml'
```

Переменные окружения читаются из `deploy/.env`; секреты из него не выводятся в
терминал и не копируются в тикеты.

---

## 1. Быстрая навигация

| Симптом | Куда смотреть |
|---|---|
| Сервис не отвечает, 502/504 от прокси | [Служба не отвечает](#служба-не-отвечает) |
| `/health/ready` отвечает 503 | [Служба не отвечает](#служба-не-отвечает), [БД недоступна](#5-бд-недоступна) |
| В логах `AuthExpired`, площадка отвечает 401 | [Токен hh.ru](#3-истёк-токен-hhru) / [Токен Talantix](#4-истёк-токен-talantix) |
| Площадка отвечает 429 | [429 от площадки](#6-429-от-площадки) |
| `hirebot_outbox_entries{status="dead"} > 0` | [Outbox растёт / задачи в DEAD](#7-outbox-растёт--задачи-в-dead) |
| Нет новых сообщений кандидатам, воркер «тихий» | [Redis недоступен](#8-redis-недоступен), [Outbox](#7-outbox-растёт--задачи-в-dead) |
| Диалоги массово в `human_review` | [LLM недоступна](#9-llm-недоступна) |
| Плохой релиз | [Откат деплоя](#10-откат-деплоя) |
| Подозрение на утечку персональных данных | [Инцидент с ПДн](#12-инцидент-с-пдн) |

---

## 2. Проверка живости

Три разных вопроса — три разных эндпоинта. Не путать: `/health` не проверяет
внешние системы, `/health/ready` — проверяет.

```bash
# 1. Жив ли процесс (без внешних вызовов: площадка может лежать, а сервис — работать)
curl -fsS http://127.0.0.1:8000/health

# 2. Готов ли принимать трафик (БД, брокер, каналы, календарь, LLM)
curl -sS -o /tmp/ready.json -w '%{http_code}\n' http://127.0.0.1:8000/health/ready
python -m json.tool /tmp/ready.json

# 3. Метрики (внутренние данные: только с ключом)
curl -fsS -H "X-API-Key: $HIREBOT_SECURITY__WEBHOOK_SECRET" \
  http://127.0.0.1:8000/metrics | grep -E '^(hirebot_|# TYPE hirebot_)' | head -40
```

### Метрики воркера

Часть метрик — счётчики событий (скрининги, отправленные сообщения, вызовы LLM).
Они живут в процессе, где произошли, а основная работа идёт **в воркере**.
Поэтому у воркера свой эндпоинт:

```bash
hirebot worker --metrics-port 9310
curl -fsS http://127.0.0.1:9310/metrics | grep -E '^hirebot_(screenings|messages|llm)_'
```

Gauge состояния воронки (`hirebot_conversations{state=...}`) снимается из БД при
каждом scrape `/metrics` в API — он всегда актуален, независимо от того, какой
процесс менял состояние.

Что означает каждый ответ:

* **`/health` 200** — процесс жив и обслуживает HTTP. Если он не отвечает, а
  контейнер `running` — смотрим логи и `hb ps` (healthcheck контейнера).
* **`/health/ready` 200, `"status": "ready"`** — БД отвечает, брокер доступен,
  хотя бы один канал жив.
* **`/health/ready` 503, `"status": "degraded"`** — трафик принимать нельзя.
  В теле смотрим, что именно `"ok": false`: `database`, `redis`, `channels`,
  `channel:hh`, `channel:avito`.
* **`/health/ready` 200, но в отчёте `calendar`/`llm` с `"ok": false`** —
  деградация: сервис работает, но собеседования не бронируются или диалоги
  уходят в `human_review`. Трафик не снимаем, разбираемся по разделам
  [LLM](#9-llm-недоступна) и [Talantix](#4-истёк-токен-talantix).

Состояние контейнеров и логи:

```bash
hb ps
hb logs --since 30m --tail=200 api
hb logs --since 30m --tail=300 worker
hb logs worker | grep -E '"level":"(error|warning)"' | tail -50
```

> Проверки `redis`, `calendar` и `llm` в ответе `/health/ready` появляются после
> подключения расширенного readiness (`hirebot.observability.health`,
> см. `deploy/README.md`, раздел 9). До этого там только `database` и `channels`.

Логи — JSON, каждое сообщение несёт `correlation_id`. Один инцидент ищется так:

```bash
hb logs --since 2h api worker | grep '<correlation_id>'
```

### Служба не отвечает

#### Симптом

* браузер/прокси получает `502`/`504`, `curl http://127.0.0.1:8000/health` не
  отвечает или висит;
* `hb ps api` показывает `unhealthy` или бесконечный перезапуск
  (`Restarting`), в `hb ps worker` — `Exited`.

#### Проверка

```bash
hb ps
hb logs --since 10m --tail=100 api
hb logs --since 10m --tail=100 worker
hb ps -q api | xargs -r docker inspect --format '{{.State.Status}} {{.State.ExitCode}}'
df -h                      # диск: логи и PostgreSQL
docker stats --no-stream   # память: не убил ли контейнер OOM-killer
ss -ltnp | grep 8000       # порт занят кем-то ещё?
```

Типичные причины и как отличить:

| Что в логе | Причина | Действие |
|---|---|---|
| `ValidationError`, `Некорректная конфигурация для production` | не задан `PII_KEY`, DSN не PostgreSQL или очередь не `arq` | исправить `deploy/.env`, `hb up -d --no-deps api` |
| `address already in use` | порт `8000` занят | сменить `HIREBOT_HOST_PORT` в `.env` |
| `OOMKilled` / контейнер пропадает без логов | не хватает памяти | поднять `API_MEMORY`/`WORKER_MEMORY` |
| `no space left on device` | диск полон | очистить старые образы/логи, **не** трогая `pgdata` |
| пусто, процесс жив | зависло соединение с площадкой/БД | `/health/ready`, затем разделы [5](#5-бд-недоступна)/[8](#8-redis-недоступен) |

#### Действие

```bash
hb restart api          # если конфигурация корректна и процесс просто завис
hb up -d --no-deps api  # если меняли .env
```

Если API поднялся, но `/health/ready` = `503` — это не «лечится» рестартом:
переходите к разбору по зависимостям (БД, Redis, каналы).

#### Чего НЕ делать

* Не выставляйте `HIREBOT_ENV=local`, чтобы «обойти проверку конфигурации»:
  так отключаются проверки безопасности (шифрование ПДн, ключ API).
* Не отключайте `healthcheck` контейнера и не снимайте его из compose: именно
  он не даёт прокси слать трафик в мёртвый инстанс.
* Не перезапускайте `postgres`/`redis` «заодно»: пока причина не найдена,
  второй сбой только увеличит окно недоступности.

---

## 3. Истёк токен hh.ru

HH требует OAuth-токен приложения; логин и пароль менеджера не используются и
не хранятся (п. 3.6 Условий API hh.ru).

### Симптом

* площадка отвечает `401`/`403 oauth/token_expired`;
* в логах: `api_domain_error` с `code=auth_expired`, `channel_healthcheck_failed`,
  `job_failed` с `error_type=AuthExpired`;
* `/health/ready`: `channel:hh` → `"ok": false`;
* метрика `hirebot_domain_errors_total{error_type="AuthExpired"}` растёт,
  `hirebot_responses_ingested_total{channel="hh"}` стоит на месте.

### Проверка

```bash
# что говорит readiness про канал
curl -sS http://127.0.0.1:8000/health/ready | python -m json.tool | grep -A2 'channel:hh'

# ошибки авторизации в логах за сутки
hb logs --since 24h api worker | grep -i -E 'auth_expired|AuthExpired|token_expired' | tail -20

# метрики
curl -fsS -H "X-API-Key: $HIREBOT_SECURITY__WEBHOOK_SECRET" http://127.0.0.1:8000/metrics \
  | grep -E 'domain_errors_total.*AuthExpired|channel="hh"'
```

Проверить состояние токенов в кэше (кэш важнее конфига — см. ниже):

```bash
hb exec redis redis-cli --no-auth-warning -a "$REDIS_PASSWORD" --scan --pattern 'hh:tokens:*'
```

### Действие

1. Менеджер проходит авторизацию в браузере (ссылку выдаёт приложение):

   ```bash
   hb exec -T api python - <<'PY'
   import asyncio
   from hirebot.config import get_settings
   from hirebot.container import build_container
   from hirebot.domain.enums import Channel

   async def main() -> None:
       container = await build_container(get_settings())
       try:
           manager = container.channels[Channel.HH].tokens
           print(manager.authorization_url(redirect_uri="https://<домен>/oauth/hh/callback"))
       finally:
           await container.aclose()

   asyncio.run(main())
   PY
   ```

   `redirect_uri` должен совпадать с зарегистрированным в приложении
   (`https://dev.hh.ru/admin`). После подтверждения браузер вернёт `code`.

2. Обменять код на пару токенов (сохраняется в зашифрованном кэше, поэтому
   рестарт не нужен):

   ```bash
   hb exec -T api python - <<'PY'
   import asyncio
   from hirebot.config import get_settings
   from hirebot.container import build_container
   from hirebot.domain.enums import Channel

   CODE = "<code из redirect_uri>"
   REDIRECT_URI = "https://<домен>/oauth/hh/callback"

   async def main() -> None:
       container = await build_container(get_settings())
       try:
           manager = container.channels[Channel.HH].tokens
           bundle = await manager.exchange_code(CODE, redirect_uri=REDIRECT_URI)
           print("access_token обновлён, срок:", bundle.expires_at)
       finally:
           await container.aclose()

   asyncio.run(main())
   PY
   ```

3. Проверить, что канал ожил: `curl -sS .../health/ready | grep channel:hh` →
   `"ok": true`, затем `hirebot_domain_errors_total{error_type="AuthExpired"}`
   перестаёт расти.

Если вместо OAuth-обмена токены вписываются в `deploy/.env`
(`HIREBOT_HH__ACCESS_TOKEN` / `HIREBOT_HH__REFRESH_TOKEN`), **обязательно**
удалить кэш токенов и перезапустить сервисы, иначе приложение продолжит
использовать старую пару из Redis:

```bash
hb exec -T redis sh -lc 'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" --scan --pattern "hh:tokens:*" \
  | xargs -r redis-cli --no-auth-warning -a "$REDIS_PASSWORD" DEL'
hb restart api worker
```

(Порядок загрузки в `HhTokenManager`: кэш → память → конфиг. Кэш старше конфига
намеренно: после ротации refresh-токена актуальная пара лежит именно там.)

### Что без этого ломается

* Новые отклики не забираются: `fetch_new_responses` падает, поллинг не видит
  кандидатов — они остаются в HH и ждут.
* Сообщения не уходят: `send_message`/`perform_action` требуют токен. Outbox
  копит `failed`, после `HIREBOT_QUEUE__MAX_ATTEMPTS` попыток записи становятся
  `DEAD` (см. [раздел 7](#7-outbox-растёт--задачи-в-dead)).
* Диалоги не продвигаются: задачи `process_conversation` падают.
* Avito/Talantix не затронуты: у них свои токены.

### Чего НЕ делать

* **Не обновляйте токен параллельно из двух мест.** Refresh-токен hh.ru
  одноразовый и работает только после истечения access-токена: второй запрос
  вернёт `400 invalid_grant: token has already been refreshed`, и сессия
  работодателя будет потеряна (понадобится полная переавторизация).
* Не «проактивно» обновляйте токен, пока он жив: hh.ru ответит
  `token not expired`, а приложение уже умеет это переживать.
* Не меняйте `HIREBOT_HH__CLIENT_ID`/`CLIENT_SECRET` во время инцидента — это
  сменит ключ кэша (`hh:tokens:{client_id}`) и потребует новой авторизации.
* Не переиспользуйте значения токенов из `.env.example`/тестов.
* Не пишите токены в тикеты, чаты и `audit_log`.

---

## 4. Истёк токен Talantix

Токены Talantix выдаёт **администратор** Talantix; приложение не может их
выпустить самостоятельно. Смена роли/блокировка администратора убивает все его
токены — тогда перевыпуск делает другой администратор компании.

### Симптом

* в логах `talantix_healthcheck_failed`, `api_domain_error` c
  `code=auth_expired`, `AuthExpired` в задачах бронирования;
* `/health/ready`: `calendar` → `"ok": false` (или `calendar_configured=true`,
  но брони не создаются);
* метрика `hirebot_calendar_events_total{action="created"}` перестала расти,
  `hirebot_domain_errors_total{error_type="AuthExpired"}` растёт.

### Проверка

```bash
curl -sS http://127.0.0.1:8000/health/ready | python -m json.tool | grep -A2 calendar
hb logs --since 6h worker | grep -i talantix | tail -30
# ключ содержит зашифрованную пару: значение не печатаем, достаточно факта наличия
hb exec redis redis-cli --no-auth-warning -a "$REDIS_PASSWORD" EXISTS talantix:tokens
```

### Действие

1. Запросить у администратора Talantix новую пару токенов (доступ по тарифу:
   «Оптимальный»/«Продвинутый»).
2. Вписать в `deploy/.env`: `HIREBOT_TALANTIX__ACCESS_TOKEN`,
   `HIREBOT_TALANTIX__REFRESH_TOKEN` (права `600`).
3. Удалить закэшированную пару — иначе конфиг не будет прочитан
   (`_reload`: кэш → память → конфиг):

   ```bash
   hb exec redis redis-cli --no-auth-warning -a "$REDIS_PASSWORD" DEL talantix:tokens talantix:token-refresh
   ```

4. Перезапустить сервисы: `hb restart api worker`; проверить `calendar` в
   `/health/ready` и `hirebot_domain_errors_total{error_type="AuthExpired"}`.

Пока токена нет: диалоги продолжаются, но **собеседования не бронируются** —
слоты предлагаются кандидату, а `book_interview` падает. Кандидатов, выбравших
слот, нужно вести вручную (в Talantix) — иначе они ждут в
`awaiting_slot_choice`.

### Чего НЕ делать

* **Не обновляйте токен Talantix параллельно из двух процессов и не запускайте
  второй экземпляр сервиса «на проверку»**: refresh-токен одноразовый с
  ротацией, конкурентное обновление теряет сессию. Приложение сериализует
  обновление через `LockPort` (`talantix:token-refresh`) — не обходите его
  ручными запросами к `/oauth/token`.
* Не меняйте `HIREBOT_SECURITY__PII_KEY` «на всякий случай»: токены в кэше
  зашифрованы им, после смены ключа их придётся выпускать заново.
* Не создавайте встречи вручную в обход Talantix на время инцидента: они не
  попадут в локальную таблицу `bookings`, и система предложит это же время
  другому кандидату.

---

## 5. БД недоступна

### Симптом

* `/health/ready` → `503`, `"database": {"ok": false, "detail": "..."}`;
* API на запросах, которым нужна БД, отвечает `500` (`internal_error`) или
  `503`, если БД недоступна уже на старте;
* в логах `readiness_database_failed`, `job_failed` с `error_type` из SQLAlchemy
  (`OperationalError`, `InterfaceError`, `TimeoutError`);
* метрика `hirebot_http_requests_total{status="503"}` растёт, воркер пишет
  ошибки на каждой задаче.

### Проверка

```bash
hb ps postgres
hb logs --since 30m postgres
hb exec postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"

# есть ли соединения и не кончился ли диск
hb exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT count(*), state FROM pg_stat_activity GROUP BY state;"
df -h /var/lib/docker
```

### Действие

1. Остановить обработку, чтобы не плодить `DEAD`-задачи:
   `hb stop worker` (API оставить — он вернёт 503, клиенты повторят).
2. Поднять БД: `hb start postgres`; дождаться `healthy` в `hb ps`.
3. Если причина — нехватка места или лимит соединений: освободить диск,
   уменьшить `HIREBOT_DATABASE__POOL_SIZE`, перезапустить `postgres`.
4. После восстановления: `hb start worker`, проверить `/health/ready` и что
   `hirebot_outbox_entries{status="pending"}` начинает убывать.
5. Если БД пересоздавалась — применить миграции: `hb run --rm migrate`, затем
   восстановить данные из последнего дампа (см. `deploy/README.md`, раздел 11).

### Чего НЕ делать

* Не перезапускайте `postgres` при активных записях без `stop api worker`:
  прерванная транзакция откатится, но плодящиеся ретраи усложнят разбор.
* Не гоняйте `alembic upgrade` «на всякий случай» во время инцидента с данными:
  сначала дамп, потом миграции.
* Не удаляйте `pgdata` ради «чистого старта» — это потеря всех ПДн и истории.
* Не переключайте `HIREBOT_DATABASE__DSN` на SQLite «чтобы работало»: в
  production `Settings` это запретит, а в обход — получите две разные базы.

---

## 6. 429 от площадки

### Симптом

* в логах `api_domain_error` с `code=rate_limited`,
  `outbox_task_failed` с `retry_in_s`;
* метрика `hirebot_domain_errors_total{error_type="RateLimited"}` растёт,
  `hirebot_messages_failed_total` тоже;
* диалоги «замирают» между `awaiting_answer` и следующим шагом.

### Проверка

```bash
hb logs --since 2h worker | grep -E 'RateLimited|rate_limited|retry_in_s' | tail -20
curl -fsS -H "X-API-Key: $HIREBOT_SECURITY__WEBHOOK_SECRET" http://127.0.0.1:8000/metrics \
  | grep -E 'domain_errors_total.*RateLimited|messages_failed_total|outbox_entries'
```

Отдельно уточните, **что именно** ограничено: у HH это разные вещи —
`5 сообщений подряд` в чате, суточный лимит просмотра резюме, общий RPS
приложения. В логе `api_domain_error` смотрите `error_type` и текст HTTP-ответа
площадки в `detail` (он есть только в DEBUG и только для наших сообщений).

### Действие

1. Убедиться, что дело в лимите, а не в бане: если площадка отвечает `403`
   вместо `429` — это права/тариф, а не частота.
2. Снизить исходящую частоту. Правильные имена переменных (вложенные):

   ```dotenv
   HIREBOT_HH__RATE_LIMIT__REQUESTS_PER_SECOND=1
   HIREBOT_HH__RATE_LIMIT__BURST=2
   HIREBOT_AVITO__RATE_LIMIT__REQUESTS_PER_SECOND=1
   HIREBOT_AVITO__RATE_LIMIT__BURST=2
   HIREBOT_TALANTIX__RATE_LIMIT__REQUESTS_PER_SECOND=1
   ```

   > `HIREBOT_HH__RPS` из старого `.env.example` **не читается**: поля `rps` в
   > `HHSettings` нет, лишние ключи игнорируются. Значения по умолчанию:
   > HH — 3 rps / burst 5, Avito — 1 rps / burst 5, Talantix — 2 rps / burst 4.

3. Увеличить паузу между сообщениями кандидату (джойтер):
   `HIREBOT_HH__RATE_LIMIT__MIN_SEND_DELAY_S=4`,
   `HIREBOT_HH__RATE_LIMIT__MAX_SEND_DELAY_S=8` (не больше `max`, иначе
   приложение не запустится: валидатор требует `max >= min`).
4. Перезапустить только воркер: `hb restart worker` (API лимиты не использует).
5. Проверить, что `hirebot_messages_failed_total` перестал расти, а
   `hirebot_outbox_entries{status="pending"}` убывает.

### Чего НЕ делать

* **Не «ретраить сильнее»**: ретраи без паузы увеличивают частоту и приводят к
  блокировке аккаунта работодателя. В приложении уже есть экспоненциальный
  backoff (30 с → 1 ч) и token bucket — их не надо обходить.
* Не поднимайте RPS «чтобы быстрее разгребсти очередь»: 429 не исчезнет, а
  суточная квота кончится раньше.
* Не игнорируйте `retry_after_s` из ответа: приложение учитывает его при
  повторной постановке задачи, ручной запуск — нет.
* Не отключайте проверку подписи/`X-API-Key`, чтобы «упростить» диагностику.

---

## 7. Outbox растёт / задачи в DEAD

Outbox — транзакционная очередь исходящих действий. Запись проходит
`pending → sent`; при ошибке `failed` с backoff; после
`HIREBOT_QUEUE__MAX_ATTEMPTS` попыток (по умолчанию 5) — `dead`, её разбирает
человек.

### Симптом

* `hirebot_outbox_entries{status="pending"}` растёт, `status="sent"` стоит;
* `hirebot_outbox_entries{status="dead"} > 0`;
* в логах: `outbox_task_dead` (level=error) и `outbox_task_failed` (warning) с
  `last_error`, `attempts`, `max_attempts`.

### Проверка

```bash
hb exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT status, count(*) FROM outbox GROUP BY status ORDER BY status;"

hb exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT id, topic, attempts, available_at, left(last_error, 200) AS error
     FROM outbox WHERE status IN ('dead','failed') ORDER BY attempts DESC LIMIT 20;"

# убывает ли очередь сейчас
curl -fsS -H "X-API-Key: $HIREBOT_SECURITY__WEBHOOK_SECRET" http://127.0.0.1:8000/metrics \
  | grep 'hirebot_outbox_entries'
```

Разбирать `last_error` нужно по типу:

| `last_error` содержит | Причина | Раздел |
|---|---|---|
| `AuthExpired` | токен площадки | [3](#3-истёк-токен-hhru) / [4](#4-истёк-токен-talantix) |
| `RateLimited` | 429 | [6](#6-429-от-площадки) |
| `ChannelUnavailable`, `ConnectError`, `Timeout` | площадка или сеть недоступны | ждать восстановления площадки, следить за `429` и ростом `pending` |
| `SlotConflict` | слот заняли между выбором и бронью | [раздел брони](#что-делать-с-slotconflict) |
| `ValueError` | битый payload задачи (баг) | эскалация разработчику |
| `OperationalError` | БД | [5](#5-бд-недоступна) |

### Действие

1. Сначала устранить **причину** (токен, лимит, БД). Пока причина жива,
   повторная постановка только увеличит `attempts`.
2. Убедиться, что воркер действительно работает:
   `hb ps worker`, `hb logs --since 15m worker | tail -50`. В норме каждые
   10 секунд есть `dispatch_outbox_done` (или `outbox_drained` при непустой
   очереди).
3. Перезапуск воркера безопасен в любой момент: незавершённая задача
   переотправится, а идемпотентность не даст продублировать сообщение или
   бронь:

   ```bash
   hb restart worker
   ```

4. Вернуть `dead`-записи в работу — **только после** устранения причины, по
   одной записи (чтобы контролировать эффект):

   ```sql
   BEGIN;
   UPDATE outbox
      SET status = 'pending', attempts = 0, available_at = now(), last_error = NULL
    WHERE id = '<id>' AND status = 'dead';
   COMMIT;
   ```

   Безопасность повтора: отправка сообщения идемпотентна по
   `idempotency_key`, бронь — по `bookings.idempotency_key`, а диспетчер
   дополнительно фиксирует ключ `outbox:{dedupe_key}`. Дубликата сообщения
   кандидату повтор не создаёт. Если запись помечена `sent`, но сообщение не
   дошло — это другой случай: сверьтесь с историей чата на площадке и
   отправьте сообщение вручную из UI, не меняя статус outbox.

5. Если `pending` растёт, а ошибок в логах нет — воркер не забирает пачку:
   проверьте Redis (раздел [8](#8-redis-недоступен)), права БД на таблицу
   `outbox` и что задач `dispatch_outbox` не «залипли»:
   `hb logs worker | grep dispatch_outbox | tail`.

### Чего НЕ делать

* Не делайте `UPDATE outbox SET status='sent'` «чтобы график успокоился»:
  действие не будет выполнено, а кандидат не получит ответ.
* Не удаляйте строки `outbox` и не очищайте `idempotency_keys`: пропадут
  доказательства и защита от дублей.
* Не запускайте второй воркер «для ускорения» без проверки, что первый не
  завис: параллельные `drain` безопасны, но затрудняют разбор.
* Не перезапускайте `postgres`/`redis` под нагрузкой без остановки воркера —
  получите пачку `failed` в момент рестарта.

### Что делать с SlotConflict

Слот заняли между показом и подтверждением. Это штатная ситуация: кандидату
нужно предложить другие слоты. Проверьте, что запись `bookings` не создана
(таблица `bookings`, `status` = `proposed`/`confirmed`), и предложите слоты
заново в UI. Не «дожимайте» бронь через SQL: частичный уникальный индекс
`bookings(manager_id, starts_at)` не даст создать две активные брони, а обход
индекса означает двойное собеседование.

---

## 8. Redis недоступен

Redis — кэш, распределённые локи, лимитер и **брокер задач arq**. Последнее
определяет тяжесть: без Redis воркер не получает задач.

### Симптом

* `/health/ready` → `503`, `"redis": {"ok": false, "detail": "..."}` (проверка
  критична, если `HIREBOT_QUEUE__BACKEND=arq`);
* в логах `webhook_enqueue_failed` (вебхуки отвечают `503`),
  `outbox_retry_schedule_failed`, `job_failed`;
* `hirebot_outbox_entries{status="pending"}` растёт, новых сообщений нет.

### Проверка

```bash
hb ps redis
hb exec redis redis-cli --no-auth-warning -a "$REDIS_PASSWORD" ping
hb exec redis redis-cli --no-auth-warning -a "$REDIS_PASSWORD" INFO persistence
hb logs --since 20m worker | grep -E 'redis|queue|enqueue' | tail -30
df -h   # AOF мог съесть диск
```

### Что деградирует, а что нет

* **Работает:** приём вебхуков не работает, но ручные действия в UI,
  чтение/запись БД, уже выполняющиеся задачи — да.
* **Не работает:** постановка задач в очередь (`webhook_enqueue_failed` →
  `503`), cron-поллинг площадок, диспетчер outbox (сообщения не уходят),
  конкурентные локи, распределённый rate limit, кэш ответов LLM, обновление
  токенов hh/Talantix (кэш токенов тоже в Redis).
* При недоступном Redis **на старте** приложение деградирует до `MemoryCache`
  и in-process лимитера; при падении Redis **после** старта такие вызовы
  начинают падать в ошибку.

### Действие

1. Остановить воркер, чтобы не копить ошибки: `hb stop worker`.
2. Поднять Redis: `hb start redis` → `ping` → `hb ps redis` (`healthy`).
3. Если причина — AOF/диск: освободить место; **не** удаляйте
   `/data/appendonly.aof`, если в очереди есть необработанные задачи.
4. Запустить воркер: `hb start worker`. Проверить, что задачи уходят:
   `hirebot_outbox_entries{status="pending"}` убывает.
5. Переотправить задачи, потерянные за время простоя: вебхуки площадки
   повторят сами (они получили `503`); задачи, поставленные в момент падения,
   могли не сохраниться — сверьте список новых откликов на площадке с БД
   (`conversations.created_at` за период инцидента).

### Чего НЕ делать

* Не переключайтесь на `HIREBOT_QUEUE__BACKEND=inline` в продакшене «на время»:
  задача выполнится внутри HTTP-запроса, время ответа вырастет, а после
  возврата `arq` часть задач потеряется.
* Не отключайте `HIREBOT_REDIS__REQUIRED`, чтобы readiness перестал отвечать
  `503`: вы потеряете сигнал о том, что воронка стоит.
* Не запускайте `redis-cli FLUSHALL`: это очередь и кэш, включая
  `idempotency_keys`-дубли и токены.
* Не публикуйте Redis наружу и не отключайте `--requirepass` ради диагностики.

---

## 9. LLM недоступна

### Симптом

* `/health/ready`: `llm` → `"ok": false` (некритично — трафик не снимается);
* в логах `job_failed` с `error_type=LlmError`, `llm_error`;
* метрика `hirebot_llm_calls_total{outcome="error"}` растёт, задержка
  `hirebot_llm_latency_seconds` растёт или пропадает;
* новые диалоги не проходят скрининг, часть уходит в `human_review`.

### Проверка

```bash
hb logs --since 1h api worker | grep -E 'LlmError|llm_' | tail -30
curl -fsS -H "X-API-Key: $HIREBOT_SECURITY__WEBHOOK_SECRET" http://127.0.0.1:8000/metrics \
  | grep -E 'llm_calls_total|llm_latency_seconds_count|llm_cache_events_total'
hb exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT state, count(*) FROM conversations GROUP BY state ORDER BY 2 DESC;"
```

Проверить настройки: `HIREBOT_LLM__PROVIDER`, `HIREBOT_LLM__BASE_URL`,
`HIREBOT_LLM__API_KEY` (не выводить значение в терминал), лимиты/квоту
провайдера — в его консоли.

### Что происходит с диалогами

`LlmError` не теряет кандидата: диалог переводится в `HUMAN_REVIEW` — шаг
воронки не выполняется автоматически, и рекрутёр завершает его вручную в UI.
Состояние `SCREENED` без результата скрининга тоже уходит в `human_review`
(`human_review_no_screening`). Уже отправленные сообщения не отзываются.

### Действие

1. Уточнить у провайдера статус (инцидент, квота, ключ). Если ключ отозван —
   выпустить новый, вписать в `deploy/.env`, перезапустить `hb restart api worker`.
2. Если провайдер деградировал надолго — временно переключить модель/эндпоинт
   (`HIREBOT_LLM__MODEL`, `HIREBOT_LLM__BASE_URL`) на резервный OpenAI-совместимый.
3. Разобрать очередь `human_review`: список в UI (`/ui`), по нему — решение
   рекрутёра. Не оставляйте кандидатов в этом состоянии дольше суток.
4. Когда LLM восстановилась: убедиться, что `outcome="error"` перестал расти и
   `hirebot_screenings_total` увеличивается.

### Чего НЕ делать

* Не переключайте `HIREBOT_LLM__PROVIDER=fake` в продакшене: детерминированная
  заглушка выдаст вердикты «как получится», и кандидаты получат необоснованные
  оценки (а следом — юридические риски).
* Не включайте `HIREBOT_LLM__SEND_PII=true`, чтобы «модель лучше понимала
  резюме»: это трансграничная передача ПДн (152-ФЗ, ст. 12).
* Не «дожимайте» скрининг повторами в цикле: `LlmError` — не транзиентная
  ошибка сети, повторы жгут квоту.
* Не удаляйте записи `screenings` и не правьте баллы руками: результаты —
  доказательство решения; корректировка делается новым скринингом.

---

## 10. Откат деплоя

Откат **кода** безопасен: миграции пишутся так, чтобы старая версия кода
продолжала работать со схемой (новые колонки — nullable/с дефолтом).

### Симптом

* после выката растёт `hirebot_http_requests_total{status=~"5.."}`;
* readiness отвечает `503` дольше пары минут;
* в логах новые `error`-типы, которых не было до релиза.

### Проверка

```bash
hb ps
curl -sS -o /tmp/ready.json -w '%{http_code}\n' http://127.0.0.1:8000/health/ready
python -m json.tool /tmp/ready.json
hb logs --since 30m api worker | grep '"level":"error"' | tail -30
curl -fsS -H "X-API-Key: $HIREBOT_SECURITY__WEBHOOK_SECRET" http://127.0.0.1:8000/metrics | grep status=
```

Проверить, какая версия запущена: `docker inspect --format '{{.Config.Image}}' $(hb ps -q api)`,
и какая ревизия схемы: `hb run --rm migrate python -m alembic current`.

### Действие

```bash
# 1. В deploy/.env вернуть предыдущий тег
#    HIREBOT_TAG=2026.11.19-1
# 2. Перезапустить приложение, не трогая БД и миграции
hb up -d --no-deps api worker
# 3. Проверить
hb ps api
curl -fsS http://127.0.0.1:8000/health
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/health/ready
```

Если миграции новой версии **несовместимы** со старой (редкий случай:
удаление/переименование колонок), порядок обратный: сначала откат кода
опасается схемы, поэтому действуйте по разделу [11](#11-откат-миграции) и не
запускайте API до завершения отката схемы.

### Чего НЕ делать

* Не откатывайте одновременно и код, и миграции «на всякий случай»: два
  изменения сразу не дают понять, что помогло.
* Не удаляйте образ предыдущей версии до подтверждения работоспособности.
* Не откатывайтесь на версию, которая не умеет читать текущую схему (сверьте
  `alembic history` и заметки к релизу).
* Не отключайте алерты, чтобы «не мешали» во время отката.

---

## 11. Откат миграции

`alembic downgrade` — операция **над данными**, а не над кодом. Сначала дамп:

```bash
hb exec -T postgres pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > /var/backups/hirebot-before-downgrade-$(date +%F-%H%M).dump
```

### Когда это безопасно

* миграция только добавляла объекты (таблицы/индексы/колонки) и данные в них
  ещё не писались;
* есть свежий дамп, и он проверен (`pg_restore --list` читается);
* API и воркер остановлены, трафик не идёт.

```bash
hb stop api worker
hb run --rm migrate python -m alembic current   # какая ревизия сейчас
hb run --rm migrate python -m alembic history   # что откатывается
hb run --rm migrate python -m alembic downgrade -1
hb run --rm migrate python -m alembic current
# затем вернуть код предыдущего тега и запустить
hb up -d api worker
```

### Когда это опасно (и делать нельзя)

* миграция удаляла или переименовывала колонку, а данные уже перенесены:
  `downgrade` воссоздаст **пустую** колонку — данные не вернутся;
* `downgrade base` на рабочей базе: удаляются все таблицы (это допустимо
  только на одноразовой/тестовой БД);
* после отката уже поработала новая версия и записала данные в новой форме;
* нет дампа или он не проверен.

Порядок «сначала дамп, потом откат, и только потом код» — единственный
безопасный. Если данных жалко, а миграция разрушительная: **не** откатывать
схему, оставить новую и выпустить исправление кода.

### Чего НЕ делать

* Не откатывайте миграции при работающих API/воркере.
* Не редактируйте таблицу `alembic_version` руками: сломается история ревизий.
* Не запускайте `alembic upgrade head` на базе, где версия «не совпадает» —
  сначала `alembic current`/`history` и разбор.
* Не используйте `alembic stamp` для «исправления» рассинхрона на проде.

---

## 12. Инцидент с ПДн

ПДн — данные кандидатов: ФИО, контакты, резюме. Они шифруются полем
`HIREBOT_SECURITY__PII_KEY` и не попадают в логи (там только типы ошибок и
идентификаторы). Инцидент = подозрение на доступ/утечку/потерю
конфиденциальности.

### Признаки

* неизвестный доступ к `/ui`/административным эндпоинтам, всплеск `401` или
  успешных запросов с чужих адресов;
* утечка `HIREBOT_SECURITY__PII_KEY`/`WEBHOOK_SECRET` (попал в git, чат, лог);
* отправка ПДн во внешнюю модель при `HIREBOT_LLM__SEND_PII=true`;
* потеря/порча данных: удалённый дамп, `DROP`/`TRUNCATE` без бэкапа.

### Что сделать в первые минуты

1. **Зафиксировать факты, ничего не «прибирая»**: время обнаружения и источник
   сигнала, список затронутых сущностей, `correlation_id` подозрительных
   запросов, выгрузку `audit_log` за окно инцидента.

   ```bash
   hb exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" > /var/backups/audit-$(date +%F-%H%M).sql \
     -c "SELECT created_at, actor, action, entity_type, entity_id, correlation_id
           FROM audit_log WHERE created_at >= now() - interval '24 hours' ORDER BY created_at;"
   ```

   `audit_log` — append-only журнал действий: `actor`, `action`, `entity_type`,
   `entity_id`, `payload` (без ПДн), `created_at`, `correlation_id`. Именно он
   отвечает на вопрос «кто и что сделал».

2. **Ограничить доступ**: отозвать скомпрометированный `X-API-Key`
   (заменить `HIREBOT_SECURITY__WEBHOOK_SECRET`, перезапустить `api`), закрыть
   публикацию `/ui` на прокси (`deploy/nginx.conf`, `allow`/`deny`).
3. **Остановить обработку**, если утечка идёт через каналы/LLM:
   `hb stop worker`, при необходимости `HIREBOT_LLM__SEND_PII=false` и
   `HIREBOT_LLM__PROVIDER=fake` до выяснения.
4. **Уведомить**: ответственного за обработку ПДн (DPO) и юриста —
   незамедлительно. По 152-ФЗ (в редакции 266-ФЗ) уведомление Роскомнадзора
   направляется в течение 24 часов с момента обнаружения инцидента; сроки и
   форму согласовать с юристом. При утечке через внешнюю модель — отдельно
   оценить трансграничную передачу.
5. **Не менять `PII_KEY` стихийно.** Смена ключа делает ранее зашифрованные
   ПДн нечитаемыми: кандидаты «исчезнут», а восстановление потребует отдельной
   процедуры расшифровки старым ключом. Смена ключа — только плановая ротация
   с миграцией данных и дампом.

### Что НЕ делать

* Не удаляйте контейнеры/логи/тома «чтобы скрыть следы» — это уничтожение
  доказательств.
* Не отправляйте дампы и выгрузки `audit_log` в общие чаты и тикеты.
* Не пересылайте `PII_KEY`, токены и пароли даже «своим» в мессенджере.
* Не отвечайте кандидатам/СМИ от лица компании: коммуникация — через
  ответственного за инцидент.
* Не запускайте `downgrade base`, `FLUSHALL` и восстановление дампа на рабочей
  БД, пока не согласована процедура с DPO.

### После инцидента

* разбор в течение 5 рабочих дней: причина, что сработало/не сработало,
  какие алерты добавить;
* проверить, что новых записей в `audit_log` от скомпрометированного `actor`
  нет;
* ротация всех секретов, которые могли быть затронуты (`WEBHOOK_SECRET`,
  токены площадок, LLM key), с проверкой после каждой смены.

---

## Приложение A. Что означают ошибки API

| HTTP | `code` | Что значит | Раздел |
|---|---|---|---|
| 401 | `http_error` | нет/неверный `X-API-Key` | [12](#12-инцидент-с-пдн) при аномалиях |
| 422 | `validation_error` | некорректный запрос | — |
| 429 | `rate_limited` | площадка ограничила частоту | [6](#6-429-от-площадки) |
| 502 | `auth_expired` | нужна переавторизация | [3](#3-истёк-токен-hhru), [4](#4-истёк-токен-talantix) |
| 502 | `llm_error` | ошибка модели | [9](#9-llm-недоступна) |
| 503 | `channel_unavailable` | площадка/сеть недоступны | [6](#6-429-от-площадки), [8](#8-redis-недоступен) |
| 503 | `channel_not_configured` | канал выключен/нет токена | [3](#3-истёк-токен-hhru) |
| 409 | `slot_conflict` | слот занят | [7](#7-outbox-растёт--задачи-в-dead) |
| 500 | `internal_error` | необработанная ошибка (`error_id` — ключ к логу) | эскалация |

## Приложение B. Ключи Redis, которые важно знать

| Ключ | Что там | Трогать? |
|---|---|---|
| `hh:tokens:{client_id}` | зашифрованная пара токенов hh.ru | только при переавторизации |
| `hh:token-refresh:{client_id}` | лок single-flight обновления | нет |
| `talantix:tokens` | зашифрованная пара токенов Talantix | только при перевыпуске |
| `talantix:token-refresh` | лок обновления | нет |
| `webhook:{channel}:{event}` | дедупликация вебхуков (TTL 24 ч) | нет |
| `hirebot:job-dedupe:*` | дедупликация задач очереди | нет |
| `arq:*` | очередь arq | нет (удаление = потеря задач) |

## Приложение C. Полезные запросы

```sql
-- диалоги, требующие человека
SELECT id, state, updated_at FROM conversations
 WHERE state = 'human_review' ORDER BY updated_at LIMIT 50;

-- записи outbox, которые «застряли» дольше часа
SELECT id, topic, status, attempts, available_at, left(last_error, 120)
  FROM outbox
 WHERE status IN ('pending','failed') AND available_at < now() - interval '1 hour'
 ORDER BY available_at LIMIT 50;

-- активные брони на ближайшие дни
SELECT manager_id, starts_at, ends_at, status FROM bookings
 WHERE status IN ('proposed','confirmed') AND starts_at > now()
 ORDER BY starts_at LIMIT 50;

-- размер таблиц (когда messages/audit_log растут)
SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS size
  FROM pg_catalog.pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;
```

## Приложение D. Метрики и что по ним смотреть

Все метрики — с префиксом `hirebot_`. Значения меток — только фиксированные
перечисления (канал, состояние, вердикт, тип), поэтому число серий не растёт
вместе с числом кандидатов.

```bash
curl -fsS -H "X-API-Key: $HIREBOT_SECURITY__WEBHOOK_SECRET" http://127.0.0.1:8000/metrics
```

| Метрика | Тип | Что показывает | На что смотреть |
|---|---|---|---|
| `hirebot_http_requests_total{method,route,status}` | counter | запросы API | рост `status="5xx"` — сбой; `route` — шаблон (`/ui/conversations/{id}`), не URL |
| `hirebot_http_request_duration_seconds{method,route}` | histogram | длительность запросов | `_bucket{le="1"}` падает — API тормозит |
| `hirebot_responses_ingested_total{channel}` | counter | принятые отклики | стоит на месте при живом канале — поллинг не работает |
| `hirebot_conversations{state}` | gauge | диалоги по состояниям | рост `human_review` — нужны люди; рост `new` — воркер не успевает |
| `hirebot_screenings_total` | counter | скрининги | не растёт при потоке откликов — LLM/скрининг сломан |
| `hirebot_screening_verdicts_total{verdict}` | counter | распределение вердиктов | резкий сдвиг — проверьте промпт/модель |
| `hirebot_screening_score` | histogram | распределение балла 0..100 | смещение к 0 — критерии вакансии или модель |
| `hirebot_messages_sent_total{channel,kind}` | counter | отправленные сообщения | — |
| `hirebot_messages_failed_total{channel,kind}` | counter | неудачные отправки | рост — 429/токен/сеть (разделы [3](#3-истёк-токен-hhru), [6](#6-429-от-площадки)) |
| `hirebot_llm_calls_total{provider,outcome}` | counter | вызовы модели | рост `outcome="error"` — LLM (раздел [9](#9-llm-недоступна)) |
| `hirebot_llm_cache_events_total{provider,event}` | counter | кэш ответов модели | `hit/miss` — сколько платим повторно; промахов много при смене промпта |
| `hirebot_llm_latency_seconds{provider}` | histogram | задержка модели | растёт — провайдер деградировал |
| `hirebot_calendar_events_total{action}` | counter | встречи `created`/`cancelled` | `created` стоит — брони не создаются (Talantix, раздел [4](#4-истёк-токен-talantix)) |
| `hirebot_calendar_slot_conflicts_total` | counter | конфликты слотов | растёт — кандидаты конкурируют за слоты, нужен запас |
| `hirebot_outbox_entries{status}` | gauge | outbox по статусам | `pending` растёт или `dead > 0` — раздел [7](#7-outbox-растёт--задачи-в-dead) |
| `hirebot_domain_errors_total{error_type}` | counter | доменные ошибки | `AuthExpired`, `RateLimited`, `LlmError` — по разделам выше |

Быстрые запросы к Prometheus (если он уже скрейпит `/metrics`):

```promql
# воронка за сутки
increase(hirebot_responses_ingested_total[24h])
sum by (state) (hirebot_conversations)
# качество: доля сильных вердиктов
sum(rate(hirebot_screening_verdicts_total{verdict="strong_match"}[1h]))
  / sum(rate(hirebot_screening_verdicts_total[1h]))
# что требует вмешательства прямо сейчас
hirebot_outbox_entries{status="dead"} > 0
rate(hirebot_messages_failed_total[10m]) > 0
increase(hirebot_domain_errors_total{error_type="AuthExpired"}[15m]) > 0
```
