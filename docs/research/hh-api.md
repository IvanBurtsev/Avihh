# HH.ru (HeadHunter) Employer API — исследовательские заметки

Дата сбора: 2026-11-20. Собрано для задачи «рекрутинг-автоматизация на официальном Employer API HH.ru».

## 0. Как собирались данные и статус проверки

* `web_search` недоступен (HTTP 402), поэтому всё собрано через `web_fetch` по известным/угаданным URL
  (raw.githubusercontent.com, api.github.com, dev.hh.ru, feedback.hh.ru, hh.ru) и по ссылкам внутри найденных страниц.
* Основной «движок» документации HH — OpenAPI: `https://api.hh.ru/openapi/redoc`, спека
  `https://api.hh.ru/openapi/specification/public`. **Спека отдаётся с `Content-Type: application/x-yaml`,
  и её не удалось прочитать напрямую** (`web_fetch` отклоняет этот content-type; зеркало на GitHub
  весит ~1.2 МБ и обрезается лимитом выдачи). Поэтому часть фактов по «новым» разделам (Chats API, Webhook API)
  подтверждена только по именам операций из сгенерированного из спеки Go-клиента и по оглавлению README.
  Все такие места помечены **[НЕ ПРОВЕРЕНО ПО ПЕРВОИСТОЧНИКУ]**.
* Значительная часть markdown-документации в репозитории `hhru/api` (master) заменена на заглушки
  вида «Данный метод доступен в OpenAPI». Полные тексты (с методами, путями, параметрами и полями ответов)
  взяты из **исторических ревизий** файлов через GitHub raw по конкретному SHA. Такие места помечены
  ссылкой на ревизию и дату.
* Явно помечаю: **ФАКТ** — есть в документации; **ВЫВОД** — моя интерпретация; **[НЕ ПРОВЕРЕНО]** — не найдено/не подтверждено.

### Источники (что реально читалось)

| Источник | Ревизия / дата | Что дал |
|---|---|---|
| `raw.githubusercontent.com/hhru/api/master/README.md` | master (обновл. 2025-12-19) | оглавление API, наличие Webhook API, пометки об устаревании методов сообщений |
| `docs/authorization.md` (master) | master | OAuth 2.0, два уровня авторизации, `Authorization: Bearer`, `/me` |
| `docs/authorization_for_application.md` | SHA `b2e70d2` (2019-05-21) | `client_credentials`, «неограниченный срок жизни», не чаще 1 раза в 5 минут |
| `docs/authorization_for_user.md` | SHA `b2e70d2` (2019-05-21) | полный `authorization_code`-флоу, `redirect_uri`, `expires_in: 1209600`, правила refresh, `force_login`/`skip_choose_account` |
| `docs/errors.md` (master) | master | коды ошибок OAuth, `bad_user_agent`, ошибки резюме и переписки |
| `docs/general.md` | SHA `cfbdaf3` (последняя версия файла, до удаления 2024-02-13) | требования к `User-Agent`/`HH-User-Agent`, пагинация, CORS, gzip |
| `docs/cache.md` (master) | master | `Etag`/`If-None-Match`, `Cache-Control`, `Expires`, 304 |
| `docs/FAQ.md` (master) | master | срок рассмотрения заявки на приложение, пошаговый сценарий приглашения |
| `docs/employer_negotiations.md` | SHA `e0dbe37` (2023-02-02) | полные пути/параметры/поля откликов, сообщений, действий |
| `docs/employer_negotiations.md` (зеркало `oshliaer/hh_api_docs`, master) | зеркало актуальнее, чем обрезанный master hhru | поисковые параметры списка откликов, `generated_collections`, пометка об устаревании сообщений |
| `docs/employer_resumes.md` | SHA `69cdfd2` (2022-05-19) | полная модель резюме (опыт, навыки, образование, зарплата, регион), `actions`, `hidden_fields` |
| `docs/payable/resume.md` (master) | master | платный доступ к базе резюме, списание контактов |
| `docs/employer_managers.md`, `docs/employer_negotiations_statistics.md`, `docs/employer_vacancies.md` (master) | master | заглушки + оглавление операций |
| `docs/me.md` | SHA `b2e70d2` (2019-05-21) | `GET /me`, объекты `employer`/`manager`/`counters` |
| `https://dev.hh.ru/admin/developer_agreement` | на 2026-11-20 | «Условия использования сервиса API» (политика, п. 4.9 и др.) |
| `https://feedback.hh.ru/knowledge-base/article/1290` | статья обновл. 2026-10-28 | правила чатов, архив 180 дней, системные сообщения |
| `https://github.com/Zoomish/go-hhru-api` (третья сторона) | 2026-05-12/2026-08 | клиент, сгенерированный из публичной спеки: подтверждает обязательный `HH-User-Agent`, ретраи на 429/503, **имена операций Chats/Webhook API** |

---

## 1. AUTH — как аутентифицируется приложение работодателя

### 1.1. Два уровня авторизации (ФАКТ)

`docs/authorization.md`:

> «API поддерживает следующие уровни авторизации: авторизация приложения; авторизация пользователя»
> «Приложение должно использовать полученный `access_token` для авторизации, передавая его в заголовке в формате:
> `Authorization: Bearer ACCESS_TOKEN`»

* **Авторизация приложения** (`client_credentials`) — 2-legged. Токен принадлежит приложению, не работодателю.
  Для методов работодателя (вакансии, отклики, резюме, чаты, вебхуки) **недостаточно**: нужен токен менеджера.
  Ошибка при попытке — `403 oauth / user_auth_expected`: «выполняется запрос с авторизацией приложения,
  для выполнения которого необходима авторизация пользователя» (`docs/errors.md`).
* **Авторизация пользователя** (`authorization_code`) — 3-legged. Именно её использует employer-приложение:
  менеджер работодателя логинится на hh.ru и нажимает «разрешить» доступ приложению.

**ВЫВОД:** для продукта рекрутинг-автоматизации нужен только `authorization_code` (3-legged) + хранение
refresh-токена на каждого работодателя/менеджера. `client_credentials` нужен лишь для анонимно-приложенческих
методов (справочники и т.п.).

### 1.2. client_credentials (токен приложения) — ФАКТ

Из `docs/authorization_for_application.md` (ревизия 2019-05-21; в master этот файл заменён ссылкой на OpenAPI,
но `docs/errors.md` master по-прежнему содержит соответствующие ошибки, т.е. поведение актуально):

```
POST https://hh.ru/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id={client_id}&client_secret={client_secret}
```

