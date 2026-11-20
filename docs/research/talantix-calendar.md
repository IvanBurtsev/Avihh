# Talantix: API, календарь и бронирование собеседований

Дата исследования: 2026-11-20. Источники — публичные страницы `api.talantix.ru/docs` и база знаний `feedback.hh.ru/talantix/knowledge-base`.
Формат: **ФАКТ** = прочитано на странице (URL указан). **ВЫВОД** = моя интерпретация, не подтверждённая документацией. **НЕ ПРОВЕРЕНО** = требует проверки на живом тенанте.

---

## 0. Краткий ответ

**Публичный документированный API есть** — это GraphQL API с отдельным порталом документации `https://api.talantix.ru/docs/`. В нём есть полноценный календарь: чтение встреч (`calendarMeetings`), создание/изменение/удаление встречи (`createCalendarMeeting` / `updateCalendarMeeting` / `deleteCalendarMeeting`). Отдельного free/busy-эндпоинта нет: доступность менеджера вычисляется нами как «сетка минус уже существующие встречи».

Публичных webhooks нет. iCal/CalDAV-фида нет. Синхронизация с Google/Outlook/Яндекс/Битрикс24 есть, но по описанию в базе знаний она **односторонняя: Talantix → внешний календарь** (плюс обратные ссылки на вакансию/резюме внутри внешнего события). Писать во внешний календарь и ждать, что Talantix «отзеркалит» встречу, — недокументированный сценарий, на него нельзя опираться.

Реалистичная схема: **читать доступность из Talantix API, бронировать через `createCalendarMeeting` в Talantix**, а во внешний календарь событие уезжает само, если передать `externalCalendarType`. Свою БД держать как источник истины по брони и как защиту от гонок.

---

## 1. Публичный API

### 1.1. ФАКТЫ: портал и общая информация

- Портал документации: `https://api.talantix.ru/docs/` — «Talantix for developers», в футере «© 2026 Группа компаний HeadHunter». Есть Playground (`/docs/pages/playground/`).
- Базовый URL: `https://api.talantix.ru`, GraphQL-эндпоинт — `/graphql` (POST). Источник: `https://api.talantix.ru/docs/general` — «Базовый URL - `https://api.talantix.ru`», «В теле запроса обязано присутствовать поле `query`», «Дата/время возвращается в виде timestamp - `1703513801000` - количество миллисекунд, прошедших с 1 января 1970 года по UTC».
- Обязательные заголовки: `Content-Type: application/json`, `Authorization: Bearer <access token>`, `User-Agent` (в нём просят указать компанию и контакты — «будет использоваться для связи в случае различных проблем»).
- Интроспекция схемы: `POST /graphql/schema` (по `https://api.talantix.ru/docs/general`: «Описание схемы GraphQL API Talantix можно получить, выполнив запрос интроспекции на url `/graphql/schema`»). Проверено: `GET /graphql/schema` → **HTTP 405 Method Not Allowed** (Jersey), т.е. только POST.
- Пример из документации: `curl https://api.talantix.ru/graphql -X POST -H "Content-Type: application/json" -H "User-Agent: ..." -H "Authorization: Bearer <token>" -d '{"query":"query Me {me {id firstName lastName middleName email}}"}'`.
- Это **GraphQL**, а не REST. «Семейства эндпоинтов» — это корневые поля схемы, а не URL-пути. Ниже — фактический список из навигации `/docs/api/queries` и `/docs/api/mutations`.

### 1.2. ФАКТЫ: авторизация

Источник: `https://api.talantix.ru/docs/authorization/` и `https://feedback.hh.ru/talantix/knowledge-base/article/0964`.

- «API Talantix доступен только в тарифе **«Оптимальный»**» (`/docs/authorization/`). При этом в статье базы знаний: «**Действует в тарифах «Оптимальный» и «Продвинутый»**» (`article/0964`). **Расхождение в источниках** — фиксирую как есть.
- «Чтобы начать работать с API, пользователю с ролью **«Администратор»** в Talantix необходимо сгенерировать авторизационные токены в личном кабинете. Токены нужны для выполнения запросов в API от лица их создателя».
- Модель — OAuth2-подобная: есть `access_token`, `refresh_token`, `expires_in: 86400`, `refresh_token_expires_in: 10368000` (120 дней), `token_type: bearer`. Пример ответа в документации.
- **Одноразовый refresh**: в документации прямо сказано, что refresh token одноразовый и при обновлении возвращается новый (ротация). Повторное использование старого → ошибка, и пара токенов аннулируется. Это критично для параллельных воркеров.
- Токены привязаны к администратору, который их создал: при смене роли/блокировке администратора все его токены перестают работать (статья `article/0964`).
- Ссылка на авторизацию действует 30 дней, токены деактивируются после 120 дней неиспользования (там же).
- **Платные места**: чтение через API не требует оплаченного рабочего места, но «если с помощью API в Talantix планируется создавать кандидатов, заявки, комментарии и совершать другие действия, тогда рабочее место такому администратору нужно выделить» (`article/0964`). Создание встречи — это «другие действия» ⇒ **ВЫВОД**: для записи на собеседование нужен оплаченный тариф и рабочее место у владельца токена.

