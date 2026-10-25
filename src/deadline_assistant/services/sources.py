from typing import TYPE_CHECKING
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram.types import Message

from deadline_assistant.db.models import User
from deadline_assistant.domain import EntityNotFound
from deadline_assistant.repositories.tenant import TenantRepository

if TYPE_CHECKING:
    from deadline_assistant.runtime import Runtime


async def show_sources(
    message: Message,
    user: User,
    runtime: "Runtime",
    event_id: UUID,
    source_id: UUID | None = None,
) -> None:
    from deadline_assistant.handlers.bot import send_chunks
    from deadline_assistant.handlers.event_actions import short_id
    from deadline_assistant.handlers.menus import keyboard

    async with runtime.database.transaction() as session:
        items = await TenantRepository(session, user.id).sources(event_id, include_conflicts=True)
    if not items:
        await message.answer("У задания пока нет сохранённых источников.")
        return
    items.sort(key=lambda source: (source.message_timestamp, str(source.id)))
    source = next((s for s in items if s.id == source_id), None) if source_id else items[0]
    if source is None:
        raise EntityNotFound
    zone = ZoneInfo(user.timezone)
    author = source.author_metadata.get("name") or source.author_metadata.get("id") or "неизвестен"
    text = (
        f"Источник {items.index(source) + 1}/{len(items)}\n"
        f"Чат: {source.chat_title or source.source_chat_id}\nАвтор: {author}\n"
        f"Отправлено: {source.message_timestamp.astimezone(zone):%d.%m.%Y %H:%M}\n"
    )
    if source.edit_timestamp:
        text += f"Изменено: {source.edit_timestamp.astimezone(zone):%d.%m.%Y %H:%M}\n"
    text += "\n" + (source.original_text or "Текст удалён по политике хранения")
    if source.source_url:
        text += "\n" + source.source_url
    if not source.available:
        text += "\nИсточник недоступен в Telegram; показана сохранённая версия."
    await send_chunks(message.answer, text)
    if len(items) > 1:
        # Page through sources without broadcasting their full texts at once.
        index = items.index(source)
        buttons = []
        for offset, label in ((-1, "← Предыдущий источник"), (1, "Следующий источник →")):
            if 0 <= index + offset < len(items):
                target = items[index + offset]
                buttons.append((label, f"src:{short_id(event_id)}:{short_id(target.id)}"))
        await message.answer("Другие сохранённые источники:", reply_markup=keyboard([buttons]))