Ответ: `{"access_token": "...", "token_type": "bearer"}` — **без** `refresh_token` и `expires_in`.

Цитаты:

> «Данный `access_token` имеет **неограниченный** срок жизни. При повторном запросе ранее выданный токен
> отзывается и выдается новый. Запрашивать `access_token` можно не чаще, чем один раз в 5 минут.»

Ошибки: `400 invalid_request/invalid_client`; `403 forbidden / app token refresh too early` — «Возникает, если
запрашивать token приложения чаще чем раз в пять минут» (`docs/errors.md`).

Где взять/посмотреть: `https://dev.hh.ru/admin` — «Владелец приложения может посмотреть актуальный
`access_token` для приложения на сайте https://dev.hh.ru/admin».

### 1.3. authorization_code (токен менеджера работодателя) — ФАКТ

Шаг 1. Отправить менеджера на страницу авторизации (`docs/authorization_for_user.md`, ревизия 2019-05-21):

```
https://hh.ru/oauth/authorize?
  response_type=code&
  client_id={client_id}&
  state={state}&
  redirect_uri={redirect_uri}
```

* `response_type=code`, `client_id` — обязательные.
* `state` — вернётся в редиректе (защита от CSRF).
* `redirect_uri` — необязательный; если не указан, берётся из настроек приложения; при передаче валидируется.
* `skip_choose_account=true` — не показывать выбор аккаунта, сразу редирект с кодом.
* `force_login=true` — всегда показывать форму логина/пароля.

Шаг 2. **Здесь работодатель и нажимает «разрешить».** Цитата:

> «Если пользователь не авторизован на сайте, ему будет показана форма авторизации на сайте. После прохождения
> авторизации на сайте, пользователю будет выведена форма с запросом разрешения доступа вашего приложения
> к его персональным данным. Если пользователь не разрешает доступ приложению, пользователь будет
> перенаправлен на указанный `redirect_uri` с `?error=access_denied` и `state={state}``.
> «Если пользователь авторизован на сайте и доступ данному приложению однажды ранее выдан, ответом будет
> сразу … редирект с `authorization_code` (без показа формы логина и выдачи прав).»

Редирект при успехе: `HTTP/1.1 302 FOUND` → `{redirect_uri}?code={authorization_code}`.

Правила `redirect_uri` (важно для настройки приложения): разрешены поддомен, уточнение пути, доп. query-параметр;
**запрещены** другой протокол (http↔https), другой домен, другой путь, указание порта, которого не было в настройках.

Шаг 3. Обмен кода на токены (server-to-server):

```
POST https://hh.ru/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code&client_id=...&client_secret=...&redirect_uri=...&code=...
```

Ответ:

```json
{"access_token":"...","token_type":"bearer","expires_in":1209600,"refresh_token":"..."}
```

* **Время жизни access-токена:** `expires_in: 1209600` секунд = **14 суток** (значение из ревизии 2019-05-21;
  текущее значение в OpenAPI не подтверждено — **[НЕ ПРОВЕРЕНО]**, но `errors.md` master по-прежнему ссылается
  на поле `expires_in`).
* `authorization_code` — «имеет довольно короткий срок жизни», одноразовый
  (`errors.md`: `code has already been used`, `code expired`, `code was revoke`).

Шаг 4. Refresh:

```
POST https://hh.ru/oauth/token
grant_type=refresh_token&refresh_token={refresh_token}
```

Жёсткие правила (ФАКТ, цитаты):

> «`refresh_token` можно использовать только один раз и только по истечению срока действия `access_token`.»
> «После получения новой пары access и refresh токенов, их необходимо использовать в дальнейших запросах.»

Ошибки (`docs/errors.md`):

| HTTP | error | error_description | смысл |
|---|---|---|---|
| 400 | `invalid_grant` | `token has already been refreshed` | refresh-токен использован второй раз |
| 400 | `invalid_grant` | `token not expired` | попытка обновить ещё живой access-токен |
| 400 | `invalid_grant` | `token was revoked` / `token deactivated` | отозван; деактивируется после смены пароля |
| 400 | `invalid_grant` | `code has already been used` / `code expired` / `code was revoke` | про `authorization_code` |
| 403 | `oauth` | `token_expired` | access истёк — нужно обновление |
| 403 | `oauth` | `token_revoked` | токен отозван пользователем, нужна новая авторизация |
| 403 | `oauth` | `bad_authorization` | токен не существует/невалиден |
| 403 | `oauth` | `application_not_found` | приложение удалено |
| 403 | `oauth` | `user_auth_expected` | запрос с токеном приложения там, где нужен пользователь |

**ВЫВОД (важно для интеграции):** «обновлять только после истечения» + «refresh одноразовый» означает, что
нельзя делать проактивный refresh; нужно либо ждать 403 `token_expired`, либо хранить время истечения и
обновляться после него, при этом параллельные обновления должны быть сериализованы (иначе второй запрос
получит `token has already been refreshed` и потеряет сессию).

### 1.4. Требования к `User-Agent` / `HH-User-Agent` — ФАКТ

`docs/general.md` (последняя версия файла, SHA `cfbdaf3`; раздел «Требования к запросам»):

> «В запросе необходимо передавать заголовок `User-Agent`, но если ваша реализация http клиента не позволяет,
> можно отправить `HH-User-Agent`. Если не отправлен ни один заголовок, то ответом будет `400 Bad Request`.
> Указание в заголовке названия приложения и контактной почты разработчика позволит нам оперативно с вами
> связаться в случае необходимости. Заголовки `User-Agent` и `HH-User-Agent` взаимозаменяемы, в случае, если вы
> отправите оба заголовка, обработан будет только `HH-User-Agent`.»

```
User-Agent: MyApp/1.0 (my-app-feedback@example.com)
```

Ошибки (`docs/errors.md`, актуальный master):

| HTTP | type | value | описание |
|---|---|---|---|
| 400 | `bad_user_agent` | `unset` | заголовок User-Agent не передан |
| 400 | `bad_user_agent` | `blacklisted` | значение User-Agent в чёрном списке |

Практика: **всегда отправляйте `HH-User-Agent`** (не `User-Agent`): он имеет приоритет, и его можно задать,
не конфликтуя с HTTP-клиентом/прокси, который перезаписывает `User-Agent`.
Сторонний клиент `go-hhru-api` (сгенерирован из публичной спеки HH) делает `HH-User-Agent` обязательным
параметром конструктора и подставляет его во все запросы — косвенное подтверждение, что это жёсткое требование.

### 1.5. Регистрация приложения на dev.hh.ru — ФАКТ

* Регистрация: `https://dev.hh.ru` (личный кабинет — `https://dev.hh.ru/admin`).
* «Условия использования сервиса API»: `https://dev.hh.ru/admin/developer_agreement`.
* Требования к названию приложений: `https://dev.hh.ru/articles/apps`; логотипы: `https://hh.ru/article/brandmaterials`.
* **Срок рассмотрения заявки** (`docs/FAQ.md`): «Заявка на создание приложения должна пройти проверку нескольких
  отделов, этот процесс может занимать **до 15 рабочих дней** (это явно указано на форме регистрации).»
