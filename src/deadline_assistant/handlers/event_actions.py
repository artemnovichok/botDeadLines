"""Versioned event selection. Callback values contain the event UUID, never a display number."""

import base64
from typing import TYPE_CHECKING
from uuid import UUID

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from deadline_assistant.db.models import Event, User
from deadline_assistant.domain import EntityNotFound, StaleState
from deadline_assistant.handlers.menus import CANCEL, MENU, PAGE_SIZE, MenuStates, keyboard
from deadline_assistant.repositories.tenant import TenantRepository, event_data
from deadline_assistant.services.digest import build_digest, render_date, render_events
from deadline_assistant.services.sources import show_sources
from deadline_assistant.services.telegram_text import truncate

if TYPE_CHECKING:
    from deadline_assistant.runtime import Runtime


def short_id(value: UUID) -> str:
    return base64.urlsafe_b64encode(value.bytes).decode().rstrip("=")


def parse_id(value: str) -> UUID:
    return UUID(bytes=base64.urlsafe_b64decode(value + "=="))


def actions(list_id: UUID) -> list[list[tuple[str, str]]]:
    token = short_id(list_id)
    return [
        [
            ("Редактировать дедлайн", f"el:edit:{token}:0"),
            ("Завершить задание", f"el:done:{token}:0"),
        ],
        [("Циклические дедлайны", "menu:recurring")],
        [("Ещё", f"digest:more:{token}")],
    ]


