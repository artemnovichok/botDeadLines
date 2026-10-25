import secrets
from datetime import datetime
from html import escape
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)

from deadline_assistant.db.models import User
from deadline_assistant.domain import (
    EntityNotFound,
    EventData,
    UserPreferences,
    normalize_study_group,
    utcnow,
)
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.repositories.users import UserRepository
from deadline_assistant.services.jobs import render_run

if TYPE_CHECKING:
    from deadline_assistant.runtime import Runtime

PAGE_SIZE = 8


class MenuStates(StatesGroup):
    choosing_chats = State()
    searching_chats = State()
    timezone = State()
    digest_time = State()
    study_group = State()
    choosing_event = State()
    choosing_field = State()
    editing_value = State()


def keyboard(rows: list[list[tuple[str, str]]], *, home: bool = True) -> InlineKeyboardMarkup:
    if home:
        rows = [*rows, [("Главное меню", "menu:home")]]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=value) for label, value in row]
            for row in rows
        ]
    )


MENU = keyboard(
    [
        [("Мои дедлайны", "menu:deadlines"), ("Мои чаты", "menu:chats")],
        [("Циклические дедлайны", "menu:recurring")],
        [("Настройки", "menu:settings")],
    ],
    home=False,
)
CONNECT_MENU = keyboard([[("Подключить аккаунт", "account:connect")]], home=False)
CANCEL = keyboard([[("Отмена", "menu:cancel")]], home=False)
FIRST_ANALYSIS = keyboard([[("Да", "analysis:first"), ("Нет", "analysis:skip")]], home=False)


async def account_menu(user: User, runtime: "Runtime") -> InlineKeyboardMarkup | None:
    async with runtime.database.transaction() as session:
        credential = await TenantRepository(session, user.id).telegram_session()
    if not credential or credential.status != "connected":
        return CONNECT_MENU
    return MENU if user.study_group else None


async def ask_study_group(
    message: Message, user: User, runtime: "Runtime", state: FSMContext | None = None
) -> None:
    async with runtime.database.transaction() as session:
        stored = await UserRepository(session).require(user.id)
        stored.study_group_prompted_at = stored.study_group_prompted_at or utcnow()
    if state is not None:
        await state.set_state(MenuStates.study_group)
    await message.answer(
        "Укажите вашу учебную группу, например БПИ267. "
        "Я сохраню её и буду выбирать подходящие вам даты из распределений по группам.\n"
        "Это обязательный шаг: после него откроются чаты и настройки расписания. "
        "Изменить группу позже можно в настройках.",
        reply_markup=CANCEL,
    )


async def offer_first_analysis(message: Message, user: User, runtime: "Runtime") -> None:
    if user.initial_analysis_choice is not None:
        return
    await message.answer(
        "Проанализировать старые сообщения выбранных чатов?\n"
        f"«Да» — один раз разобрать до {runtime.settings.initial_message_limit} последних "
        "доступных сообщений каждого выбранного чата и собрать дедлайны. "
        f"Учитываются сообщения за последние {runtime.settings.message_max_age_days} дней.\n"
        "«Нет» — начать отслеживание с текущего момента без старой переписки.\n"
        "Выбор доступен один раз. Затем анализ выполняется только ночью, "
        "а сводка приходит утром.",
        reply_markup=FIRST_ANALYSIS,
    )


async def connection_link(message: Message, user: User, runtime: "Runtime") -> None:
    if not runtime.settings.telegram_enabled:
        await message.answer("Администратор ещё не настроил Telegram API.")
        return
    token = await runtime.auth.issue(user.id)
    await runtime.login.cleanup()
    url = runtime.settings.public_base_url.rstrip("/") + "/connect#token=" + token
    hostname = urlsplit(url).hostname or ""
    local = hostname == "localhost"
    try:
        local = local or ip_address(hostname).is_loopback
    except ValueError:
        pass
    if local:
        # Telegram rejects localhost in inline URL buttons. Code text is copyable
        # and keeps the one-time token out of link previews.
        await message.answer(
            "Подключите тот же Telegram-аккаунт, с которого вы открыли бота.\n\n"
            "Скопируйте ссылку целиком и откройте в браузере на компьютере, где запущен бот:\n"
            f"<code>{escape(url)}</code>\n\n"
            "Ссылка одноразовая и действует 10 минут. Не пересылайте её. "
            "Коды и пароль вводятся только на защищённой странице.",
            parse_mode="HTML",
        )
        return
    await message.answer(
        "Подключите тот же Telegram-аккаунт, с которого вы открыли бота. "
        "Ссылка одноразовая и действует 10 минут. Не пересылайте её. "
        "Коды и пароль вводятся только на защищённой странице.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Подключить аккаунт", url=url),
                ]
            ]
        ),
    )