* Обжалование отказа — повторная заявка на `https://dev.hh.ru/admin`, срок ответа до 15 календарных дней
  (п. 1.8 и 2.3 Условий).
* Получение токена приложения: `https://api.hh.ru/openapi/redoc#tag/Avtorizaciya-prilozheniya`.

**ВЫВОД:** закладывайте в план проекта ~3 недели только на модерацию приложения до первых боевых вызовов.

---

## 2. Отклики и переписка (NEGOTIATIONS) — точные методы

Базовый префикс: `https://api.hh.ru`. Все методы этого раздела — «работодательские» и требуют **токена
менеджера работодателя**; большая часть помечена бейджем `emp_paid`
(«Методы требуют наличия платного доступа для работодателя»).

Важные цитаты из `docs/employer_negotiations.md`:

> «Вся работа с откликами/приглашениями происходит в рамках **одной выбранной вакансии**.»
> «Для работы с существующим откликом/приглашением доступ не требуется. Это позволяет обрабатывать отклики
> на вакансии, даже не имея доступ к базе резюме.»
> «Для создания приглашения, работодатель должен иметь активированный доступ к базе резюме.»
> «Чтобы соискательский отклик считался просмотренным работодателем необходимо сделать одно из следующих
> действий: запросить список сообщений по урлу … / запросить сообщения по отклику / запросить резюме по урлу … /
> отметить определенные отклики как прочитанные.»

### 2.1. Сводная таблица endpoint'ов

| # | Что нужно | Метод + путь | Ключевые параметры | Ключевые поля ответа |
|---|---|---|---|---|
| a1 | Коллекции + работодательские статусы по вакансии | `GET /negotiations?vacancy_id={vacancy_id}&with_generated_collections={true\|false}` | `vacancy_id` (обяз.), `with_generated_collections` (по умолч. `false`) | `collections[]` (`id`, `name`, `url`, `counters.with_updates`, `counters.total`, `order_types[]`), `generated_collections[]`, `employer_states[]` |
| a2 | **Список новых входящих откликов** | `GET {collections[].url}` — фактически `GET /negotiations/{collection}?vacancy_id={id}&...` | `vacancy_id` (обяз.), `order_by`, `page`, `per_page` (макс. **50**, по умолч. 20), `show_only_new_responses` (только непросмотренные, только для коллекции «Все неразобранные»/`response`), `show_only_new` (есть новые сообщения; для коллекций кроме «Все неразобранные»), плюс фильтры: `search_text`, `area`, `age_from`/`age_to`, `salary_from`/`salary_to`, `experience`, `education_level`, `gender`, `language`, `citizenship`, `relocation`, `driver_license_types`, `educational_institution`, `show_only_with_vehicle`, `search_radius_meters`, `currency` | `found`, `pages`, `page`, `per_page`, `ordered_by`, `items[]`: `id`, `created_at`, `updated_at`, `has_updates`, `state{id,name}`, `employer_state{id,name}`, `actions[]`, `url`, `messages_url`, `resume{}`, `viewed_by_opponent`, `counters{messages,unread_messages}`, `source` (`NEGOTIATION\|PHONE_CALL\|CHAT\|VR`), `test_result` |
| b1 | Полный отклик/приглашение | `GET /negotiations/{nid}` (по `items[].url`) | — | всё из списка + `messaging_status`, `vacancy{...}` (краткая вакансия) |
| b2 | Список сообщений (чат) | `GET /negotiations/{nid}/messages` (по `messages_url`) | `with_text_only=true/false` | `found`, `pages`, `page`, `per_page`, `items[]`: `id`, `text` (может быть `null`), `created_at`, `author.participant_type` (`applicant\|employer`), `viewed_by_me`, `viewed_by_opponent`, `state{id,name}`, `address`, `assessments[]`, `editable` |
| c1 | **Отправить сообщение** | `POST /negotiations/{nid}/messages` | тело `application/x-www-form-urlencoded`, параметр `message` | `201 Created`, тело пустое |
| d1 | Смена состояния (пригласить/отказ/отложить) | `PUT {actions[].url}` — напр. `PUT /negotiations/{collection}/{nid}` | аргументы из `actions[].arguments`: `message` (обяз. для приглашения), `address_id`, `send_sms` | `204 No Content`; смена состояния видна в `resulting_employer_state` |
| d2 | Приглашение без отклика (по резюме) | `POST {negotiations_actions[].url}` — напр. `POST /negotiations/phone_interview?resume_id=..&vacancy_id=..&message=..` | `resume_id`, `vacancy_id`, `message` (+ возможные `address_id`, `send_sms`) | `201 Created` + заголовок `Location: /negotiations/{nid}` |
| d3 | Отметить отклики прочитанными | операция OpenAPI `post-negotiations-topics-read` (`POST /negotiations/topics/read` — **[НЕ ПРОВЕРЕНО]**, путь взят из оглавления/названия операции) | — | — |
| e1 | Предпочитаемая сортировка | `GET /vacancies/{id}/preferred_negotiations_order` / `PUT` (параметр `order`) | `order` из `order_types` | `{"order_type":{"id","name"}}` / `204` |
| e2 | Статистика по откликам | `get-negotiations-statistics-employer`, `get-negotiations-statistics-manager` (`docs/employer_negotiations_statistics.md` — только заглушки) | — | `received`, `viewed_percent`, `replied_percent`, `average_reply_time`, `politeness{index,index_change,hint,description,article_url}` |
| e3 | Шаблоны писем | `get-mail-templates` / `put-mail-templates-item` / `get-negotiation-message-templates` | `template`, `topic_id` | `mail.employer_state`, `mail.text`, `template_modified` |

### 2.2. Модель: коллекции, состояния, действия (ФАКТ)

* **Отклик** — инициирован соискателем; **приглашение** — инициировано работодателем. «Отклик всегда происходит
  одним резюме на одну вакансию»; «Для каждой пары *вакансия + резюме* может быть только один отклик/приглашение».
