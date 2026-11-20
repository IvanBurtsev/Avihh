"""Тексты GraphQL-запросов Talantix.

Жёсткие правила платформы, которые учтены в каждом запросе:

* **одно корневое поле на операцию** (``HTTP 269 root_selection_exceeded``):
  в каждом ``query``/``mutation`` ровно одно корневое поле; ``__typename`` и
  фрагменты в лимит не входят;
* **сложность ≈ 2500** (``HTTP 210 query_complexity_exceeded``): не запрашиваем
  всю схему «на всякий случай», только нужные поля;
* бизнес-ошибки приходят как объекты в ``data`` (union-ветки ``*Error``),
  технические — в ``errors[]``; поэтому в каждом запросе явно перечислены
  обе ветки.

Про ``Instant``: это скаляр в **миллисекундах** epoch, поэтому аргументы
``start``/``end`` передаются числами (см. ``mapper.to_millis``).
"""

from __future__ import annotations

__all__ = [
    "CALENDAR_MEETINGS_QUERY",
    "CALENDAR_MEETING_QUERY",
    "CREATE_CALENDAR_MEETING_MUTATION",
    "DELETE_CALENDAR_MEETING_MUTATION",
    "MANAGERS_QUERY",
]

#: Общий набор полей встречи. Фрагмент используется в нескольких операциях,
#: чтобы не дублировать описание и не «стоить» дороже по сложности запроса.
MEETING_FIELDS_FRAGMENT = """
fragment CalendarMeetingFields on CalendarMeetingItem {
  id
  topic
  place
  comment
  startDate
  endDate
  canEdit
  timezone { id name }
  managers { id firstName lastName email }
  externalCalendarMeeting { calendarType eventId }
}
"""

#: ``calendarMeetings(end: Instant!, start: Instant!)`` — окно максимум 93 дня.
#: Пагинации у этого корня нет: это запрос по диапазону дат, поэтому
#: ``first``/``after`` здесь неприменимы (курсорная пагинация — у ``managers``).
CALENDAR_MEETINGS_QUERY = (
    MEETING_FIELDS_FRAGMENT
    + """
query CalendarMeetings($start: Instant!, $end: Instant!) {
  calendarMeetings(start: $start, end: $end) {
    __typename
    ... on CalendarMeetingsItem {
      items {
        ...CalendarMeetingFields
      }
    }
    ... on CalendarMeetingsError {
      errorType
      message
    }
  }
}
"""
)

#: ``calendarMeeting(id: Int!)`` — обязательный шаг верификации после создания.
CALENDAR_MEETING_QUERY = (
    MEETING_FIELDS_FRAGMENT
    + """
query CalendarMeeting($id: Int!) {
  calendarMeeting(id: $id) {
    __typename
    ...CalendarMeetingFields
    ... on CalendarMeetingError {
      errorType
      message
    }
  }
}
"""
)

#: ``createCalendarMeeting(input: CalendarMeetingCreateInput!)``.
#: Возвращаемый union: ``CalendarMeetingCreateError | CalendarMeetingItem``.
CREATE_CALENDAR_MEETING_MUTATION = (
    MEETING_FIELDS_FRAGMENT
    + """
mutation CreateCalendarMeeting($input: CalendarMeetingCreateInput!) {
  createCalendarMeeting(input: $input) {
    __typename
    ...CalendarMeetingFields
    ... on CalendarMeetingCreateError {
      errorType
      message
    }
  }
}
"""
)

#: ``deleteCalendarMeeting(id: Int!)``; union ``CalendarMeetingDeleteError | CalendarMeetingDeleteSuccess``.
DELETE_CALENDAR_MEETING_MUTATION = """
mutation DeleteCalendarMeeting($id: Int!) {
  deleteCalendarMeeting(id: $id) {
    __typename
    ... on CalendarMeetingDeleteSuccess {
      id
    }
    ... on CalendarMeetingDeleteError {
      errorType
      message
    }
  }
}
"""

#: ``managers(first, after, filter, sortType)`` — курсорная пагинация,
#: ``first`` не больше 50. Нужна, чтобы получить числовые ID менеджеров:
#: в настройках они заданы строками, а API Talantix принимает только Int.
MANAGERS_QUERY = """
query Managers($first: Int, $after: String) {
  managers(first: $first, after: $after) {
    __typename
    ... on Managers {
      items {
        id
        firstName
        lastName
        email
        blocked
        managerRole
      }
      pageInfo {
        endCursor
        first
        hasNextPage
      }
    }
    ... on ManagersError {
      errorType
      message
    }
  }
}
"""