### 1.3. ФАКТЫ: корневые запросы (queries)

Из навигации `https://api.talantix.ru/docs/category/queries` (полный список):

`calendarMeeting`, `calendarMeetings`, `calendar`, `company`, `dictionaries`, `hhManagers`, `hiringRequestTemplate`, `hiringRequest`, `hiringRequests`, `manager`, `managers`, `me`, `messageTemplates`, `personApprovalLinks`, `personSources`, `person`, `persons`, `response`, `testTaskTemplates`, `timezones`, `vacancies`, `vacancy`, `workflowStatus`.

Маппинг на «семейства» из задачи:

| Семейство | Что есть в API |
|---|---|
| Вакансии | `vacancies`, `vacancy`, `hiringRequests`, `hiringRequest`, `workflowStatus` |
| Кандидаты | `persons`, `person`, `personSources`, `personApprovalLinks`, `response` |
| Отклики | `response` (одиночный), `createResponses` (мутация), `persons` с фильтрами |
| Воронка | `workflowStatus`, `transferPersonsToWorkflowStatus`, `workflowStatuses` |
| Календарь/события | `calendar`, `calendarMeeting`, `calendarMeetings`, `createCalendarMeeting`, `updateCalendarMeeting`, `deleteCalendarMeeting` |
| Сотрудники | `managers`, `manager`, `me`, `hhManagers` |
| Справочники | `dictionaries`, `timezones` |

### 1.4. ФАКТЫ: мутации (полный список из навигации)

Полный список из `https://api.talantix.ru/docs/category/mutations` (жирным — календарные):

`approveHiringRequest`, `cancelPersonWorkflowStatusTransfer`, `changeVacanciesStatus`, **`createCalendarMeeting`**, `createHiringRequestTemplateField`, `createHiringRequest`, `createPersonComment`, `createPersonDiscardReason`, `createPersonSource`, `createPersonTag`, `createPerson`, `createResponses`, `declineHiringRequest`, **`deleteCalendarMeeting`**, `deleteCompanyPersonTag`, `deleteHiringRequestTemplateField`, `deletePersonComment`, `deletePersonMessage`, `deletePersonSource`, `deletePersons`, `deleteResponse`, `deleteVacancyTemplate`, `disableIntegration`, `discardPersons`, `editPerson`, `hirePersons`, `invitePersonsOnHhRu`, `mergePersons`, `mergeVacancies`, `openPersonContacts`, `replaceCompanyPersonSource`, `replaceCompanyPersonTag`, `requestHiringRequestApprove`, `restorePersons`, `sendMessageToPerson`, `sendPersonToStartLink`, `sendPersonalDataRequest`, `sendPersonsApprovalRequests`, `sendTestTaskToPersons`, `setDefaultVacancyTemplate`, `transferPersonsToWorkflowStatus`, **`updateCalendarMeeting`**, `updateCompanyInformation`, `updateHiringRequestTemplateField`, `updateHiringRequest`, `updatePersonComment`, `updatePersonSource`, `withdrawHiringRequest`.

### 1.5. ФАКТЫ: пагинация

Источник: `https://api.talantix.ru/docs/guides/graphQL/pagination`.

- Курсорная пагинация: аргументы `first` (размер страницы) и `after` (курсор).
- Ответ содержит `pageInfo { first, hasNextPage, endCursor }`.
- Флоу из документации: запрос без `after` → берём `pageInfo.endCursor` → следующий запрос с `after = endCursor` → повторять, пока `pageInfo.hasNextPage = false`.
- Лимиты `first`: у `managers` — «Количество запрашиваемых менеджеров, максимум 50» (`/docs/api/queries/managers`). У `persons` — максимум 50. У `calendarMeetings` пагинации нет вообще: это запрос по диапазону дат.