* **Коллекция** — «набор откликов/приглашений, объединённых по определённым критериям… Не стоит полагаться на то,
  что в определённые этапы обработки отклика/приглашения оно будет входить в известные коллекции, так как эта
  логика может изменяться.» Единой коллекции «все отклики по вакансии» нет: «Если вам необходимо получить полный
  список – запросите последовательно все коллекции.»
* Сгенерированные коллекции (`with_generated_collections=true`): `vacancy_visitors`, `relevant_responses`,
  `by_location`, `phone_calls` — «созданы для облегчения работы пользователя». Коллекция `phone_calls` принимает
  только `vacancy_id`, `order_by`, `page`, `per_page`.
* **Соискательское состояние** (`state`, справочник `negotiations_state`): `response`, `invitation`, `discard`,
  `hidden`. **Работодательское состояние** (`employer_state`) зависит от вакансии и работодателя — «список
  необходимо запрашивать», передавая вакансию. Пример из доков: `response`, `invitation`, `discard`,
  `discard_after_interview`.
* **Действия** (`actions[]`): `id`, `name`, `enabled`, `method`, `url`, `resulting_employer_state` (или `null`),
  `templates[]`, `arguments[]` (`id`, `required`, `required_arguments`). Примеры: `invitation`, `hold`, `discard`.
  Аргументы: `message` (текст письма соискателю), `address_id`, `send_sms` («в sms-сообщении используется
  стандартный текст, изменить его нельзя»).
* **Критично:** «Приложение должно уметь заполнять данные аргументы. В будущем могут появляться и другие
  аргументы… Обязательность аргументов может быть разная для разных откликов/приглашений.» То есть
  URL/метод действия нельзя хардкодить — их нужно брать из ответа.

### 2.3. Как выглядит «достать новые отклики» на практике (ВЫВОД)

1. `GET /negotiations?vacancy_id={id}` → взять коллекцию с `id` вида `response` («Все неразобранные»/
   «Неразобранные») — именно у неё работает `show_only_new_responses=true`.
2. `GET {collections[].url}&per_page=50&order_by=...&show_only_new_responses=true`.
3. Для каждого `items[].id` — `GET items[].url` (полный отклик) и/или `GET items[].resume.url` (полное резюме).
   Любой из этих запросов сбрасывает «непросмотренность» отклика и флаг `has_updates`.
4. `GET items[].messages_url` — переписка; `POST /negotiations/{nid}/messages` — ответ.
5. Действия (`invitation`/`discard`/`hold`) — строго через `PUT {actions[].url}` с аргументами.

---

## 3. Chats API — новый чат вместо устаревших `/negotiations/{id}/messages`

**ФАКТ (master README и `docs/employer_negotiations.md`):**

> «‼️ Обратите внимание, что методы для работы с сообщениями в рамках отклика/приглашения от имени соискателя и
> менеджера работодателя **устарели**, и новые возможности чатов в них не будут поддерживаться. В связи с этим
> переписка может некорректно отображаться.»

То есть `GET/POST /negotiations/{nid}/messages` — легаси. Новые возможности (файлы, редактирование/удаление,
участники, счётчики непрочитанного) живут в отдельном **Chats API**.

**Имена операций Chats API** (получены из `gen/employer/client.gen.go` — кода, сгенерированного из публичной
OpenAPI-спеки HH; пути в тексте спеки я прочитать не смог — **[НЕ ПРОВЕРЕНО ПО ПЕРВОИСТОЧНИКУ]**):

| Операция (operationId в спеке) | Смысл |
|---|---|
| `GetCommonChatList` | список чатов (`found/pages/page/per_page`, `items[]`: `id`, `type` (`NEGOTIATION`), `creation_time`, `display.title`, `last_message{...}`, `unread_message_count`, `muted`, `block_reason` — напр. `UNTRUSTED_EMPLOYER`) |
| `UnreadChatsCount` | **счётчик непрочитанных чатов** (ближайший аналог «менеджерских» счётчиков) |
| `GetChatMessages` | сообщения чата (`ChatsCommon*`), постранично |
| `ChatMessagePost` | отправить сообщение (есть `idempotency_key`, `text`, `file_upload_ids`) |
| `ChatMessagePut` / `ChatMessageDelete` | редактировать / удалить сообщение |
| `SetLastViewedMessage` | отметить чат прочитанным до сообщения |
| `GetParticipantList` / `PutParticipantList` | участники чата (добавить коллегу) |
| `LeaveChat` | покинуть чат |
| `SetWritePossibilityCommon` | запретить/разрешить переписку |
| `GetCommonChatFilesConditions` / `GetCommonChatFilesUploadLinks` | условия и ссылки загрузки файлов (`max_size: 20971520`, mime-типы) |
| `GetOrCreateChatWithoutVacancyCommon` | начать чат без вакансии (`resume_hash`, `first_message`) |

Из примеров схем видны поля: `idempotency_key` (защита от дублей при повторной отправке — **важно для
автоматизации**), `file_upload_ids`, `block_reason: UNTRUSTED_EMPLOYER`, `muted`, `unread_message_count`.

**ВЫВОД:** для автоматизации переписки нужно использовать Chats API; `idempotency_key` позволяет безопасно
ретраить отправку. Точные URL и схемы надо снять с `https://api.hh.ru/openapi/redoc` (тег Chats) — это
единственный оставшийся пробел в этом отчёте.

---

## 4. Резюме (RESUMES)

### 4.1. Как получить полное резюме за откликом — ФАКТ

* В списке откликов приходит **сокращённое** представление резюме (`docs/employer_resumes.md#resume-short`):
  `id`, `title`, `url`, ФИО, `age`, `can_view_full_info`, `alternate_url`, `created_at`, `updated_at`, `area`,
  `education.level`, `total_experience.months`, `experience[]` (без `description`), `gender`, `salary`, `photo`,
  `owner`, `negotiations_history`, `download`.
* **Полное резюме** — `GET /resumes/{resume_id}` (`docs/employer_resumes.md#item`). Ключевая цитата:

> «**Важно!** Дополнительный запрос на получение данных о резюме **необходимо** выполнять именно по url из JSON
> ответа. Только в таком случае запрос будет правильно учтён в истории просмотров. Например, при запросе резюме из
> отклика на анонимную вакансию, в истории будет отражено анонимное название компании.»

  То есть из отклика надо брать `items[].resume.url` (там `?topic_id={nid}`), а не конструировать
  `/resumes/{id}` самостоятельно.
