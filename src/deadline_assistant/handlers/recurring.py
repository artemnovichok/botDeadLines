"""Inline setup and management of manually configured recurring deadlines."""

import secrets
from datetime import date, datetime
from html import escape
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from deadline_assistant.db.models import User
from deadline_assistant.domain import EntityNotFound, StaleState, utcnow
from deadline_assistant.handlers.event_actions import parse_id, short_id
from deadline_assistant.handlers.menus import PAGE_SIZE, keyboard
from deadline_assistant.services.recurring import MSK, RecurringService, resolve_first_due

if TYPE_CHECKING:
    from deadline_assistant.runtime import Runtime

WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")


class RecurringStates(StatesGroup):
    title = State()
    interval = State()
    day = State()
    date = State()
    time = State()
    confirm = State()
    stopping = State()


def recurrence_label(days: int) -> str:
    return "Каждый день" if days == 1 else f"Каждые {days} " + ("дня" if days < 5 else "дней")


async def show_recurring_card(
    message: Message, user: User, runtime: "Runtime", series_id: UUID
) -> None:
    async with runtime.database.transaction() as session:
        service = RecurringService(session, user.id)
        series = await service.get(series_id)
        if not series.enabled:
            await message.answer(
                "Отслеживание этого дедлайна закончено.",
                reply_markup=keyboard([[("Циклические дедлайны", "menu:recurring")]]),
            )
            return
        event = await service.current(series_id)
    assert event.due_at is not None
    title = escape(series.title)
    closed = event.status == "completed"
    title = f"<s>{title}</s>" if closed else title
    when = event.due_at.astimezone(ZoneInfo(series.timezone)).strftime("%d.%m.%Y %H:%M")
    zone = "МСК" if series.timezone == MSK else escape(series.timezone)
    rows = [
        [
            (
                "Закрыто ✓" if closed else "Закрыть",
                f"rc:{'closed' if closed else 'close'}:{short_id(event.id)}:{event.version}",
            )
        ],
        [("Изменить", f"rc:edit:{short_id(series.id)}:{series.version}")],
        [
            (
                "Закончить отслеживание",
                f"rc:stop:{short_id(series.id)}:{series.version}",
            )
        ],
        [("Назад", "menu:recurring"), ("Отмена", "menu:cancel")],
    ]
    await message.answer(
        f"🔁 <b>Циклический дедлайн</b>\n\n<b>{title}</b>\n"
        f"Ближайший срок: {when} · {zone}\n"
        + ("Без времени — до 23:59 МСК.\n" if not series.has_time else "")
        + recurrence_label(series.interval_days)
        + (
            "\n\nЭто повторение закрыто. Оно останется зачёркнутым до своего срока, "
            "затем появится следующее."
            if closed
            else "\n\n«Закрыть» завершает только это повторение. "
            "«Закончить отслеживание» отключает всю серию."
        ),
        parse_mode="HTML",
        reply_markup=keyboard(rows),
    )


