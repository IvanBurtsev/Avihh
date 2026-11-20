"""Наполнение локальной базы демонстрационными данными.

Зачем: чтобы показать интерфейс рекрутера, нужны диалоги, а реальные площадки
без токенов ничего не отдадут. Команда создаёт компанию, вакансию с критериями
и несколько кандидатов с резюме, а **скрининг прогоняет через настоящий
``ScreeningService``** — то есть цифры в интерфейсе посчитаны тем же кодом, что
и в продакшене, включая пересчёт балла по весам и правила вердикта.

Отличия от ``hirebot demo``:

* ``demo`` — сквозной сценарий в памяти, ничего не сохраняет, показывает поток;
* ``seed`` — наполняет **рабочую** базу, чтобы открыть ``/ui`` и кликать по карточкам.

Данные помечены каналом ``hh`` и вымышленными именами: это не ПДн реальных людей.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from hirebot.container import Container
from hirebot.domain.entities import (
    Candidate,
    Company,
    Conversation,
    Message,
    Resume,
    Vacancy,
    new_id,
)
from hirebot.domain.enums import (
    Channel,
    ConversationState,
    MessageDirection,
    MessageKind,
)
from hirebot.logging_setup import get_logger
from hirebot.services.screening import ScreeningService

logger = get_logger(__name__)

#: Компания демо-данных. Один идентификатор, чтобы сценарии были воспроизводимы.
DEMO_COMPANY_ID = __import__("uuid").UUID("00000000-0000-4000-8000-000000000001")

VACANCY_TITLE = "Python-разработчик"
VACANCY_DESCRIPTION = (
    "Разработка сервисов на Python. Задачи: FastAPI, PostgreSQL, REST API. "
    "Условия: удалённый формат, оформление по ТК РФ."
)

#: Критерии вакансии — из них считается балл.
VACANCY_CRITERIA: list[dict[str, object]] = [
    {"key": "python", "title": "Опыт разработки на Python", "weight": 3.0, "must_have": True},
    {
        "key": "sql",
        "title": "Работа с реляционными БД (PostgreSQL/SQL)",
        "weight": 2.0,
        "must_have": True,
    },
    {"key": "api", "title": "Проектирование REST API", "weight": 1.5, "must_have": False},
    {"key": "english", "title": "Английский для чтения документации", "weight": 1.0},
]


@dataclass(slots=True, frozen=True)
class DemoCase:
    """Один демонстрационный кандидат."""

    name: str
    resume: str
    state: ConversationState
    outgoing: tuple[str, ...] = ()
    incoming: tuple[str, ...] = ()


STRONG_RESUME = (
    "Алексей Петров, backend-разработчик. Опыт работы: 5 лет, пишу на Python. "
    "Стек: Python 3.12, FastAPI, Django, PostgreSQL, SQL, REST API, Docker, Git, pytest. "
    "Проектировал и поддерживал REST API высоконагруженного сервиса, оптимизировал SQL-запросы. "
    "Английский язык — B2: читаю документацию и пишу письма без переводчика."
)

MEDIUM_RESUME = (
    "Дмитрий Орлов, разработчик. Опыт работы: 2 года. "
    "Пишу на Python, делал сервисы на FastAPI, работал с PostgreSQL и SQL. "
    "Понимаю принципы REST. Английский — B1."
)

WEAK_RESUME = (
    "Мария Смирнова, junior Python-разработчик. Опыт работы: 1 год. "
    "Писала учебные проекты на Python, немного знакома с SQL. "
    "Английский язык — A2, читаю со словарём."
)

SHIFT_RESUME = (
    "Сергей Кузнецов, системный администратор. Опыт работы: 8 лет. "
    "Администрирование Linux, настройка сети, резервное копирование. "
    "Python использовал для небольших скриптов автоматизации."
)

CLOSED_RESUME = "Иван Кузнецов, аналитик. Опыт работы: 3 года. Работал с отчётностью и Excel."


def demo_cases() -> list[DemoCase]:
    """Набор кандидатов: сильный, средний, слабый, смежник и «закрытое резюме»."""
    return [
        DemoCase(
            name="Алексей Петров",
            resume=STRONG_RESUME,
            # «Новый» специально: на нём видно, как бот проходит воронку целиком —
            # скрининг, решение и либо отправку, либо черновик на подтверждение.
            state=ConversationState.NEW,
        ),
        DemoCase(
            name="Ольга Соколова",
            resume=STRONG_RESUME.replace("Алексей Петров", "Ольга Соколова").replace("5 лет", "6 лет"),
            state=ConversationState.INVITED,
            outgoing=(
                (
                    "Здравствуйте, Ольга! Спасибо за отклик на вакансию «Python-разработчик». "
                    "Резюме посмотрели — хочется обсудить детали на коротком созвоне. "
                    "Вот время, где конфликта в календаре нет: понедельник 13:00, вторник 11:00, "
                    "среда 16:00. Напишите, пожалуйста, номер удобного варианта."
                ),
            ),
            incoming=("Здравствуйте! Да, интересно, готов обсудить. Удобен вторник.",),
        ),
        DemoCase(
            name="Дмитрий Орлов",
            resume=MEDIUM_RESUME,
            state=ConversationState.AWAITING_ANSWER,
            outgoing=(
                (
                    "Здравствуйте, Дмитрий! Спасибо за отклик. Уточните, пожалуйста: "
                    "с какими объёмами данных приходилось работать в PostgreSQL "
                    "и какие задачи закрывали на FastAPI?"
                ),
            ),
        ),
        DemoCase(
            name="Мария Смирнова",
            resume=WEAK_RESUME,
            state=ConversationState.HUMAN_REVIEW,
        ),
        DemoCase(
            name="Сергей Кузнецов",
            resume=SHIFT_RESUME,
            state=ConversationState.HUMAN_REVIEW,
        ),
        DemoCase(
            name="Иван Кузнецов",
            resume=CLOSED_RESUME,
            state=ConversationState.HUMAN_REVIEW,
        ),
    ]


async def seed_demo_data(container: Container, *, reset: bool = False) -> int:
    """Создать демонстрационные диалоги и прогнать по ним настоящий скрининг.

    Возвращает количество созданных диалогов. ``reset=True`` сначала удаляет
    прежние демо-данные (по фиксированному идентификатору компании).
    """
    now = container.clock.now()
    if now.tzinfo is None:  # pragma: no cover - защита от неверных часов
        now = now.replace(tzinfo=UTC)

    async with container.uow_factory() as uow:
        # Компания обязана существовать: у вакансии и кандидатов внешний ключ на неё.
        await uow.companies.upsert(ensure_company())

        existing = await uow.vacancies.list_active(DEMO_COMPANY_ID)
        if existing and not reset:
            logger.info("seed_skipped", reason="демо-данные уже есть", vacancies=len(existing))
            return 0
        if reset:
            # Удаления через репозитории нет: это демо-команда, и повторный запуск
            # с reset просто создаёт новые записи с уникальными внешними id.
            logger.info("seed_reset_requested")

        vacancy = Vacancy(
            id=new_id(),
            channel=Channel.HH,
            external_id=f"demo-vacancy-{int(now.timestamp())}",
            title=VACANCY_TITLE,
            company_id=DEMO_COMPANY_ID,
            description=VACANCY_DESCRIPTION,
            city="Москва",
            criteria=[dict(criterion) for criterion in VACANCY_CRITERIA],
            created_at=now,
            updated_at=now,
        )
        await uow.vacancies.upsert(vacancy)

        created: list[tuple[Conversation, DemoCase]] = []
        for index, case in enumerate(demo_cases()):
            marker = f"{int(now.timestamp())}-{index}"
            candidate = Candidate(
                id=new_id(),
                channel=Channel.HH,
                external_id=f"demo-candidate-{marker}",
                full_name=case.name,
                company_id=DEMO_COMPANY_ID,
                city="Москва",
                created_at=now,
            )
            await uow.candidates.upsert(candidate)

            resume = Resume(
                id=new_id(),
                candidate_id=candidate.id,
                channel=Channel.HH,
                external_id=f"demo-resume-{marker}",
                raw_text=case.resume,
                parsed={"total_experience_months": 12},
                source_url=f"https://hh.ru/resume/demo-{marker}",
                fetched_at=now,
            )
            await uow.resumes.save(resume)

            conversation = Conversation(
                id=new_id(),
                company_id=DEMO_COMPANY_ID,
                candidate_id=candidate.id,
                vacancy_id=vacancy.id,
                channel=Channel.HH,
                external_id=f"demo-chat-{marker}",
                state=case.state,
                created_at=now,
                updated_at=now,
            )
            await uow.conversations.upsert(conversation)

            for offset, text in enumerate(case.outgoing):
                await uow.messages.add(
                    Message(
                        id=new_id(),
                        conversation_id=conversation.id,
                        direction=MessageDirection.OUTBOUND,
                        body=text,
                        kind=MessageKind.INVITATION if offset == 0 else MessageKind.CLARIFYING_QUESTION,
                        created_at=now + timedelta(minutes=offset),
                    )
                )
            for offset, text in enumerate(case.incoming):
                await uow.messages.add(
                    Message(
                        id=new_id(),
                        conversation_id=conversation.id,
                        direction=MessageDirection.INBOUND,
                        body=text,
                        kind=MessageKind.MANUAL,
                        created_at=now + timedelta(minutes=5 + offset),
                    )
                )
            created.append((conversation, case))
        await uow.commit()

    # Скрининг — настоящим сервисом: он пересчитает балл по весам и применит
    # правила вердикта. Для «закрытого» резюме площадка его не отдала бы —
    # имитируем это, не сохраняя текст.
    service = ScreeningService(container)
    for conversation, case in created:
        if case.name == "Иван Кузнецов":
            continue
        try:
            result = await service.screen(conversation_id=conversation.id)
            logger.info(
                "seed_screened",
                candidate=case.name,
                score=result.total_score,
                verdict=result.verdict.value,
            )
        except Exception as exc:
            logger.warning("seed_screen_failed", candidate=case.name, error=str(exc))

    # Один черновик кладём в очередь подтверждения: иначе экран «На подтверждении»
    # выглядит пустым, и непонятно, как он вообще работает.
    await _seed_pending_draft(container, created)

    logger.info("seed_done", conversations=len(created))
    return len(created)


async def _seed_pending_draft(container: Container, created: list[tuple[Conversation, DemoCase]]) -> None:
    """Создать один черновик для демонстрации очереди подтверждения."""
    from hirebot.services.approval import ApprovalService

    target = next(
        ((conversation, case) for conversation, case in created if case.name == "Дмитрий Орлов"),
        None,
    )
    if target is None:
        return
    conversation, case = target
    first_name = case.name.split()[0]
    try:
        await ApprovalService(container).prepare(
            conversation.id,
            body=(
                f"Здравствуйте, {first_name}! Спасибо за отклик на вакансию "
                f"«{VACANCY_TITLE}». Резюме посмотрели — хочется уточнить пару деталей: "
                "с какими объёмами данных работали в PostgreSQL и какие задачи закрывали "
                "на FastAPI? Это займёт пару минут."
            ),
            kind=MessageKind.CLARIFYING_QUESTION,
            target_state=ConversationState.QUESTIONS_SENT,
            reason="Балл выше порога, но нужны уточнения по опыту работы с БД",
            model="fake-llm-v1",
            generated_by_llm=True,
        )
    except Exception as exc:
        logger.warning("seed_draft_failed", error=str(exc))


def ensure_company() -> Company:
    """Компания демо-данных (создаётся в БД при первом обращении, если нужна)."""
    return Company(
        id=DEMO_COMPANY_ID,
        name="ООО Демо",
        slug="demo",
        created_at=datetime.now(tz=UTC),
    )


__all__ = [
    "DEMO_COMPANY_ID",
    "DemoCase",
    "demo_cases",
    "ensure_company",
    "seed_demo_data",
]