* После запроса этого URL отклик считается просмотренным.

### 4.2. Платный доступ, контакты и лимиты — ФАКТ

`docs/employer_resumes.md`:

> «Для работодателя данный метод требует наличия **платного доступа**»
> Ошибки: `403 Forbidden` — неверная авторизация; `404 Not Found` — резюме не существует или недоступно;
> `429 Too Many Requests` — превышен лимит просмотров резюме в сутки (при авторизации работодателем).

`docs/payable/resume.md`:

> «Работодатели, использующие новый тариф, должны явно открывать контактную информацию при просмотре резюме
> из базы резюме.»
> «Для создания приглашения, работодатель должен иметь активированный доступ к базе резюме.»
> «Если соискатель откликнулся первым, списание происходить не будет. В этом случае будут сразу видны контакты,
> без дополнительных действий.»
> «Списание происходит только один раз: либо при просмотре резюме с контактами, либо при приглашении соискателя
> на одну из вакансий компании.»

Механика контактов: в ответе резюме есть `actions.get_with_contact.url` и `actions.download_with_contact.*`,
ссылки «генерируется автоматически и каждый раз разный»; использование этих ссылок списывает контакт.
Контакты = `last_name`, `first_name`, `middle_name`, `contact[type=cell]`, `contact[type=email]`, `site`, `photo`.
Ограничение: для резюме, найденных через **поиск по базе**, есть отдельный лимит запросов просмотра
(услуга `API_LIMITED`); «После превышения этого лимита … вернется ошибка».

Ошибки доступа к платному методу (`docs/errors.md`):

| HTTP | type | value | смысл |
|---|---|---|---|
| 403 | `api_access_payment` | `action_must_be_payed` | платный метод без оплаченного доступа |
| 429 | `resumes` | `view_limit_exceeded` | суточный лимит просмотров резюме |
| 403 | `resumes` | `quota_exceeded` | квота просмотров, установленная менеджеру |
| 403 | `resumes` | `no_available_service` | не хватает услуг для просмотра |
| 403 | `resumes` | `cant_view_contacts` | нет прав на просмотр контактов |

### 4.3. Поля, пригодные для автоматического анализа — ФАКТ (модель из `docs/employer_resumes.md`)

| Блок | Поля |
|---|---|
| Идентификация | `id`, `real_id`, `title`, `alternate_url`, `created_at`, `updated_at`, `platform.id` |
| Личные | `first_name`, `last_name`, `middle_name`, `age`, `birth_date`, `gender{id,name}`, `photo{small,medium,40,100,500}` |
| География | `area{id,name,url}`, `metro{id,name,lat,lng,line{...},order}`, `relocation{type,area[]}`, `travel_time`, `citizenship[]`, `work_ticket[]` |
| Зарплата | `salary{amount,currency}` |
| Опыт | `total_experience.months`, `experience[]`: `company`, `company_id`, `company_url`, `area`, `industries[]`, `industry`, `position`, `start`, `end`, `description`; плюс `experience_group_by_company[]` (`duration`, `experience_ids`) |
| Навыки | `skill_set[]` (ключевые навыки, массив строк), `skills` (свободный текст «о себе») |
| Образование | `education.level{id,name}`, `education.primary[]` (`name`, `name_id`, `organization`, `organization_id`, `result`, `result_id`, `year`), `education.additional[]`, `education.attestation[]`, `education.elementary[]` |
| Языки | `language[]` (`id`, `name`, `level{id,name}`) |
| Доп. | `certificate[]`, `recommendation[]`, `professional_roles[]`, `specialization[]`, `employments[]`/`employment`, `schedules[]`/`schedule`, `business_trip_readiness`, `has_vehicle`, `driver_license_types[]`, `resume_locale`, `marked`, `favorited`, `viewed` |
| Скрытое | `hidden_fields[]` (`names_and_photo`, `phones`, `email`, `other_contacts`, `experience`), `can_view_full_info`, `contact_view_status` (`NONE`/`FULL`), `contacts_open_until_date`, `complaint_status` |
| Скачивание | `actions.download.pdf/rtf`, `actions.download_with_contact.*`, `actions.get_with_contact.url`, `download.pdf/rtf` |

Правило анонимности: «В поле `hidden_fields` содержится список скрытых соискателем полей анонимного резюме.
Скрытые поля заменяются на `null`.» — `names_and_photo` → `null` в ФИО и фото; `phones`/`email` → `null` в
`contact[].value`; `other_contacts` → `null` в `site[].url`; `experience` → `null` в
`experience[].company/company_id/company_url/employer` + пустой `recommendation`.

**ВЫВОД:** для скоринга доступны структурированные признаки (`total_experience.months`, `skill_set`,
`education.level`, `salary.amount/currency`, `area.id`, `professional_roles`, `language[].level`,
`experience[].industries`) — их можно использовать без NLP. Но для анонимных резюме и без оплаченного доступа
к базе часть полей будет `null`, поэтому парсер должен быть устойчив к `null` (и проверять `hidden_fields`,
иначе легко принять «скрыто» за «не указано»).

---

## 5. REALTIME: webhook или polling

### 5.1. Webhook API — существует (ФАКТ по оглавлению master README)

README master, раздел «Webhook API» (тег OpenAPI `Webhook-API`), все методы — работодательские:

| Операция | OpenAPI operationId (из ссылок README) |
|---|---|
| Список уведомлений, на которые подписан пользователь | `get-webhook-subscriptions` |
| Подписка на уведомления | `post-webhook-subscription` |
| Удаление подписки | `cancel-webhook-subscription` |
| Изменение подписки | `change-webhook-subscription` |

Подтверждение из сгенерированного клиента: `GetWebhookSubscriptions`, `PostWebhookSubscription`,
`ChangeWebhookSubscription` (с телом), `CancelWebhookSubscription` (`subscription_id`).

**[НЕ ПРОВЕРЕНО]:** перечень типов событий (например, «новый отклик», «новое сообщение»), формат payload,
подпись/секрет, retry-политика и требования к HTTPS-endpoint. Это есть только в OpenAPI-спеке
(`https://api.hh.ru/openapi/redoc`, тег Webhook API), которую в этой среде прочитать не удалось
(`application/x-yaml`). В markdown-документации репозитория вебхуки **нигде больше не упоминаются** —
ни в `docs/*.md`, ни в FAQ.

**ВЫВОД:** наличие Webhook API — главный аргумент не строить продукт на polling. Но до подключения нужно
вручную снять со спеки список событий и формат; в публичной markdown-документации их нет.