def create_event_router(runtime: "Runtime") -> Router:
    router = Router(name="event_actions")

    async def show_list(
        message: Message,
        user: User,
        state: FSMContext,
        action: str,
        list_id: UUID | None = None,
        page: int = 0,
    ) -> None:
        async with runtime.database.transaction() as session:
            repo = TenantRepository(session, user.id)
            if list_id is None:
                if action == "undo":
                    listing = await repo.save_event_list(await repo.events_with_corrections())
                    list_id = listing.id
                else:
                    _, summary = await build_digest(repo, user.timezone)
                    list_id = summary.list_id
                assert list_id is not None
            listing = await repo.event_list(list_id)
            # Keep numbering immutable, including on page changes.
            entries = listing.entries
            events: list[Event | None] = []
            for entry in entries:
                try:
                    events.append(await repo.event(UUID(entry["id"])))
                except EntityNotFound:
                    events.append(None)
        if not entries:
            await state.clear()
            await message.answer(
                "Нет исправлений для отмены."
                if action == "undo"
                else "Активных дедлайнов пока нет.",
                reply_markup=MENU,
            )
            return
        page = max(0, min(page, (len(entries) - 1) // PAGE_SIZE))
        token = short_id(list_id)
        rows = []
        for index in range(page * PAGE_SIZE, min(len(entries), (page + 1) * PAGE_SIZE)):
            event = events[index]
            label = f"{index + 1}. " + (event.title[:32] if event else "Событие удалено")
            if event:
                label += " — " + render_date(event_data(event), user.timezone)
            rows.append(
                [(label[:64], f"ep:{action[0]}:{token}:{short_id(UUID(entries[index]['id']))}")]
            )
        nav = []
        if page:
            nav.append(("←", f"el:{action}:{token}:{page - 1}"))
        if (page + 1) * PAGE_SIZE < len(entries):
            nav.append(("→", f"el:{action}:{token}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([("Отмена", "menu:cancel")])
        labels = {
            "edit": "редактирования",
            "done": "завершения",
            "undo": "отмены исправления",
            "sources": "просмотра источников",
        }
        await state.set_state(MenuStates.choosing_event)
        panel = await message.answer(
            f"Выберите дедлайн для {labels[action]}. Список {token[:6]}.\n"
            "Нажмите кнопку или ответьте на это сообщение номером задания.",
            reply_markup=keyboard(rows),
        )
        await state.set_data(
            {"list_id": str(list_id), "action": action, "number_prompt": panel.message_id}
        )

    @router.message(
        F.text.in_(
            {
                "Редактировать дедлайн",
                "Завершить задание",
                "Отменить своё исправление",
                "Посмотреть источники",
            }
        )
    )
    async def begin(message: Message, user: User, state: FSMContext) -> None:
        action = {
            "Редактировать дедлайн": "edit",
            "Завершить задание": "done",
            "Отменить своё исправление": "undo",
            "Посмотреть источники": "sources",
        }[message.text or ""]
        await state.clear()
        await show_list(message, user, state, action)

    @router.callback_query(F.data.startswith("el:"))
    async def list_callback(callback: CallbackQuery, user: User, state: FSMContext) -> None:
        assert isinstance(callback.message, Message) and callback.data
        _, action, token, page = callback.data.split(":")
        if action not in {"edit", "done", "undo", "sources"}:
            raise EntityNotFound
        await callback.answer()
        await show_list(callback.message, user, state, action, parse_id(token), int(page))

    @router.callback_query(F.data.startswith("digest:more:"))
    async def more_actions(callback: CallbackQuery, user: User, state: FSMContext) -> None:
        assert isinstance(callback.message, Message) and callback.data
        token = callback.data.split(":")[2]
        async with runtime.database.transaction() as session:
            await TenantRepository(session, user.id).event_list(parse_id(token))
        await callback.answer()
        await state.clear()
        await callback.message.answer(
            "Действия с дедлайнами",
            reply_markup=keyboard(
                [
                    [("Посмотреть источники", f"el:sources:{token}:0")],
                    [("Отменить своё исправление", "undo:list")],
                    [("Уточнения", "digest:conflicts")],
                    [("Обновить сводку", "digest:refresh")],
                ]
            ),
        )

    @router.callback_query(F.data == "undo:list")
    async def undo_list(callback: CallbackQuery, user: User, state: FSMContext) -> None:
        assert isinstance(callback.message, Message)
        await callback.answer()
        await state.clear()
        await show_list(callback.message, user, state, "undo")

    @router.message(Command("undo"))
    async def undo_command(message: Message, user: User, state: FSMContext) -> None:
        from deadline_assistant.handlers.bot import command_uuid, send_digest

        async with runtime.database.transaction() as session:
            await TenantRepository(session, user.id).undo_correction(command_uuid(message))
        await state.clear()
        await message.answer("Исправление отменено.")
        await send_digest(message, user, runtime)

    async def select(message: Message, user: User, state: FSMContext, event_id: UUID) -> None:
        data = await state.get_data()
        async with runtime.database.transaction() as session:
            repo = TenantRepository(session, user.id)
            event = await repo.listed_event(UUID(data["list_id"]), event_id, require_active=False)
            recurring_id = event.recurring_deadline_id
            if recurring_id is None and data["action"] != "undo" and event.status != "active":
                raise StaleState("Событие изменилось. Откройте новый список.")
            if recurring_id is None and data["action"] == "done":
                await repo.complete(event.id)
            elif recurring_id is None and data["action"] == "undo":
                await repo.undo_correction(event.id)
        if recurring_id is not None:
            from deadline_assistant.handlers.recurring import show_recurring_card

            await state.clear()
            await show_recurring_card(message, user, runtime, recurring_id)
            return
        if data["action"] == "sources":
            await state.clear()
            await show_sources(message, user, runtime, event_id)
        elif data["action"] in {"done", "undo"}:
            from deadline_assistant.handlers.bot import send_digest

            await state.clear()
            await message.answer(
                "Задание завершено." if data["action"] == "done" else "Исправление отменено."
            )
            await send_digest(message, user, runtime)
        else:
            await state.set_state(MenuStates.choosing_field)
            await state.update_data(event_id=str(event.id), event_version=event.version)
            token = short_id(UUID(data["list_id"]))
            target = short_id(event.id)
            await message.answer(
                truncate(render_events([event], user.timezone), 2500)
                + "\nНапишите исправление обычным текстом или выберите поле.",
                reply_markup=keyboard(
                    [
                        [
                            ("Название", f"ef:{token}:{target}:title"),
                            ("Срок", f"ef:{token}:{target}:due_at"),
                        ],
                        [
                            ("Описание", f"ef:{token}:{target}:description"),
                            ("Место", f"ef:{token}:{target}:location"),
                        ],
                        [("Отмена", "menu:cancel")],
                    ]
                ),
            )

    @router.callback_query(MenuStates.choosing_event, F.data.startswith("ep:"))
    async def pick(callback: CallbackQuery, user: User, state: FSMContext) -> None:
        assert isinstance(callback.message, Message) and callback.data
        _, mode, token, event_token = callback.data.split(":")
        data = await state.get_data()
        if str(parse_id(token)) != data.get("list_id") or mode != data.get("action", "")[:1]:
            raise StaleState("Список устарел. Откройте список заново.")
        await callback.answer()
        await select(callback.message, user, state, parse_id(event_token))

    @router.message(MenuStates.choosing_event, F.text, ~F.text.startswith("/"))
    async def number(message: Message, user: User, state: FSMContext) -> None:
        data = await state.get_data()
        if not message.reply_to_message or message.reply_to_message.message_id != data.get(
            "number_prompt"
        ):
            await message.answer(
                "Ответьте номером именно на сообщение со списком или нажмите кнопку задания.",
                reply_markup=CANCEL,
            )
            return
        text = (message.text or "").strip()
        async with runtime.database.transaction() as session:
            listing = await TenantRepository(session, user.id).event_list(UUID(data["list_id"]))
            if not text.isdigit() or not 1 <= int(text) <= len(listing.entries):
                await message.answer("Такого номера в показанном списке нет.", reply_markup=CANCEL)
                return
            event_id = UUID(listing.entries[int(text) - 1]["id"])
        await select(message, user, state, event_id)

    @router.callback_query(MenuStates.choosing_field, F.data.startswith("ef:"))
    async def field(callback: CallbackQuery, state: FSMContext) -> None:
        assert isinstance(callback.message, Message) and callback.data
        _, token, target, name = callback.data.split(":")
        data = await state.get_data()
        if str(parse_id(token)) != data.get("list_id") or str(parse_id(target)) != data.get(
            "event_id"
        ):
            raise StaleState("Меню устарело")
        if name not in {"title", "due_at", "description", "location"}:
            raise EntityNotFound
        await state.update_data(field=name)
        await state.set_state(MenuStates.editing_value)
        await callback.answer()
        await callback.message.answer(
            "Введите срок ДД.ММ.ГГГГ ЧЧ:ММ или ДД.ММ.ГГГГ. «-» убирает срок."
            if name == "due_at"
            else "Введите новое значение. «-» очищает поле.",
            reply_markup=CANCEL,
        )

    @router.message(Command("status"))
    async def status(message: Message, user: User) -> None:
        from deadline_assistant.services.jobs import render_run

        run = await runtime.jobs.latest(user.id)
        await message.answer(render_run(run, user.timezone) if run else "Анализ ещё не запускался.")

    @router.callback_query(F.data.startswith("src:"))
    async def source_callback(callback: CallbackQuery, user: User) -> None:
        assert isinstance(callback.message, Message) and callback.data
        _, event, source = callback.data.split(":")
        await callback.answer()
        await show_sources(callback.message, user, runtime, parse_id(event), parse_id(source))

    @router.callback_query(F.data.startswith("evsrc:"))
    async def all_sources(callback: CallbackQuery, user: User) -> None:
        assert isinstance(callback.message, Message) and callback.data
        await callback.answer()
        await show_sources(callback.message, user, runtime, parse_id(callback.data.split(":")[1]))

    @router.message(F.text == "Обновить сводку")
    @router.message(Command("refresh"))
    async def refresh(message: Message, user: User, state: FSMContext) -> None:
        from deadline_assistant.handlers.bot import send_digest

        await state.clear()
        await send_digest(message, user, runtime)

    @router.callback_query(F.data == "digest:refresh")
    async def refresh_callback(callback: CallbackQuery, user: User, state: FSMContext) -> None:
        assert isinstance(callback.message, Message)
        await callback.answer()
        await refresh(callback.message, user, state)

    return router