def create_recurring_router(runtime: "Runtime") -> Router:
    router = Router(name="recurring")

    async def show_list(message: Message, user: User, state: FSMContext, page: int = 0) -> None:
        await state.clear()
        async with runtime.database.transaction() as session:
            service = RecurringService(session, user.id)
            series = await service.list_active()
            pages = max(1, (len(series) + PAGE_SIZE - 1) // PAGE_SIZE)
            page = max(0, min(page, pages - 1))
            rows = []
            for item in series[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
                event = await service.current(item.id)
                assert event.due_at is not None
                day = event.due_at.astimezone(ZoneInfo(item.timezone)).strftime("%d.%m")
                rows.append(
                    [
                        (
                            ("✓ " if event.status == "completed" else "🔁 ")
                            + item.title[:38]
                            + " · "
                            + day,
                            f"rc:open:{short_id(item.id)}",
                        )
                    ]
                )
        nav = []
        if page:
            nav.append(("←", f"rc:list:{page - 1}"))
        if page + 1 < pages:
            nav.append(("→", f"rc:list:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.extend(
            [
                [("Добавить дедлайн", "rc:new")],
                [("Мои дедлайны", "menu:deadlines"), ("Отмена", "menu:cancel")],
            ]
        )
        await message.answer(
            "🔁 <b>Циклические дедлайны</b>\n\n"
            + (
                f"Отслеживается: {len(series)} · страница {page + 1}/{pages}.\n"
                "Выберите дедлайн, чтобы закрыть повторение или изменить расписание."
                if series
                else "Добавьте регулярное задание один раз — сроки будут повторяться автоматически."
            ),
            parse_mode="HTML",
            reply_markup=keyboard(rows),
        )

    async def step(message: Message, user: User, state: FSMContext, name: str) -> None:
        token = secrets.token_hex(4)
        await state.update_data(panel_token=token)
        await state.set_state(getattr(RecurringStates, name))
        data = await state.get_data()
        prefix = f"rcw:{token}:"
        rows: list[list[tuple[str, str]]] = []
        if name == "title":
            text = "🔁 <b>Название · 1/5</b>\n\nВведите название дедлайна (до 300 символов)."
            if data.get("editing_version"):
                text += "\nСейчас: " + escape(data["title"])
                rows.append([("Оставить название", prefix + "keep")])
            previous = "list"
        elif name == "interval":
            text = (
                "🔁 <b>Периодичность · 2/5</b>\n\n"
                "Через сколько дней повторять? Выберите от 1 до 14."
            )
            rows = [
                [
                    (str(value), prefix + f"interval:{value}")
                    for value in range(start, min(start + 4, 15))
                ]
                for start in range(1, 15, 4)
            ]
            previous = "title"
        elif name == "day":
            text = (
                "🔁 <b>Первый срок · 3/5</b>\n\nВыберите ближайший день недели "
                "или введите дату ДД.ММ.ГГГГ.\n"
                "От первого срока повторяем каждые " + str(data["interval_days"]) + " дн."
            )
            rows = [
                [(WEEKDAYS[day], prefix + f"day:{day}") for day in range(start, min(start + 2, 7))]
                for start in range(0, 7, 2)
            ]
            rows.append([("Указать дату", prefix + "date")])
            previous = "interval"
        elif name == "date":
            text = (
                "🔁 <b>Первый срок · 3/5</b>\n\n"
                "Введите будущую дату ДД.ММ.ГГГГ, например 02.11.2026."
            )
            previous = "day"
        elif name == "time":
            text = (
                "🔁 <b>Время · 4/5</b>\n\nВведите время ЧЧ:ММ в поясе "
                + escape(user.timezone)
                + ".\nИли выберите «Без времени»: срок будет в 23:59 МСК."
            )
            rows = [
                [
                    (value, prefix + "time:" + value.replace(":", ""))
                    for value in ("12:00", "18:00")
                ],
                [("Без времени", prefix + "time:none")],
            ]
            previous = "day"
        elif name == "confirm":
            due = datetime.fromisoformat(data["first_due_at"])
            zone = data["timezone"]
            when = due.astimezone(ZoneInfo(zone)).strftime("%d.%m.%Y %H:%M")
            text = (
                "🔁 <b>Проверка · 5/5</b>\n\n<b>"
                + escape(data["title"])
                + "</b>\nПервый срок: "
                + when
                + " · "
                + ("МСК" if zone == MSK else escape(zone))
                + ("\nБез времени — до 23:59 МСК." if not data["has_time"] else "")
                + "\n"
                + recurrence_label(data["interval_days"])
                + "\nПовторяется, пока вы не закончите отслеживание."
            )
            rows = [[("Сохранить", prefix + "save")]]
            previous = "time"
        else:
            raise ValueError("Неизвестный шаг")
        rows.append([("Назад", prefix + "back:" + previous), ("Отмена", "menu:cancel")])
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard(rows))

    async def begin(
        message: Message,
        user: User,
        state: FSMContext,
        series_id: UUID | None = None,
        expected_version: int | None = None,
    ) -> None:
        await state.clear()
        data: dict[str, Any] = {"series_id": str(series_id or uuid4())}
        if series_id:
            async with runtime.database.transaction() as session:
                series = await RecurringService(session, user.id).get(series_id)
                if not series.enabled or series.version != expected_version:
                    raise StaleState("Расписание изменилось. Откройте карточку заново.")
                data.update(title=series.title, editing_version=series.version)
        await state.set_data(data)
        await step(message, user, state, "title")

    async def choose_time(
        message: Message, user: User, state: FSMContext, value: str | None
    ) -> None:
        data = await state.get_data()
        try:
            due_time = datetime.strptime(value, "%H:%M").time() if value else None
            day = data["weekday"] if "weekday" in data else date.fromisoformat(data["day"])
            first = resolve_first_due(day, due_time, user.timezone)
        except ValueError as exc:
            await message.answer(
                str(exc) or "Введите время ЧЧ:ММ.",
                reply_markup=keyboard([[("Отмена", "menu:cancel")]]),
            )
            await step(message, user, state, "time")
            return
        await state.update_data(
            first_due_at=first.isoformat(),
            has_time=due_time is not None,
            timezone=user.timezone if due_time is not None else MSK,
        )
        await step(message, user, state, "confirm")

    @router.callback_query(F.data == "menu:recurring")
    async def menu(callback: CallbackQuery, user: User, state: FSMContext) -> None:
        assert isinstance(callback.message, Message)
        await callback.answer()
        await show_list(callback.message, user, state)

    @router.message(F.text == "Циклические дедлайны")
    async def text_menu(message: Message, user: User, state: FSMContext) -> None:
        await show_list(message, user, state)

    @router.callback_query(F.data.startswith("rc:"))
    async def manage(callback: CallbackQuery, user: User, state: FSMContext) -> None:
        assert isinstance(callback.message, Message) and callback.data
        parts = callback.data.split(":")
        action = parts[1]
        await callback.answer()
        if action == "new":
            await begin(callback.message, user, state)
        elif action == "list":
            await show_list(callback.message, user, state, int(parts[2]))
        elif action == "open":
            await state.clear()
            await show_recurring_card(callback.message, user, runtime, parse_id(parts[2]))
        elif action == "edit":
            await begin(callback.message, user, state, parse_id(parts[2]), int(parts[3]))
        elif action == "closed":
            async with runtime.database.transaction() as session:
                from deadline_assistant.repositories.tenant import TenantRepository

                event = await TenantRepository(session, user.id).event(parse_id(parts[2]))
                if not event.recurring_deadline_id:
                    raise EntityNotFound
            await state.clear()
            await show_recurring_card(callback.message, user, runtime, event.recurring_deadline_id)
        elif action == "close":
            async with runtime.database.transaction() as session:
                event = await RecurringService(session, user.id).close(
                    parse_id(parts[2]), int(parts[3])
                )
            assert event.recurring_deadline_id is not None
            await state.clear()
            await callback.message.edit_reply_markup(reply_markup=None)
            from deadline_assistant.handlers.bot import send_digest

            await send_digest(callback.message, user, runtime)
            await show_recurring_card(callback.message, user, runtime, event.recurring_deadline_id)
        elif action == "stopcancel":
            data = await state.get_data()
            if (
                await state.get_state() != RecurringStates.stopping.state
                or parts[2] != data.get("stop_series_id")
                or parts[3] != data.get("stop_token")
            ):
                raise StaleState("Подтверждение уже закрыто. Откройте карточку заново.")
            await state.clear()
            await callback.message.edit_reply_markup(reply_markup=None)
            await show_recurring_card(callback.message, user, runtime, parse_id(parts[2]))
        elif action in {"stop", "stopconfirm"}:
            series_id, version = parse_id(parts[2]), int(parts[3])
            if action == "stopconfirm":
                data = await state.get_data()
                if (
                    await state.get_state() != RecurringStates.stopping.state
                    or parts[2] != data.get("stop_series_id")
                    or version != data.get("stop_version")
                    or parts[4] != data.get("stop_token")
                ):
                    raise StaleState("Подтверждение уже закрыто. Откройте карточку заново.")
            async with runtime.database.transaction() as session:
                service = RecurringService(session, user.id)
                series = await service.get(series_id)
                if not series.enabled or series.version != version:
                    raise StaleState("Расписание изменилось. Откройте карточку заново.")
                if action == "stopconfirm":
                    await service.stop(series_id, version)
            await state.clear()
            if action == "stop":
                token = secrets.token_hex(4)
                await state.set_state(RecurringStates.stopping)
                await state.set_data(
                    {
                        "stop_series_id": short_id(series_id),
                        "stop_version": version,
                        "stop_token": token,
                    }
                )
                await callback.message.answer(
                    "Закончить отслеживание «" + series.title + "»?\n"
                    "Все следующие повторения исчезнут из сводки.",
                    reply_markup=keyboard(
                        [
                            [
                                (
                                    "Закончить отслеживание",
                                    f"rc:stopconfirm:{short_id(series_id)}:{version}:{token}",
                                )
                            ],
                            [
                                ("Назад", f"rc:stopcancel:{short_id(series_id)}:{token}"),
                                ("Отмена", f"rc:stopcancel:{short_id(series_id)}:{token}"),
                            ],
                        ]
                    ),
                )
            else:
                await callback.message.edit_reply_markup(reply_markup=None)
                await callback.message.answer("Отслеживание закончено. Новых повторений не будет.")
                await show_list(callback.message, user, state)
        else:
            raise EntityNotFound

    @router.callback_query(F.data.startswith("rcw:"))
    async def wizard(callback: CallbackQuery, user: User, state: FSMContext) -> None:
        assert isinstance(callback.message, Message) and callback.data
        parts = callback.data.split(":")
        data = await state.get_data()
        current = await state.get_state()
        if parts[1] != data.get("panel_token"):
            raise StaleState(
                "Эта панель устарела. Используйте последний шаг или откройте меню заново."
            )
        action = parts[2]
        required = {
            "keep": "title",
            "interval": "interval",
            "day": "day",
            "date": "day",
            "time": "time",
            "save": "confirm",
        }
        if action != "back" and (
            action not in required or current != getattr(RecurringStates, required[action]).state
        ):
            raise StaleState("Шаг уже изменился. Используйте последнюю панель.")
        await callback.answer()
        if action == "back":
            if parts[3] == "list":
                await show_list(callback.message, user, state)
            elif parts[3] in {"title", "interval", "day", "time"}:
                await step(callback.message, user, state, parts[3])
            else:
                raise EntityNotFound
        elif action == "keep":
            await step(callback.message, user, state, "interval")
        elif action == "interval":
            interval = int(parts[3])
            if not 1 <= interval <= 14:
                raise ValueError("Интервал должен быть от 1 до 14 дней")
            await state.update_data(interval_days=interval)
            await step(callback.message, user, state, "day")
        elif action == "date":
            await step(callback.message, user, state, "date")
        elif action == "day":
            weekday = int(parts[3])
            if not 0 <= weekday <= 6:
                raise ValueError("Выберите день недели")
            data.pop("day", None)
            data["weekday"] = weekday
            await state.set_data(data)
            await step(callback.message, user, state, "time")
        elif action == "time":
            value = parts[3]
            await choose_time(
                callback.message,
                user,
                state,
                None if value == "none" else value[:2] + ":" + value[2:],
            )
        elif action == "save":
            first = datetime.fromisoformat(data["first_due_at"])
            if first < utcnow():
                await callback.message.answer(
                    "Первый срок уже прошёл. Выберите ближайший срок заново."
                )
                await step(callback.message, user, state, "day")
                return
            values: dict[str, Any] = {
                "title": data["title"],
                "interval_days": data["interval_days"],
                "first_due_at": first,
                "timezone": data["timezone"],
                "has_time": data["has_time"],
            }
            async with runtime.database.transaction() as session:
                service = RecurringService(session, user.id)
                if data.get("editing_version"):
                    series = await service.update(
                        UUID(data["series_id"]), data["editing_version"], **values
                    )
                else:
                    series = await service.create(**values, series_id=UUID(data["series_id"]))
            await state.clear()
            await callback.message.edit_reply_markup(reply_markup=None)
            await show_recurring_card(callback.message, user, runtime, series.id)

    @router.message(RecurringStates.title, F.text, ~F.text.startswith("/"), F.text != "Отмена")
    async def title_value(message: Message, user: User, state: FSMContext) -> None:
        title = " ".join((message.text or "").split())
        if not 1 <= len(title) <= 300:
            await message.answer("Название должно содержать от 1 до 300 символов.")
            await step(message, user, state, "title")
            return
        await state.update_data(title=title)
        await step(message, user, state, "interval")

    @router.message(RecurringStates.interval, F.text, ~F.text.startswith("/"), F.text != "Отмена")
    async def interval_value(message: Message, user: User, state: FSMContext) -> None:
        text = (message.text or "").strip()
        if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 14:
            await message.answer("Выберите интервал от 1 до 14 дней.")
            await step(message, user, state, "interval")
            return
        await state.update_data(interval_days=int(text))
        await step(message, user, state, "day")

    @router.message(RecurringStates.day, F.text, ~F.text.startswith("/"), F.text != "Отмена")
    @router.message(RecurringStates.date, F.text, ~F.text.startswith("/"), F.text != "Отмена")
    async def day_value(message: Message, user: User, state: FSMContext) -> None:
        text = (message.text or "").strip().casefold()
        data = await state.get_data()
        if text in [day.casefold() for day in WEEKDAYS]:
            data.pop("day", None)
            data["weekday"] = [day.casefold() for day in WEEKDAYS].index(text)
        else:
            try:
                day = datetime.strptime(text, "%d.%m.%Y").date()
                # Time/zone is selected on the next step, so validate only the date format here.
            except ValueError:
                await message.answer("Введите дату ДД.ММ.ГГГГ или выберите день недели.")
                await step(message, user, state, "day")
                return
            data.pop("weekday", None)
            data["day"] = day.isoformat()
        await state.set_data(data)
        await step(message, user, state, "time")

    @router.message(RecurringStates.time, F.text, ~F.text.startswith("/"), F.text != "Отмена")
    async def time_value(message: Message, user: User, state: FSMContext) -> None:
        text = (message.text or "").strip()
        await choose_time(message, user, state, None if text.casefold() == "без времени" else text)

    return router