### 5.2. Polling: что реально документировано про лимиты

**ФАКТ: в публичной документации нет ни одного числового лимита «запросов в секунду/минуту» и нет описания
заголовков `X-RateLimit-*`.** Проверено:

* `docs/general.md` (последняя ревизия файла, 2024-02-13) — разделы «Требования к запросам», «Формат тела»,
  «Ответ», «Ошибки и коды ответов», «Пагинация», «CORS»; **ни слова о rate limit**.
* `docs/errors.md` (master) — код `429` встречается только как `resumes / view_limit_exceeded`
  (суточный лимит просмотров резюме) и в описании скачивания резюме.
* `docs/cache.md` (master) — только клиентское кэширование по `Etag`/`Cache-Control`/`Expires`.
* Спека `api.hh.ru/openapi/specification/public` — прочитать не удалось; **[НЕ ПРОВЕРЕНО]**, есть ли там раздел
  про лимиты. Никаких `X-RateLimit-*` в доступных текстах нет.

Что **есть** вместо rate limit — бизнес-лимиты (ФАКТ, `docs/errors.md`, раздел «Переписка»):

| HTTP | type | value | смысл |
|---|---|---|---|
| 403 | `negotiations` | `in_a_row_limit` | «превышено количество последовательных сообщений в переписке, оппонент должен ответить на сообщение, чтобы появилась возможность писать вновь» |
| 403 | `negotiations` | `overall_limit` | «превышен лимит сообщений» |
| 400/403 | `negotiations` | `limit_exceeded` | «превышен лимит количества откликов/приглашений» |
| 403 | `negotiations` | `not_enough_purchased_services` | не хватает оплаченных услуг (обычно доступ к базе резюме) |
| 403 | `resumes` | `quota_exceeded` / `cant_view_contacts` / `no_available_service` | квоты/права на резюме и контакты |
| 429 | `resumes` | `view_limit_exceeded` | суточный лимит просмотров резюме |
| 403 | `vacancies` | `quota_exceeded` | квота менеджера на публикацию вакансий данного типа |
| 403 | `vacancies` | `too_early` | продление вакансии раньше времени |

Справочник `messaging_status` (из спеки, пример) даёт конкретику по «подряд»:
`in_a_row_limit` → «**Вы отправили 5 сообщений подряд.** Дождитесь ответа, чтобы написать еще».
То есть лимит последовательных сообщений работодателя — **5** (пока соискатель не ответит).

Прочее про 429/5xx:

* `docs/errors.md`: «В случае, если сервис не может обработать запрос… придёт `503 Service Unavailable` и `type`
  будет содержать `service_unavailable`. В случае непредвиденной ситуации API вернёт 500. В редких случаях ошибки
  с 5** кодами могут возвращаться с телом не содержащим валидный json. Приложение должно в таких случаях
  ориентироваться только на код ответа.»
* Сторонний клиент `go-hhru-api` (не первоисточник, но полезно как практика): ретраит `429`/`503`
  **с учётом заголовка `Retry-After`** и экспоненциальной задержкой. Значит `Retry-After` сервер отдаёт
  (в документации HH он не описан — **[НЕ ПРОВЕРЕНО]**).

### 5.3. Кэширование — ФАКТ (`docs/cache.md`)

* Ответ может содержать `Etag`, `Cache-Control: max-age=...`, `Expires`.
* Клиент может запомнить `Etag` и передать его в `If-None-Match`; если ресурс не изменился — `304 Not Modified`
  без тела. При изменении — новый `200` с новым `Etag`.
* «При проверке по `Etag` возможно использование HEAD запроса, он будет работать аналогично, но даже в случае,
  если ресурс изменился, будет возвращены только заголовки без тела ответа.»
* «Кэширование поддерживается большинством справочников. Определить поддержку можно по наличию заголовка `Etag`
  в ответе.»

### 5.4. Счётчики/уведомления вместо вебхуков

* `GET /me` для работодателя (ревизия `b2e70d2`, 2019) возвращает объекты `employer{manager_id,id,name}`,
  `manager{id,has_admin_rights,is_main_contact_person,manager_settings_url}`, `personal_manager{...}`.
  Объект `counters` (`unread_negotiations`, `new_resume_views`, `resumes_count`) в документации описан как
  выдаваемый **соискателю**: «`counters` | объект, отсутствует | информация о счетчиках, **если текущий
  пользователь — соискатель**».
* Для работодателя счётчики откликов доступны через коллекции: `collections[].counters.with_updates`
  («количество откликов/приглашений в коллекции, требующих внимания») и `counters.total`; в элементе отклика —
  `counters.unread_messages`, `has_updates`.
* Chats API: `UnreadChatsCount` — счётчик непрочитанных чатов.
* **«Mail New» и `/employer/manager` как публичные методы не найдены.** В оглавлении README master и в
  markdown-доках таких методов нет; `docs/employer_managers.md` и `docs/employer_negotiations_statistics.md`
  в master — заглушки со ссылками на OpenAPI. **[НЕ ПРОВЕРЕНО]**, что таких операций нет в спеке вовсе.

### 5.5. Вердикт по realtime (ВЫВОД)

1. **Есть Webhook API (4 метода, работодатель)** — это штатный push-механизм; детали событий надо снять со спеки.
2. Если вебхуков по нужным событиям не окажется — polling, но **без документированных rate limit**: безопасный
   интервал придётся подбирать эмпирически, опираясь на `429`/`Retry-After` и на бизнес-лимиты (просмотры резюме
   в сутки, 5 сообщений подряд, лимит приглашений в сутки).
3. Практичная схема: опрашивать **коллекции по каждой активной вакансии** (это самый дешёвый «счётчик новых»:
   `counters.with_updates`), и только при изменении тянуть список с `show_only_new_responses=true`.
   Интервал 1–5 минут выглядит разумным компромиссом; **это ВЫВОД, а не документированная рекомендация.**

---

## 6. Ограничения и политика использования (для автоматизации и ИИ-текстов)

### 6.1. Условия использования сервиса API — ФАКТ (`https://dev.hh.ru/admin/developer_agreement`)

Ключевые пункты, релевантные боту, который сам пишет соискателям:

> «**4.9.** Приложение не должно вызывать какие-либо активные действия в API **без ведома авторизуемых
> пользователей и без их согласия**.»