def picker(data: dict[str, Any], page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    query = data.get("query", "").casefold()
    catalog = [d for d in data["catalog"] if query in d["title"].casefold()]
    selected = set(data["selected"])
    pages = max(1, (len(catalog) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    prefix = f"ch:{data['picker']}:"
    rows = [
        [
            (
                ("✅ " if d["id"] in selected else "▫️ ")
                + d["title"][:46]
                + ("" if d["available"] else " · нет доступа"),
                prefix + "toggle:" + str(d["id"]),
            )
        ]
        for d in catalog[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    ]
    navigation = []
    if page > 0:
        navigation.append(("←", prefix + f"page:{page - 1}"))
    if page < pages - 1:
        navigation.append(("→", prefix + f"page:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.extend(
        [
            [("Поиск", prefix + "search"), ("Все чаты", prefix + "all")],
            [("Обновить список", prefix + "refresh"), ("Подключения", "connections:0")],
            [("Сохранить", prefix + "save"), ("Отмена", "menu:cancel")],
        ]
    )
    text = (
        f"Мои чаты · страница {page + 1}/{pages}\nВыбрано: {len(selected)}. "
        "Нажмите на чат, чтобы изменить выбор. Затем нажмите «Сохранить».\n"
        "До сохранения отслеживание не меняется. Сообщения выбранных чатов передаются ИИ."
    )
    if query:
        text += f"\nПоиск: {data['query'][:100]}"
    if not catalog:
        text += "\nЧаты не найдены."
    return text, keyboard(rows)


def create_menu_router(runtime: "Runtime") -> Router:
    router = Router(name="menus")

    async def load_picker(
        user: User, state: FSMContext, *, preserve: bool = False
    ) -> dict[str, Any]:
        old = await state.get_data() if preserve else {}
        dialogs = await runtime.accounts.dialogs(user.id)
        tracked = await runtime.accounts.connections(user.id)
        catalog = [{"id": d["id"], "title": d["title"], "available": True} for d in dialogs]
        available = {d["id"] for d in catalog}
        missing = {c.telegram_chat_id: c.title for c in tracked if c.enabled}
        for item in old.get("catalog", []):
            if item["id"] in old.get("selected", []):
                missing[item["id"]] = item["title"]
        catalog.extend(
            {"id": chat_id, "title": title, "available": False}
            for chat_id, title in missing.items()
            if chat_id not in available
        )
        catalog.sort(key=lambda d: str(d["title"]).casefold())
        data = {
            "catalog": catalog,
            "selected": old.get("selected", [c.telegram_chat_id for c in tracked if c.enabled]),
            "picker": old.get("picker", secrets.token_hex(4)),
            "query": old.get("query", ""),
            "page": 0,
        }
        await state.set_state(MenuStates.choosing_chats)
        await state.set_data(data)
        return data

    async def show_settings(message: Message, user: User) -> None:
        async with runtime.database.transaction() as session:
            credential = await TenantRepository(session, user.id).telegram_session()
        run = await runtime.jobs.latest(user.id)
        connection = "не подключён"
        if credential:
            connection = "подключён" if credential.status == "connected" else "нужно переподключить"
        await message.answer(
            f"Telegram: {connection}\nАккаунт: {user.telegram_user_id}\n"
            f"Учебная группа: {user.study_group or 'не указана'}\n"
            f"Часовой пояс: {user.timezone}\n"
            f"Утренняя сводка: {user.preferred_digest_time:%H:%M}\n\n"
            + (render_run(run, user.timezone) if run else "Анализ ещё не запускался."),
            reply_markup=keyboard(
                [
                    [("Учебная группа", "settings:group")],
                    [("Часовой пояс", "settings:timezone"), ("Время сводки", "settings:time")],
                    [("Аккаунт", "account:settings")],
                ]
            ),
        )

    async def show_connections(message: Message, user: User, page: int) -> None:
        items = await runtime.accounts.connections(user.id)
        pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        visible = items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        text = f"Текущие подключения · {page + 1}/{pages}\n" + "\n".join(
            f"{'✅' if c.enabled else '⏸'} {c.title} · {c.telegram_chat_id}" for c in visible
        )
        rows = [
            [("Отключить: " + c.title[:35], "untrack:" + c.id.hex)] for c in visible if c.enabled
        ]
        nav = []
        if page:
            nav.append(("←", f"connections:{page - 1}"))
        if page + 1 < pages:
            nav.append(("→", f"connections:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([("Проверить доступ", "settings:check")])
        await message.answer(
            text if items else "Подключённых чатов пока нет.", reply_markup=keyboard(rows)
        )

    async def ask_group(message: Message, user: User, state: FSMContext) -> None:
        await ask_study_group(message, user, runtime, state)

    async def continue_onboarding(message: Message, user: User, state: FSMContext) -> None:
        menu = await account_menu(user, runtime)
        if menu is CONNECT_MENU:
            await connection_link(message, user, runtime)
        elif not user.study_group:
            await ask_group(message, user, state)
        else:
            await message.answer(
                "Настройка аккаунта завершена. Откройте «Мои чаты» и выберите источники. "
                "В «Настройках» можно задать время утренней сводки.",
                reply_markup=MENU,
            )
            if user.initial_analysis_choice is None and any(
                chat.enabled for chat in await runtime.accounts.connections(user.id)
            ):
                await offer_first_analysis(message, user, runtime)

    @router.message(Command("start"))
    async def start(message: Message, user: User, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "Deadline Assistant находит дедлайны в выбранных вами чатах. "
            "Сначала подключите Telegram и укажите учебную группу, затем выберите чаты. "
            "Никогда не присылайте боту коды входа и пароль 2FA.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await continue_onboarding(message, user, state)

    @router.callback_query(
        F.data.in_({"menu:home", "menu:deadlines", "menu:chats", "menu:settings"})
    )
    async def navigate(callback: CallbackQuery, user: User, state: FSMContext) -> None:
        assert isinstance(callback.message, Message)
        await callback.answer()
        await state.clear()
        if callback.data == "menu:deadlines":
            await deadlines(callback.message, user, state)
        elif callback.data == "menu:chats":
            await chats(callback.message, user, state)
        elif callback.data == "menu:settings":
            await show_settings(callback.message, user)
        else:
            await continue_onboarding(callback.message, user, state)

    @router.message(Command("group"))
    async def group_command(message: Message, user: User, state: FSMContext) -> None:
        await state.clear()
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 1:
            await ask_group(message, user, state)
            return
        onboarding = user.study_group is None
        async with runtime.database.transaction() as session:
            saved = await UserRepository(session).set_study_group(user.id, parts[1])
        await message.answer(
            f"Учебная группа сохранена: {saved.study_group}. "
            "Следующий анализ будет учитывать вашу группу.",
            reply_markup=await account_menu(saved, runtime),
        )
        if onboarding:
            await continue_onboarding(message, saved, state)

    @router.message(Command("connect"))
    @router.message(F.text == "Подключить аккаунт")
    async def connect(message: Message, user: User, state: FSMContext) -> None:
        await state.clear()
        await connection_link(message, user, runtime)

    @router.message(Command("cancel"))
    @router.message(F.text == "Отмена")
    async def cancel_menu(message: Message, user: User, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "Изменения в меню отменены.", reply_markup=await account_menu(user, runtime)
        )
        if not user.study_group:
            await continue_onboarding(message, user, state)

    @router.message(F.text == "Мои дедлайны")
    async def deadlines(message: Message, user: User, state: FSMContext) -> None:
        await state.clear()
        from deadline_assistant.handlers.bot import send_digest

        await send_digest(message, user, runtime)

    @router.message(Command("chats"))
    @router.message(F.text == "Мои чаты")
    async def chats(message: Message, user: User, state: FSMContext) -> None:
        await state.clear()
        data = await load_picker(user, state)
        text, markup = picker(data)
        await message.answer(text, reply_markup=markup)

    @router.message(Command("settings"))
    @router.message(F.text == "Настройки")
    async def settings(message: Message, user: User, state: FSMContext) -> None:
        await state.clear()
        parts = (message.text or "").split()
        if parts[0].startswith("/settings") and len(parts) > 1:
            if len(parts) != 3:
                raise ValueError("Используйте /settings Europe/Moscow 08:00")
            preferences = UserPreferences(timezone=parts[1], preferred_digest_time=parts[2])
            async with runtime.database.transaction() as session:
                user = await UserRepository(session).preferences(user.id, preferences)
        await show_settings(message, user)

    @router.message(Command("stop_sync"))
    async def stop_sync(message: Message, user: User) -> None:
        stopped = await runtime.jobs.stop(user.id)
        await message.answer(
            "Анализ остановлен. Сохранённые события остались; продолжение — ночью. "
            "Незавершённый запрос мог быть оплачен провайдером."
            if stopped
            else "Активного фонового анализа нет."
        )

    @router.message(Command("tracked"))
    async def connections(message: Message, user: User) -> None:
        await show_connections(message, user, 0)

    @router.message(Command("disconnect"))
    async def disconnect_command(message: Message, user: User, state: FSMContext) -> None:
        await do_disconnect(message, user, state)

    async def do_disconnect(message: Message, user: User, state: FSMContext) -> None:
        await state.clear()
        revoked = await runtime.accounts.disconnect(user.id)
        await runtime.login.cleanup()
        await message.answer(
            "Аккаунт отключён, отслеживание чатов остановлено. Ваши дедлайны сохранены."
            + (
                ""
                if revoked
                else " Отзыв в Telegram не подтверждён. Завершите сессию в Telegram → Устройства."
            ),
            reply_markup=CONNECT_MENU,
        )

    @router.callback_query(F.data.startswith("auth:"))
    async def auth_confirm(callback: CallbackQuery, user: User) -> None:
        assert isinstance(callback.message, Message) and callback.data
        _, action, raw_id = callback.data.split(":")
        link_id = UUID(raw_id)
        if action == "approve":
            await runtime.auth.approve(user.id, link_id)
            text = "Открытие подтверждено. Вернитесь на страницу подключения."
        elif action == "cancel":
            await runtime.auth.cancel(user.id, link_id)
            await runtime.login.cleanup()
            text = "Авторизация отменена. Ссылка больше не действует."
        else:
            raise EntityNotFound
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(text)

    @router.callback_query(F.data == "menu:cancel")
    async def cancel_callback(callback: CallbackQuery, user: User, state: FSMContext) -> None:
        assert isinstance(callback.message, Message)
        await state.clear()
        await callback.answer("Изменения отменены")
        await callback.message.edit_reply_markup(reply_markup=None)
        await continue_onboarding(callback.message, user, state)

    @router.callback_query(F.data.startswith("ch:"))
    async def choose_chat(callback: CallbackQuery, user: User, state: FSMContext) -> None:
        assert isinstance(callback.message, Message) and callback.data
        data = await state.get_data()
        parts = callback.data.split(":")
        if (
            len(parts) < 3
            or parts[1] != data.get("picker")
            or await state.get_state()
            not in {MenuStates.choosing_chats.state, MenuStates.searching_chats.state}
        ):
            await callback.answer("Это меню устарело. Откройте «Мои чаты» заново.", show_alert=True)
            return
        action = parts[2]
        page = int(data.get("page", 0))
        await callback.answer()
        if action == "toggle":
            chat_id = int(parts[3])
            item = next((d for d in data["catalog"] if d["id"] == chat_id), None)
            if item is None:
                raise EntityNotFound
            selected = set(data["selected"])
            if chat_id in selected:
                selected.remove(chat_id)
            elif item["available"]:
                selected.add(chat_id)
            data["selected"] = list(selected)
        elif action == "page":
            page = max(0, min(int(parts[3]), len(data["catalog"]) // PAGE_SIZE))
        elif action == "search":
            await state.set_state(MenuStates.searching_chats)
            await callback.message.answer("Введите часть названия чата.", reply_markup=CANCEL)
            return
        elif action == "all":
            data["query"], page = "", 0
        elif action == "refresh":
            data = await load_picker(user, state, preserve=True)
            page = 0
        elif action == "save":
            count = await runtime.accounts.select(user.id, set(data["selected"]))
            await state.clear()
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(
                f"Выбор сохранён. Чатов подключено: {count}.", reply_markup=MENU
            )
            if count and user.initial_analysis_choice is None:
                await offer_first_analysis(callback.message, user, runtime)
            elif count:
                await callback.message.answer(
                    "Следующий анализ — ночью, сводка — утром. "
                    "Для добавленных чатов отслеживаются сообщения с момента подключения.",
                    reply_markup=MENU,
                )
            return
        else:
            raise EntityNotFound
        data["page"] = page
        await state.set_state(MenuStates.choosing_chats)
        await state.set_data(data)
        text, markup = picker(data, page)
        await callback.message.edit_text(text, reply_markup=markup)

    @router.callback_query(F.data == "analysis:first")
    async def first_analysis(callback: CallbackQuery, user: User) -> None:
        assert isinstance(callback.message, Message)
        queued = await runtime.jobs.enqueue(user.id, initial=True)
        await callback.answer(
            "Анализ запущен" if queued else "Первый анализ уже выбран или анализ выполняется"
        )
        await callback.message.edit_reply_markup(reply_markup=None)
        if queued:
            await callback.message.answer(
                "Первый анализ выполняется в фоне. Пришлю число сообщений и найденных событий.",
                reply_markup=MENU,
            )

    @router.callback_query(F.data == "analysis:skip")
    async def skip_first_analysis(callback: CallbackQuery, user: User) -> None:
        assert isinstance(callback.message, Message)
        async with runtime.database.transaction() as session:
            chosen = await UserRepository(session).decide_initial_analysis(user.id, analyze=False)
        await callback.answer("Выбор сохранён" if chosen else "Выбор первого анализа уже сделан")
        await callback.message.edit_reply_markup(reply_markup=None)
        if chosen:
            await callback.message.answer(
                "Старая переписка пропущена. Новые сообщения отслеживаются с текущего момента. "
                "Анализ — ночью, сводка — утром.",
                reply_markup=MENU,
            )

    @router.callback_query(F.data.startswith("connections:"))
    async def connections_callback(callback: CallbackQuery, user: User) -> None:
        assert isinstance(callback.message, Message) and callback.data
        await callback.answer()
        await show_connections(callback.message, user, int(callback.data.split(":")[1]))

    @router.callback_query(F.data.startswith("untrack:"))
    async def untrack(callback: CallbackQuery, user: User, state: FSMContext) -> None:
        assert isinstance(callback.message, Message) and callback.data
        await runtime.accounts.untrack(user.id, UUID(callback.data.split(":")[1]))
        await state.clear()
        await callback.answer("Чат отключён")
        await callback.message.edit_reply_markup(reply_markup=None)
        await show_connections(callback.message, user, 0)

    @router.callback_query(F.data.startswith("settings:"))
    async def change_settings(callback: CallbackQuery, user: User, state: FSMContext) -> None:
        assert isinstance(callback.message, Message) and callback.data
        action = callback.data.split(":")[1]
        await callback.answer()
        await state.clear()
        if action == "group":
            await ask_group(callback.message, user, state)
        elif action == "timezone":
            await state.set_state(MenuStates.timezone)
            await callback.message.answer(
                "Введите часовой пояс IANA, например Europe/Moscow, Europe/Berlin "
                "или Asia/Yekaterinburg.",
                reply_markup=CANCEL,
            )
        elif action == "time":
            await state.set_state(MenuStates.digest_time)
            await callback.message.answer(
                f"Введите время утренней сводки ЧЧ:ММ в поясе {user.timezone}.",
                reply_markup=CANCEL,
            )
        elif action == "check":
            dialogs = {d["id"]: d for d in await runtime.accounts.dialogs(user.id)}
            chats = [c for c in await runtime.accounts.connections(user.id) if c.enabled]
            missing = [c.title for c in chats if c.telegram_chat_id not in dialogs]
            await callback.message.answer(
                f"Аккаунт доступен. Выбранных чатов: {len(chats)}. "
                f"Доступны: {len(chats) - len(missing)}."
                + ("\nНет доступа: " + ", ".join(missing)[:2500] if missing else "")
            )
        else:
            raise EntityNotFound

    @router.callback_query(F.data.startswith("account:"))
    async def account(callback: CallbackQuery, user: User, state: FSMContext) -> None:
        assert isinstance(callback.message, Message) and callback.data
        await callback.answer()
        if callback.data == "account:settings":
            await state.clear()
            await callback.message.answer(
                "Управление аккаунтом Telegram",
                reply_markup=keyboard(
                    [
                        [("Проверить доступ", "settings:check")],
                        [("Переподключить аккаунт", "account:connect")],
                        [("Отключить аккаунт", "account:disconnect")],
                        [("Удалить мои данные", "privacy:ask")],
                    ]
                ),
            )
        elif callback.data == "account:connect":
            await state.clear()
            await connection_link(callback.message, user, runtime)
        elif callback.data == "account:disconnect":
            await callback.message.answer(
                "Отключить Telegram и остановить отслеживание? Существующие дедлайны сохранятся.",
                reply_markup=keyboard(
                    [[("Отключить", "account:confirm_disconnect"), ("Отмена", "menu:cancel")]]
                ),
            )
        elif callback.data == "account:confirm_disconnect":
            await do_disconnect(callback.message, user, state)
        else:
            raise EntityNotFound

    @router.message(MenuStates.choosing_field, F.text, ~F.text.startswith("/"))
    async def free_correction(message: Message, user: User, state: FSMContext) -> None:
        from deadline_assistant.handlers.bot import send_digest, submit_correction

        data = await state.get_data()
        status = await submit_correction(
            runtime,
            user.id,
            UUID(data["event_id"]),
            message.text or "",
            message.chat.id,
            message.message_id,
            expected_version=data["event_version"],
        )
        await state.clear()
        await message.answer(
            "Дедлайн обновлён."
            if status == "applied"
            else "Исправление сохранено и ожидает обработки или подтверждения.",
            reply_markup=MENU,
        )
        await send_digest(message, user, runtime)

    @router.message(MenuStates.searching_chats, F.text, ~F.text.startswith("/"))
    async def search(message: Message, state: FSMContext) -> None:
        query = (message.text or "").strip()
        if len(query) > 100:
            await message.answer("Введите до 100 символов.", reply_markup=CANCEL)
            return
        data = await state.update_data(query=query, page=0)
        await state.set_state(MenuStates.choosing_chats)
        text, markup = picker(data)
        await message.answer(text, reply_markup=markup)

    @router.message(MenuStates.study_group, F.text, ~F.text.startswith("/"))
    @router.message(F.text.regexp(r"^[A-Za-zА-Яа-яЁё]{2,12}[\s-]*[0-9]{3,6}\s*$"))
    async def group_value(message: Message, user: User, state: FSMContext) -> None:
        # The persisted prompt also accepts a reply after an FSM/process restart.
        if await state.get_state() != MenuStates.study_group.state and (
            await state.get_state() is not None
            or user.study_group is not None
            or user.study_group_prompted_at is None
        ):
            return
        try:
            group = normalize_study_group(message.text or "")
        except ValueError as exc:
            await message.answer(str(exc), reply_markup=CANCEL)
            return
        onboarding = user.study_group is None
        async with runtime.database.transaction() as session:
            user = await UserRepository(session).set_study_group(user.id, group)
        await state.clear()
        await message.answer(
            f"Учебная группа сохранена: {user.study_group}. "
            "Следующий анализ будет учитывать вашу группу.",
            reply_markup=await account_menu(user, runtime),
        )
        if onboarding:
            await continue_onboarding(message, user, state)

    @router.message(MenuStates.timezone, F.text, ~F.text.startswith("/"))
    @router.message(MenuStates.digest_time, F.text, ~F.text.startswith("/"))
    async def settings_value(message: Message, user: User, state: FSMContext) -> None:
        timezone_mode = await state.get_state() == MenuStates.timezone.state
        preferences = UserPreferences.model_validate(
            {
                "timezone": message.text if timezone_mode else user.timezone,
                "preferred_digest_time": user.preferred_digest_time
                if timezone_mode
                else message.text,
            }
        )
        async with runtime.database.transaction() as session:
            user = await UserRepository(session).preferences(user.id, preferences)
        await state.clear()
        await message.answer("Настройки сохранены.", reply_markup=await account_menu(user, runtime))
        await show_settings(message, user)

    @router.message(MenuStates.editing_value, F.text, ~F.text.startswith("/"))
    async def edit_value(message: Message, user: User, state: FSMContext) -> None:
        data = await state.get_data()
        field, value = data["field"], (message.text or "").strip()
        async with runtime.database.transaction() as session:
            repo = TenantRepository(session, user.id)
            event = await repo.listed_event(UUID(data["list_id"]), UUID(data["event_id"]))
            payload = {key: getattr(event, key) for key in EventData.model_fields}
            if field == "due_at":
                if value == "-":
                    payload["due_at"], payload["date_precision"] = None, "unknown"
                    payload["end_at"] = None
                else:
                    exact = len(value) > 10
                    parsed = datetime.strptime(value, "%d.%m.%Y %H:%M" if exact else "%d.%m.%Y")
                    payload["due_at"] = parsed.replace(tzinfo=ZoneInfo(user.timezone))
                    payload["date_precision"] = "exact" if exact else "day"
                    payload["timezone"] = user.timezone
                    payload["end_at"] = None
            else:
                payload[field] = (None if field == "location" else "") if value == "-" else value
            updated = EventData.model_validate(payload)
            correction = await repo.correction(event.id, f"Изменение поля {field}: {value}")
            await repo.apply_correction(correction.id, updated)
        await state.clear()
        await message.answer("Дедлайн обновлён.", reply_markup=MENU)
        from deadline_assistant.handlers.bot import send_digest

        await send_digest(message, user, runtime)

    @router.callback_query(F.data.startswith("cf:"))
    async def choose_conflict(callback: CallbackQuery, user: User) -> None:
        from deadline_assistant.handlers.bot import send_digest

        assert isinstance(callback.message, Message) and callback.data
        _, raw_id, version, number = callback.data.split(":")
        async with runtime.database.transaction() as session:
            await TenantRepository(session, user.id).resolve_conflict(
                UUID(raw_id), int(number), int(version)
            )
        await callback.answer("Выбор сохранён")
        await callback.message.edit_reply_markup(reply_markup=None)
        await send_digest(callback.message, user, runtime)

    @router.callback_query(F.data.startswith("cfcheck:"))
    async def recheck_conflict(callback: CallbackQuery, user: User) -> None:
        from deadline_assistant.handlers.bot import send_digest

        assert isinstance(callback.message, Message) and callback.data
        await callback.answer("Проверяю конфликт")
        await runtime.corrections.recheck(user.id, UUID(callback.data.split(":")[1]))
        await callback.message.edit_reply_markup(reply_markup=None)
        await send_digest(callback.message, user, runtime)

    @router.callback_query()
    async def stale_callback(callback: CallbackQuery) -> None:
        await callback.answer("Меню устарело. Откройте нужный раздел заново.", show_alert=True)

    @router.message(F.text.startswith("/"))
    async def unknown_command(message: Message, user: User, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "Откройте нужный раздел кнопками под сообщением.",
            reply_markup=await account_menu(user, runtime),
        )

    return router