### 1.6. ФАКТЫ: лимиты и ограничения

Источник: `https://api.talantix.ru/docs/guides/errors`.

- **HTTP 429 `too_many_requests`** — «Превышено количество запросов в единицу времени». **Численное значение rate limit в документации не опубликовано** (ни в errors, ни в general, ни в authorization). Это надо выяснять эмпирически/у поддержки.
- **HTTP 413 `payload_too_large`** — превышен максимальный размер запроса.
- **HTTP 210 `query_complexity_exceeded`** — превышен лимит сложности запроса. Пример из документации: запрос с алиасами дал сложность 2650 при лимите 2499, т.е. **лимит сложности ≈ 2500**.
- **HTTP 269 `root_selection_exceeded`** — «Допускается использовать только одно корневое поле в запросе». То есть нельзя одним запросом получить `calendarMeetings` и `managers` — только по одному корню за вызов.
- **HTTP 200 `ok`** — норма.
- Доменные ограничения из схемы: `calendarMeetings` — «максимальный диапазон - 93 дня»; `managerIds` — максимум 20; `personIds` — максимум 50; `vacancyIds` — максимум 20; `topic` — 512 символов; `place` — 512; `comment` — 20 000 (HTML); `personEmail.text` — 50 000; `personSmsText` — 70.
- Ошибки бизнес-логики приходят как HTTP 200 + `errors` с расширениями (`code`, `type`, `field`, `message`), а не как HTTP-коды.

### 1.7. ФАКТЫ: webhooks

**Публичных webhooks нет.** В документации нет ни раздела, ни типа «webhook», ни подписки на события. Единственное, что называется «Subscriptions», — это `union Subscriptions = VacancySubscriptionsError | VacancySubscriptionsItem` с описанием «Подписки на уведомления по кандидатам на этапах» (`https://api.talantix.ru/docs/api/unions/subscriptions`). Это внутренние подписки рекрутера на этапы вакансии, не HTTP-колбэки.

⇒ **ВЫВОД**: интеграция вынужденно polling. Дешёвый поллинг — `calendarMeetings` по узкому окну (например, сегодня…+7 дней) с интервалом 5–15 минут; полный бэкафилл — раз в сутки по 93-дневным окнам.

### 1.8. ФАКТЫ: iCal / CalDAV / Zapier / Make / n8n / клиентские библиотеки

- **iCal/CalDAV**: ни в документации API, ни в базе знаний нет ни слова про iCal-фид или CalDAV. Календарная интеграция сделана как OAuth-приложение к Google/Outlook/Яндекс/Битрикс24, а не как подписка на `.ics`.
- **Zapier**: страница `https://zapier.com/apps/talantix/integrations` — приложение Talantix в каталоге Zapier не найдено. Более того, Zapier официально прекратил продажи в Россию (`https://zapier.com/l/us-sanctions`), так что как канал это нерабочий вариант.
- **Make (Integromat)**: `https://www.make.com/en/integrations/talantix` отдаёт общую страницу каталога, а не приложение Talantix. Приложения нет.
- **n8n**: community-node `n8n-nodes-talantix` в npm не существует (см. ниже).
- **npm**: `https://registry.npmjs.org/-/v1/search?text=talantix` → `{"objects":[],"total":0}`. Пакетов нет.
- **PyPI**: `https://pypi.org/pypi/talantix/json` → HTTP 404, пакета нет.
- **GitHub**: `https://api.github.com/search/repositories?q=talantix` → 32 репозитория, все — тестовые задания на вакансию в Talantix (`TalantixTest`, `talantix-test-task` и т.п.), поддерживаемого клиента API нет. Готового SDK нет ни для одного языка.
- **Официальные интеграции** (из базы знаний, статья «Мой профиль» `article/settings`): hh.ru, Зарплата.ру, Авито Работа, Почта, Календарь, Яндекс Телемост, Zoom, Google Meet; плюс приложение Talantix в маркетплейсе Битрикс24. Отдельно упоминается интеграция с «Джейкет.Работа» через API.
- **Партнёрская программа**: отдельной публичной страницы для разработчиков/партнёров не найдено. Публичного «API для интеграций» за пейволом, кроме тарифного ограничения, нет.

---

## 2. Календарь: что именно даёт API

### 2.1. ФАКТЫ: чтение