> «**1.6.** В случае, если у HeadHunter есть основания полагать, что Разработчиком приложения или конечным
> пользователем Приложения Сервис потенциально может быть использован не только для целей привлечения
> потенциальных работников и их последующего трудоустройства (например, оценка платежеспособности и кредитного
> рейтинга соискателя, маркетинговые опросы…, продвижение рекламы и **рекламные рассылки без согласия
> получателей**, и другие случаи использования данных, не связанные с привлечением потенциальных работников и
> трудоустройством соискателей), HeadHunter вправе в любое время … отказать Разработчику приложения
> в использовании Сервиса без какой-либо компенсации.»

> «**4.7.** Запрещено использовать пользовательские данные в рекламных (без согласия получателей; например,
> в виде рассылки спама) или целях политической или иной агитации.»

> «**4.5.** Запрещено хранить и использовать полученные посредством Приложения данные для целей, отличных от
> тематики сайта https://hh.ru, а именно контакта потенциального работодателя с потенциальным соискателем
> с целью последующего трудоустройства.»

> «**4.6.** Запрещается передавать данные, полученные посредством Сервиса из баз данных и сайтов, принадлежащих
> HeadHunter … сторонним сервисам для использования. Единственными пользователями данных … могут быть
> зарегистрированные пользователи сайта https://hh.ru, для которых инициировалось получение данных.»

> «**3.1.** Разработчик приложения несет ответственность за использование, сохранность и конфиденциальность
> полученного им уникального API-ключа.»
> «**3.6.** … запрещается собирать, хранить или каким-либо иным образом обрабатывать учетную информацию
> (логины и пароли) пользователей Сайта.»
> «**3.11.** … запрещается вносить изменения в материалы, которые получены посредством Сервиса с Сайта.
> Разработчик Приложения обязан следить за актуальностью вышеуказанных материалов, в том числе, своевременно
> удалять вакансии, которые перемещены в архив.»
> «**2.3.** В случае нарушения … HeadHunter вправе … приостановить возможность использования всего Сервиса …
> действие уникального API-ключа … предоставляется срок в течение 15 календарных дней … для устранения
> обнаруженных нарушений.»

**Что это значит для продукта (ВЫВОД):**
* Автоматическая отправка сообщений/приглашений допустима как функция приложения, которой **управляет менеджер**
  (пользователь дал согласие и видит действия). Полностью автономный «ИИ-бот, который сам пишет всем откликам»
  без участия/согласия авторизуемого менеджера — прямой конфликт с п. 4.9 и риск отключения ключа по п. 2.3.
* Массовые рассылки, реклама, использование данных не для трудоустройства — запрещены (п. 1.6, 4.5, 4.7).
* Нельзя отдавать данные HH сторонним сервисам/собственным аналитическим платформам за пределами
  зарегистрированных пользователей HH (п. 4.6) — важное ограничение для «общей базы кандидатов» в продукте.

### 6.2. Обязательная маркировка ИИ-текста / шаблоны — ФАКТ

**В документации нет требования маркировать ИИ-сгенерированный текст.** Ни в `docs/*.md`, ни в Условиях
использования API такого требования нет. **[НЕ ПРОВЕРЕНО]** — возможно, есть в других документах hh.ru
(например, оферте/правилах сайта), но в API-документации отсутствует.

Что есть вместо этого:

* **Шаблоны писем** (серверные): `templates[]` в действиях → `GET /message_templates/{template}?topic_id={nid}`;
  операции `get-mail-templates`, `get-negotiation-message-templates`, `put-mail-templates-item`
  («Изменение шаблона ответа соискателю»). Пример ответа: `mail.employer_state: PHONE_INTERVIEW`,
  `mail.text`, `template_modified`.
* **СМС**: «в sms-сообщении используется стандартный текст, изменить его нельзя» (аргумент `send_sms`);
  в базе знаний: «Тексты СМС — стандартные для всех компаний, без возможности редактирования.»
* **Системные сообщения** (автосообщения при переводе отклика по этапам): «Сообщения, отправленные
  автоматически при переводе отклика по этапам подбора, удалить или отредактировать после отправки нельзя»
  (`feedback.hh.ru/knowledge-base/article/1290`).
* Ограничения на текст письма: `empty_message` («передан пустой текст письма»), `too_long_message`
  («передан слишком длинный текст письма») — `docs/errors.md`.
* `in_a_row_limit` (5 подряд) и `overall_limit` — фактический «анти-спам» механизм платформы.

**ВЫВОД:** продуктовое «ИИ пишет ответ» технически возможно (свободный текст через `POST .../messages` или
аргумент `message`), но: (1) требует действия/согласия менеджера, (2) упирается в лимит 5 сообщений подряд,
(3) шаблоны и СМС-тексты контролируются HH. Явного требования «маркировать ИИ-текст» в API-документации нет.

### 6.3. Прочие ограничения, найденные в доках (ФАКТ)

* Вакансия в архиве/скрыта → `403 negotiations / invalid_vacancy`; резюме удалено/скрыто →
  `400|403 negotiations / resume_not_found`; переписка отключена работодателем → `disabled_by_employer`;
  отклик/приглашение заархивировано → `chat_archived`; вакансия заархивирована → `archived`.
* `403 negotiations / no_invitation` — «переписка недоступна, так как в отклике ещё не было приглашения».
* `403 negotiations / edit_forbidden` — редактирование сообщения недоступно.
* `403 negotiations / test_required` — «для отклика необходимо пройти тест (в данный момент, отклик на такие
  вакансии через API недоступен)».
* `403 negotiations / resume_visibility_conflict` — нельзя откликнуться на анонимную вакансию резюме
  с видимостью «белый список».
* Переписку по конкретной вакансии можно отключить полем `allow_messages` у вакансии.
* Капча: некоторые операции защищены; ошибка `captcha_required` с `fallback_url`/`captcha_url`
  (в `captcha_url` нужно добавить обязательный параметр `backurl`).

---

## 7. Практические подводные камни интеграции

1. **Хранение токенов.** Для каждого работодателя/менеджера — своя пара `access_token` + `refresh_token`.
   `refresh_token` одноразовый и работает только после истечения access-токена; при параллельных запросах
   обновление надо сериализовать (mutex), иначе `token has already been refreshed`. Токен приложения
   (`client_credentials`) — «неограниченный», перевыпуск отзывает предыдущий и не чаще 1 раза в 5 минут.
   Пароль менеджера/приложения сбрасывается → токен деактивируется (`token deactivated`), нужна повторная
   авторизация. Никогда не храните логины/пароли пользователей hh.ru (п. 3.6 Условий).
2. **403 vs 404 у резюме.** `403` — «неверная авторизация. Ожидается авторизация пользователя» либо не хватает
   услуг/прав (`no_available_service`, `cant_view_contacts`, `quota_exceeded`, `action_must_be_payed`);
   `404` — «резюме не существует **или недоступно пользователю**»; `429` — суточный лимит просмотров.
   То есть `404` не означает «удалено»: это ещё и «нет доступа». **ВЫВОД:** в UI нельзя писать «резюме удалено»
   по коду 404; нужно различать «нет доступа» и «удалено» по бизнес-контексту (например, `resume: null`
   в отклике — «соискатель удалил резюме или закрыл к нему доступ»).
3. **Архивные вакансии.** `invalid_vacancy` (403) при любых операциях с откликом по архивной/скрытой вакансии;
   `archived` (403) при отправке сообщения; `chat_archived`. В базе знаний: «Если вакансия была перенесена
   в архив, возможность переписки отключается через **180 дней** с момента архивации, после чего контакты
   кандидатов будут скрыты»; «Вы можете связаться с кандидатом, который откликнулся на вашу вакансию,
   пока вакансия не пробудет в архиве 180 дней». Для списков вакансий есть операции
   `get-archived-vacancies`, `get-hidden-vacancies`, `add-vacancy-to-archive`, `add-vacancy-to-hidden`,
   `restore-vacancy-from-hidden`. **ВЫВОД:** синхронизируйте статус вакансии и не держите «активные» отклики
   по архивным вакансиям без обработки 403.
4. **Видимость чата.** Переписка доступна **после приглашения**: `no_invitation` — «переписка доступна только
   после приглашения» (значение справочника `messaging_status`); в `docs/employer_negotiations.md`:
   «Обмен сообщениями возможен после приглашения соискателя». Наличие/состояние переписки смотрите в поле
   `messaging_status` (`ok`, `archived`, `disabled_by_employer`, `in_a_row_limit`, `overall_limit`,
   `no_invitation`, `resume_deleted`, `prohibited`). Отдельно: если вакансия с опубликованными контактами,
   соискатель может написать **без отклика** — такой чат выглядит как обычный, но отклика в нём нет
   (`source: CHAT` в откликах; в базе знаний — «чат без вакансии»).
5. **Кулдауны и «анти-спам».** `in_a_row_limit` — не более **5** последовательных сообщений работодателя до
   ответа соискателя; `overall_limit` — общий лимит сообщений; `limit_exceeded` — лимит приглашений/откликов
   в сутки; `too_early` — продление вакансии раньше срока; `quota_exceeded` — квота менеджера.
   Отдельный лимит — просмотры резюме в сутки (429) и лимит запросов к просмотру резюме из поиска (услуга
   `API_LIMITED`). **ВЫВОД:** любой автоответчик должен уважать «5 подряд» и не ретраить 403-ошибки бизнес-логики
   (они не пройдут повтором), а ретраить только 429/503.
6. **`has_updates` и «прочитанность».** Флаг `has_updates` сбрасывается при просмотре сообщений или резюме;
   отклик становится «просмотренным» после запроса `messages_url` / резюме по `resume.url` / метода
   «отметить прочитанными». **ВЫВОД:** если автоматика просто читает список откликов, ничего не «сгорает»;
   но как только вы тянете переписку/резюме, отклик уходит из «новых» — это нужно учитывать, чтобы не терять
   «новые» в собственной аналитике (сохраняйте `created_at`/`id` у себя).
7. **Списание контактов.** Контакт списывается один раз: либо просмотр резюме с контактами (по ссылкам
   `get_with_contact`/`download_with_contact`), либо приглашение. Если соискатель откликнулся сам — списания нет,
   контакты сразу видны. Ссылки на контакты одноразовые/разные при каждом ответе. **ВЫВОД:** нельзя кэшировать
   ссылку `get_with_contact` и переиспользовать её — генерируйте заново из свежего ответа.
8. **Не хардкодить пути действий.** Список `actions[]`/`negotiations_actions[]`, их `method`, `url`, `arguments`
   и `resulting_employer_state` приходят из API и «могут отличаться для каждого отклика/приглашения».
   Документация прямо предупреждает: «Приложение должно уметь заполнять данные аргументы. В будущем могут
   появляться и другие аргументы».
9. **Модерация приложения.** До 15 рабочих дней; заявка может быть отклонена, если продукт выглядит как
   не-рекрутинговая автоматизация (п. 1.6 Условий).
10. **`HH-User-Agent` обязателен** (иначе `400 bad_user_agent/unset`), а «в чёрном списке» — `blacklisted`;
    закладывайте реальный контактный e-mail разработчика в значение заголовка.
11. **Устаревание методов сообщений.** `/negotiations/{nid}/messages` официально устарел: «новые возможности
    чатов в них не будут поддерживаться… переписка может некорректно отображаться». Для новых интеграций —
    Chats API.
12. **Пагинация.** Единый формат `{found, pages, page, per_page, items}`; нумерация с 0; по умолчанию 20;
    в списке откликов максимум `per_page=50`. Для справочников используйте `Etag`/`If-None-Match` (304 экономит
    квоту).

---

## 8. Что осталось непроверенным (нужно снять со спеки/поддержки)

1. **Список событий Webhook API**, формат payload, подпись, retry — только в OpenAPI (тег Webhook API).
2. **Точные URL и схемы Chats API** (`GetCommonChatList`, `GetChatMessages`, `ChatMessagePost`,
   `UnreadChatsCount`, `SetLastViewedMessage`, файлы, участники) — в спеке; у меня только имена операций.
3. **Актуальное значение `expires_in`** для access-токена (в старой ревизии — 1209600 = 14 суток).
4. **Есть ли в OpenAPI раздел про rate limit / `X-RateLimit-*` / `Retry-After`.** В markdown-документации —
   нет; в спеке проверить не удалось (content-type `application/x-yaml`). Практический источник:
   `https://api.hh.ru/openapi/redoc` → «Общая информация» / «Требования к запросам».
5. **Scope'ы OAuth** (перечень прав, запрашиваемых приложением) — в текущей доке ушли в OpenAPI
   (`authorization_for_user.md` в master — заглушка).
6. **Существуют ли «менеджерские» методы уведомлений/счётчиков** помимо `UnreadChatsCount` и счётчиков коллекций
   (`Mail New`, `/employer/manager`) — в публичных markdown-доках не найдены.
7. **Точные пути** для `post-negotiations-topics-read`, `get-negotiations-statistics-employer/manager`,
   `get-mail-templates` — в master-доках только ссылки на OpenAPI.