`https://api.talantix.ru/docs/api/queries/calendarMeetings`:

```
calendarMeetings(end: Instant!, start: Instant!): CalendarMeetings!
```

«Получить список встреч за указанный диапазон дат (в UTC, максимальный диапазон - 93 дня)». Возвращает `union CalendarMeetings = CalendarMeetingsError | CalendarMeetingsItem`, где `CalendarMeetingsItem { items: [CalendarMeetingItem!]! }`.

`CalendarMeetingItem` (`https://api.talantix.ru/docs/api/objects/calendar-meeting-item`):

```
id: Int!
topic: String
place: String
comment: String
startDate: Instant!
endDate: Instant!
timezone: Timezone
canEdit: Boolean!
createdAt: Instant!
updatedAt: Instant!
creatorManager: CompanyManager!
managers: [CompanyManager!]!
persons: [MeetingPerson!]!
vacancies: [MeetingVacancy!]!
externalVideoConference: ExternalVideoConference
externalCalendarMeeting: ExternalCalendarMeeting
```

`ExternalCalendarMeeting { calendarType: CalendarIntegrationType!, eventId: String! }` — то есть у встречи, созданной в Talantix, **есть ID события во внешнем календаре**. Это позволяет нам потом найти/обновить событие в Google/Outlook, не храня его отдельно.

`https://api.talantix.ru/docs/api/queries/calendar`:

```
calendar: Calendar!
type Calendar { hasMeetings: Boolean! }
```

«Информация о календаре **текущего менеджера**» — и всё, что он отдаёт, это флаг «есть ли встречи». **Отдельного free/busy-запроса нет.** Ни `freeBusy`, ни `availability`, ни `slots` в схеме нет (полный список queries см. выше).

`timezones` → `Timezones { items: [Timezone!]! }`; `Timezone` — `{ id, name }`, id в нижнем регистре, например `europe/moscow` (видно в примере ответа `https://api.talantix.ru/docs/guides/examples/calendar`).

### 2.2. ФАКТЫ: запись

`createCalendarMeeting(input: CalendarMeetingCreateInput!): CalendarMeetingCreated!` (`https://api.talantix.ru/docs/api/mutations/create-calendar-meeting`).

Поля `CalendarMeetingCreateInput` (собрано из страниц input'ов):
- `period: LocalizedPeriodInput!` — `{ start, end, timezoneId }`, где `start`/`end` — «Локальная дата и время в формате ISO-8601, **без смещения**», `timezoneId` — «Идентификатор временной зоны для start и end» (`/docs/api/inputs/localized-period-input`). То есть сервер сам переводит локальное время в UTC по указанной зоне.
- `managerIds: [Int!]!` — максимум 20;
- `personIds: [Int!]` — максимум 50;
- `vacancyIds: [Int!]` — максимум 20;
- `topic: String` — максимум 512;
- `place: String` — максимум 512;
- `comment: String` — HTML, до 20 000;
- `personEmail: CalendarMeetingPersonEmailInput` — `{ fileIds, subject (≤128), text (≤50000, HTML) }` — письмо-приглашение кандидату;
- `personSmsText: String` — до 70 символов;
- `sendEmailToManagers: Boolean` — «Отправить Email-уведомления менеджерам»;
- `externalCalendarType: CalendarIntegrationType` — в какой внешний календарь продублировать;
- `externalVideoConferenceType` — тип видеоконференции.

`updateCalendarMeeting(input: CalendarMeetingUpdateInput!)` и `deleteCalendarMeeting(...)` — есть, с типами результата `CalendarMeetingUpdated` / `CalendarMeetingDeleted` (union с error-ветками).

`CalendarIntegrationType` = `BITRIX | GOOGLE | OUTLOOK | YANDEX` (`https://api.talantix.ru/docs/api/enums/calendar-integration-type`).

`CalendarIntegration` (`/docs/api/objects/calendar-integration`): `{ customizable: Boolean!, dummy: Boolean!, enabled: Boolean!, enabledType: CalendarIntegrationType, login: String, redirectUrls: CalendarRedirectUrls! }`. Читается через `me { integrations(levels, types) }` — то есть мы можем узнать, подключён ли у менеджера внешний календарь и какой именно.

### 2.3. ФАКТЫ: синхронизация с Google/Outlook/Яндекс/Битрикс24

Статья «Как подключить свой рабочий календарь к Talantix» — `https://feedback.hh.ru/talantix/knowledge-base/article/00047474`:

- «**Функция доступна на любом тарифе.**»
- «Вы можете подключить: календарь Outlook; календарь Google; календарь Яндекс; календарь «Битрикс24»».
- Подключение: «Календарь» → «Подключить календарь», либо «Мой профиль» → «Интеграции» → блок «Календарь» → «Подключить».
- Для Google на экране согласия запрашивается scope «**Просмотр и изменение мероприятий во всех ваших календарях**» + доступ к Google Meet. Для Outlook — условия Microsoft Graph (доступность домена, часовой пояс, корпоративная почта); для Яндекса — «доступ к вашему аккаунту в Яндексе», включая «просмотр и изменение событий в календаре»; для Битрикс24 — установка приложения из маркетплейса, затем вставка ссылки на портал.
- Про Битрикс24: «события будут автоматически отображаться в календаре «Битрикс24» с темой, комментарием и ссылкой на видеовстречу».

Статья «Работа с календарём в Talantix» — `https://feedback.hh.ru/talantix/knowledge-base/article/0057`:

- «После подключения **все события, созданные в Talantix**, будут мгновенно появляться и в вашем внешнем календаре, а из внешнего календаря можно будет быстро перейти к вакансии и резюме кандидата (кроме Яндекс Календаря — переход по ссылкам не предусмотрен)».
- При создании события: «Если подключён рабочий календарь, отметьте опцию «**Добавить событие в рабочий календарь**» — встреча синхронизируется с ним автоматически».

**Ключевой момент:** нигде не сказано, что события, созданные *во внешнем* календаре, попадают в календарь Talantix. Описанное направление — Talantix → внешний календарь плюс обратные ссылки внутри события. Формально scope Google даёт право читать все календари, но документированного поведения «внешнее событие → блок в календаре Talantix» нет.

⇒ **ВЫВОД**: двусторонней синхронизации в смысле «записал в Google — появилось в Talantix» документально нет. Считать её таковой нельзя. Это опция (b) из задачи — и она **нереалистична** как основной механизм брони.

### 2.4. Что реально можно узнать про занятость

- **ФАКТ**: `calendarMeetings` возвращает встречи, а не интервалы занятости. Все поля — про конкретную встречу (`startDate`, `endDate`, `managers`, `persons`, `topic`).
- **ФАКТ**: `calendar` даёт только `hasMeetings` для текущего менеджера.
- **НЕ ПРОВЕРЕНО**: возвращает ли `calendarMeetings` встречи *других* менеджеров (всех в компании, только тех, где текущий — участник, или вообще только свои). В документации написано «Список встреч за указанный период» без уточнения области видимости; `calendar` явно помечен как «текущего менеджера». Это первое, что нужно проверить на живом тенанте: создать встречу с менеджером B, запросить `calendarMeetings` токеном менеджера A и посмотреть, видна ли она.
- **ВЫВОД**: если область видимости ограничена участником, то для чтения доступности интервьюера нужен **его собственный токен** — то есть схема «один токен на менеджера» либо «токен администратора, который видит всё». Токены создаются только администратором, поэтому «токен на менеджера» — это административная работа на каждого интервьюера.
- **ВЫВОД**: рабочих часов/графика работы менеджера в API нет. `CompanyManager` (`/docs/api/objects/company-manager`) = `{ blocked, email, firstName, hhManagerId, id, lastName, licenseType, managerRole, middleName }` — ни часового пояса, ни графика. Сетку слотов (09:00–18:00, обед, буферы) придётся хранить у себя.

---

## 3. Free/busy: чего не хватает и что это значит для логики слотов

Это ответ на вопрос 4 задачи — но применительно к API, а не к iCal. iCal-фида у Talantix нет вообще, поэтому сценарий «только iCal» здесь не возникает; зато есть более точный аналог — «только `calendarMeetings`».

**Что мы знаем из `calendarMeetings`:**
- точные интервалы занятости (`startDate`–`endDate` в UTC);
- кто участник (`managers`, `persons`) — то есть можно построить занятость по каждому менеджеру;
- тему, место, вакансию, кандидата, видеоконференцию;
- `canEdit` — можно ли править эту встречу нашим токеном.

**Чего мы не знаем:**
- свободных слотов в явном виде — их надо вычислять;
- рабочих часов и предпочтений интервьюера (нет в API);
- занятости вне Talantix: личные события, встречи в Google/Outlook, которые не попали в Talantix, поездки, отпуска (если они не заведены встречей в Talantix);
- занятости других менеджеров, если область видимости `calendarMeetings` ограничена — **НЕ ПРОВЕРЕНО**;
- буферов между встречами (переезд между комнатами, 15 минут на заметки) — это наша политика, не данные.

**Последствия для логики слотов:**
1. Слот = `[workWindow] − [union(busy по всем участникам)] − [buffer до/после] − [уже предложенные/забронированные нами слоты]`. Считать надо по **UTC**, а сетку строить в локальной зоне каждого участника (таймзона участника берётся из `Timezone` встречи или из нашего справочника менеджеров).
2. «Дырка» в календаре Talantix ≠ «свободен»: интервьюер может быть в отпуске, на больничном, в чужой встрече, которая не заведена в Talantix. Поэтому **всегда** показывать кандидату 2–3 варианта и оставлять подтверждение, а не бронировать молча.
3. Нельзя обещать «свободен» — можно обещать только «в Talantix нет конфликта». Это надо писать в UI/письме.
4. Кандидатские слоты лучше предлагать «мягко» (2–3 окна с запасом), потому что отказ по конфликту стоит дорого: переписка, потеря кандидата.
5. Если участников несколько, пересечение занятости надо считать по каждому; лимит `managerIds` ≤ 20 — это и есть практический потолок группы.
6. Так как webhooks нет, между чтением и записью проходит время → окно гонки. Нужен lock на слоте и повторная проверка непосредственно перед созданием.

---

## 4. Альтернативы, если писать в Talantix нельзя/не хочется

| Вариант | Что даёт | Ограничения | Годность для брони |
|---|---|---|---|
| **Google Calendar API** | `freebusy.query` (занятость по списку календарей, без имён событий), `events.insert` с `conferenceData` (Meet), `events.patch` с `If-Match`/ETag, инкрементальный `syncToken`, `watch`-каналы | Нужен OAuth на каждого интервьюера или service account + domain-wide delegation (только Google Workspace). `freebusy` требует как минимум `freeBusyReader` на календарь. Watch-каналы живут ≤ 7 дней и требуют продления. | **Высокая.** Лучший free/busy в классе. |
| **Microsoft Graph** | `POST /users/{id}/calendar/getSchedule` (готовый `availabilityView` по 30-мин. слотам, `scheduleItems` с `busy`/`tentative`/`oof`), `POST /users/{id}/events` с `isOnlineMeeting` + `onlineMeetingProvider=teamsForBusiness`, `PATCH` с `If-Match` | Нужны app-разрешения `Calendars.Read` / `Calendars.ReadWrite` (+ `Calendars.ReadBasic.All` для чтения), tenant admin consent. Подписки на изменения для календаря живут ≤ 3 дней. Имена зон: Graph исторически на Windows-зонах, сейчас поддерживает IANA через `Prefer: outlook.timezone="..."`. | **Высокая**, особенно в корпоративном сегменте. |
| **CalDAV** | Открытый протокол, `REPORT` с `calendar-query`/`free-busy-query`, `PUT` события | Free/busy в CalDAV — отдельное расширение планирования (RFC 6638), его поддерживают далеко не все; Google CalDAV закрыт с 2013 г.; push (WebDAV-Push) почти нигде не работает → только polling. | **Средняя/низкая.** Реалистично для Яндекс.Календаря, Mail.ru, iCloud, Zimbra. |
| **iCal (.ics) подписка** | Только чтение, только «занято/свободно» | Нет имён, нет участников, нет ID события для обновления, нет отмены в реальном времени (обновление по расписанию), нет приватных событий (только `TRANSP:OPAQUE`). | **Только как fallback для чтения.** |

**Про часовые пояса и DST:**
- Хранить всё в UTC (`timestamptz`), конвертировать в зону участника на границе. Никогда не хранить «наивное» локальное время как истину.
- Для Talantix `LocalizedPeriodInput` передавать **локальное ISO-8601 без смещения + `timezoneId`** (`europe/moscow`), а не UTC: сервер сам переведёт. Если передать UTC-строку со смещением, есть риск двойной конвертации — **НЕ ПРОВЕРЕНО**, но синтаксис в документации явно «без смещения».
- В России DST отменён с 2014 г., но: (а) в других странах DST есть; (б) исторические события и переходы 2014/2020 гг. требуют актуальной tzdata; (в) библиотеки (Python `zoneinfo`, JS `Intl`/`Temporal`) берут tzdata из разных источников — версии могут расходиться. Обновлять tzdata и не кэшировать смещения.
- Google/Graph возвращают время со смещением и зоной; при `events.insert` в Google поле `timeZone` (IANA) обязательно, иначе сервер применит зону календаря.
- Осторожно с полночью и «24:00»: слоты, пересекающие сутки, ломают наивные date-парсеры.

---

## 5. Рекомендуемая архитектура

**Принцип: Talantix — система записи (system of record) для собеседования; наш сервис — система записи для брони (booking state) и владелец бизнес-логики слотов.**

Поток:

1. **Чтение доступности — из Talantix.** `calendarMeetings(start, end)` окнами ≤ 93 дня, по каждому интервьюеру (если видимость позволяет) или одним токеном, который видит всё. Плюс `me { integrations }`, чтобы понять, подключён ли у человека внешний календарь.
2. **Дополнение занятости — из внешних календарей.** Для интервьюеров с подключённым Google/Outlook — `freebusy.query` / `getSchedule`, чтобы поймать личные и внешние блоки, которых нет в Talantix. Это закрывает главную дыру «дырка в Talantix ≠ свободен».
3. **Построение слотов** в нашей БД: сетка рабочих часов + буферы + вычитание объединённой занятости + округление к 15/30 мин + проверка «не в прошлом» + проверка лимита 93 дня на окно запроса.
4. **Предложение кандидату** 2–3 слота (ссылка/письмо). Никакого «молчаливого» бронирования.
5. **Бронь — через `createCalendarMeeting`** с `externalCalendarType = GOOGLE | OUTLOOK | YANDEX | BITRIX`, если у интервьюера подключён внешний календарь. Так событие появляется и в Talantix, и в его календаре — без двусторонней синхронизации.
6. **Подтверждение брони** — перечитать `calendarMeeting(id)` / `calendarMeetings` и убедиться, что встреча создана и время совпадает. Сохранить `id` встречи и `externalCalendarMeeting.eventId` в нашей БД.
7. **Отмена/перенос** — `updateCalendarMeeting` / `deleteCalendarMeeting`, а не удаление события в Google.
8. **Опрос** — `calendarMeetings` по узкому окну каждые 5–15 минут, чтобы ловить отмены и переносы, сделанные руками в Talantix.

**Идемпотентность и защита от гонок:**
- Таблица `booking` с уникальным ключом `(talantix_meeting_id)` и `(slot_hash, status='pending')`, TTL на pending.
- Перед `createCalendarMeeting` — транзакционный захват слота (`SELECT ... FOR UPDATE` / advisory lock).
- `createCalendarMeeting` не выглядит как «проверяющий конфликты» (в документации нет ни слова про валидацию пересечений) — **ВЫВОД**: конфликт он, скорее всего, не отклонит. Значит, проверка конфликтов — наша ответственность, а после создания обязательна верификация.
- Идемпотентный маркер в `comment`/`topic` (например, `[bot:req-123]`), чтобы при повторе найти уже созданную встречу через `calendarMeetings` и не создать дубль.

**Токены:**
- Хранить refresh token в секрет-хранилище, обновлять **строго последовательно** (single-flight), потому что refresh одноразовый и ротация убивает старую пару.
- Планировать фоновое обновление до истечения 24 ч, не «по факту 401».
- Знать, что при смене роли/блокировке админа все токены умирают → нужен алерт и процедура перевыпуска (ручная, через ЛК).

### 5.1. Failure modes (что сломается и как это пережить)

| # | Отказ | Последствие | Митигация |
|---|---|---|---|
| 1 | `access_token` истёк (24 ч) | 401, все запросы падают | Проактивное обновление, retry с backoff, метрика «возраст токена» |
| 2 | Два воркера одновременно обновляют refresh | Ротация: один получил новый, второй — ошибку; возможна потеря пары | Single-flight + распределённый лок на обновление |
| 3 | Админ сменил роль / заблокирован | Все токены недействительны, интеграция встала | Алерт, документированная процедура перевыпуска, «запасной» админ |
| 4 | 120 дней без использования | Токены деактивированы | Heartbeat-запрос раз в сутки |
| 5 | 429 / `query_complexity_exceeded` / `root_selection_exceeded` | Часть запросов отклонена | Один корень на запрос, complexity ≤ 2500, exponential backoff, очередь, кэш |
| 6 | Окно > 93 дней | Ошибка запроса | Нарезать диапазоны, склеивать результаты |
| 7 | Гонка: слот заняли между чтением и записью | Двойная бронь | Lock + перепроверка + верификация после создания |
| 8 | Событие создано, но клиент не получил ответ (таймаут) | Дубль при повторе | Идемпотентный маркер + поиск по `calendarMeetings` перед повтором |
| 9 | Внешний календарь не подключён | Событие только в Talantix, интервьюер его не видит | Проверять `me.integrations`, при отсутствии — уведомление рекрутеру |
| 10 | Синхронизация Talantix→внешний календарь не сработала (например, отозван OAuth) | Событие в Talantix есть, в Google нет | `ExternalCalendarMeeting` пустой → алерт, повторная попытка через `updateCalendarMeeting` |
| 11 | Занятость вне Talantix не видна | «Свободный» слот занят личной встречей | Подтягивать freebusy из Google/Graph, предлагать 2–3 варианта |
| 12 | Нет webhooks | Отмена, сделанная руками, обнаруживается с задержкой | Поллинг 5–15 мин, TTL на предложенные слоты, повторная проверка перед показом |
| 13 | Часовой пояс перепутан (UTC vs локальное) | Встреча на 3 часа не туда | Хранить UTC, передавать в Talantix локальное + `timezoneId`, тесты на DST |
| 14 | Письма/СМС кандидату уходят при каждом retry | Спам, жалобы | `personEmail`/`personSmsText` — только при первом создании; идемпотентность по маркеру |
| 15 | Нет оплаченного рабочего места у владельца токена | Запись запрещена, чтение работает | Проверять на онбординге, требовать тариф «Оптимальный»/«Продвинутый» |
| 16 | Изменение схемы GraphQL | Ломается запрос | Фиксировать версию полей в коде, мониторить ошибки валидации, держать интеграционные тесты |

### 5.2. Что проверить на живом тенанте (до проектирования)

1. Видит ли `calendarMeetings` встречи других менеджеров; если да — при каких ролях.
2. Отклоняет ли `createCalendarMeeting` конфликт по времени (пересечение с существующей встречей).
3. Реальный rate limit (запросов/мин) и поведение при 429.
4. Действительно ли `externalCalendarType` создаёт событие в Google/Outlook и заполняет `externalCalendarMeeting`.
5. Двусторонность: создать событие напрямую в Google и проверить, появилось ли оно в календаре Talantix (ожидание — нет).
6. Требуется ли оплаченное место для `createCalendarMeeting` при чтении без места.
7. Можно ли получить токен не-администратору (в документации — только администратор).

---

## 6. Сводка по вопросам задачи

1. **API есть, публичный и документированный**, GraphQL, `https://api.talantix.ru/graphql`, Bearer-токен, refresh-токен с ротацией, доступ по тарифу. Webhooks нет. Пагинация курсорная. Rate limit численно не опубликован; известны лимиты сложности (~2500) и «один корень на запрос».
2. **Календарь**: чтение — `calendarMeetings` (≤ 93 дня, UTC), запись — `createCalendarMeeting` / `updateCalendarMeeting` / `deleteCalendarMeeting`. Free/busy-эндпоинта нет, доступность считаем сами. Приоритет: (a) нативный API — **реалистично и является основным путём**; (b) двусторонняя синхронизация Google/Outlook — **не подтверждена, направление Talantix→внешний**; (c) iCal — **отсутствует**; (d) «только ATS API» — это и есть (a).
3. **Интеграция с Google/Outlook есть** (плюс Яндекс и Битрикс24), но по документации — односторонняя из Talantix во внешний календарь, с обратными ссылками. См. `article/00047474` и `article/0057`.
4. **Free/busy по iCal**: неприменимо (фида нет). По `calendarMeetings` знаем интервалы и участников, но не рабочие часы и не занятость вне Talantix → предлагать варианты, а не бронировать молча.
5. **Альтернативы**: Google Calendar API (лучший freebusy), Microsoft Graph (`getSchedule` + `availabilityView`), CalDAV (ограниченно). Часовые пояса — UTC внутри, IANA-зоны на границе, актуальная tzdata.
6. **Рекомендация**: Talantix — источник истины по встрече, наш сервис — по брони; читать доступность из Talantix (+ внешний freebusy), писать через `createCalendarMeeting` с `externalCalendarType`. Не полагаться на запись в Google как на способ создать встречу в Talantix.
