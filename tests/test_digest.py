from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from uuid import uuid4

import pytest

from deadline_assistant.db.models import Event
from deadline_assistant.domain import EventData
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.services.digest import build_digest
from deadline_assistant.services.lifecycle import boundary, expired
from deadline_assistant.services.summary import group_events
from deadline_assistant.services.telegram_text import units

NOW = datetime.fromisoformat("2026-10-19T12:00:00+03:00")  # Monday


def event(title, when=None, precision="day", **kwargs):
    data = EventData(
        title=title, due_at=when, date_precision=precision if when else "unknown", **kwargs
    )
    return Event(id=uuid4(), user_id=uuid4(), version=1, **data.model_dump())


def names(groups):
    return [[item.event.title for item in group] for group in groups]


def test_four_sections_order_and_no_duplicates():
    events = [
        event("Ноябрь", "2026-11-07T12:00:00+03:00"),
        event("Конец месяца", "2026-10-31T12:00:00+03:00"),
        event("Среда", "2026-10-21T12:00:00+03:00"),
        event("Сегодня поздно", "2026-10-19T18:00:00+03:00", "exact"),
        event("Сегодня рано", "2026-10-19T13:00:00+03:00", "exact"),
        event("Воскресенье", "2026-10-25T23:59:00+03:00", "exact"),
        event("Прошлое", "2026-10-18T12:00:00+03:00"),
        event("Без даты"),
    ]
    assert names(group_events(events, "Europe/Moscow", NOW)) == [
        ["Сегодня рано", "Сегодня поздно"],
        ["Среда", "Воскресенье"],
        ["Конец месяца"],
        ["Ноябрь"],
        ["Без даты"],
    ]


@pytest.mark.parametrize(
    ("today", "target", "section"),
    [
        ("2026-10-30", "2026-11-01", 1),  # Sunday in the next month is still this week
        ("2026-10-30", "2026-11-02", 3),
        ("2026-12-31", "2027-01-03", 1),
        ("2026-12-31", "2027-01-04", 3),
        ("2026-10-25", "2026-10-26", 2),  # Sunday; Monday belongs to the month
        ("2026-10-26", "2026-10-31", 1),
        ("2028-02-28", "2028-02-29", 1),
    ],
)
def test_calendar_month_year_and_week_boundaries(today, target, section):
    now = datetime.fromisoformat(today + "T12:00:00+03:00")
    groups = group_events([event("Работа", target + "T18:00:00+03:00")], "Europe/Moscow", now)
    assert [index for index, group in enumerate(groups) if group] == [section]


def test_user_timezone_changes_today_and_handles_dst():
    now = datetime(2026, 10, 20, 0, 30, tzinfo=UTC)
    task = event("Работа", "2026-10-20T02:00:00+00:00", "exact")
    assert names(group_events([task], "Europe/Moscow", now))[0] == ["Работа"]
    assert names(group_events([task], "America/Los_Angeles", now))[0] == ["Работа"]
    # In LA the local date is still the 19th, while this event is the morning of the 20th.
    later = event("Позже", "2026-10-20T10:00:00+00:00", "exact")
    assert names(group_events([later], "Europe/Moscow", now))[0] == ["Позже"]
    assert names(group_events([later], "America/Los_Angeles", now))[1] == ["Позже"]
    start = datetime.fromisoformat("2026-03-08T00:00:00-05:00")
    assert boundary(start, "day", "America/New_York") == datetime(2026, 3, 9, 4, tzinfo=UTC)
    assert boundary(start, "day", "America/New_York") - start == timedelta(hours=23)


async def test_day_only_approximate_times_and_source_links(database, users):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        from deadline_assistant.domain import MessageData

        first = await repo.create_event(
            EventData(title="БЖД", due_at="2026-10-19T00:00:00+03:00", date_precision="day")
        )
        await repo.create_event(
            EventData(
                title="Консультация",
                due_at="2026-10-20T16:00:00+03:00",
                end_at="2026-10-20T18:00:00+03:00",
                date_precision="approximate",
            )
        )
        source = await repo.source(
            MessageData(
                chat_id=-1,
                message_id=1,
                text="Сдать БЖД",
                timestamp=NOW,
                source_url="https://t.me/math/42?x=1&y=2",
            )
        )
        await repo.attach_source(first.id, source.id)
        _, summary = await build_digest(repo, "Europe/Moscow", now=NOW)
        assert "Актуально на 19 октября 2026" in summary
        assert "23:59" not in summary and "00:00" not in summary
        assert "Примерно 20 октября 2026, 16:00–18:00" in summary
        assert '<a href="https://t.me/math/42?x=1&amp;y=2">ссылка</a>' in summary
        listing = await repo.event_list(summary.list_id)
        assert [e["id"] for e in listing.entries][0] == str(first.id)


async def test_active_resubmission_and_unknown_date_are_preserved(database, users):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        late_task = await repo.create_event(
            EventData(
                title="Досдача",
                due_at="2026-10-18T00:00:00+03:00",
                date_precision="day",
                resubmission_end="2026-10-20T00:00:00+03:00",
                penalties="Минус 10%",
            )
        )
        await repo.create_event(EventData(title="Ждём выдачи"))
        _, summary = await build_digest(repo, "Europe/Moscow", now=NOW)
        assert summary.index("Досдача</b>") < summary.index("НЕОПРЕДЕЛЁННОСТИ")
        assert "Основной срок: 18 октября 2026" in summary
        assert "Действует период досдачи" in summary and "Минус 10%" in summary
        assert summary.index("НЕОПРЕДЕЛЁННОСТИ") < summary.index("Ждём выдачи")
        _, next_day = await build_digest(repo, "Europe/Moscow", now=NOW + timedelta(days=1))
        assert "Досдача</b>" in next_day
        _, after = await build_digest(repo, "Europe/Moscow", now=NOW + timedelta(days=2))
        assert "Досдача</b>" not in after
        assert "Ждём выдачи" in after
        late_task.needs_clarification = True
        _, uncertain = await build_digest(repo, "Europe/Moscow", now=NOW)
        assert uncertain.index("НЕОПРЕДЕЛЁННОСТИ") < uncertain.index("Досдача</b>")
        assert "Действует период досдачи" in uncertain and "Предстоит досдача" not in uncertain


def test_future_resubmission_and_unknown_end():
    task = event(
        "Позже", "2026-10-17T00:00:00+03:00", resubmission_start="2026-10-22T15:00:00+03:00"
    )
    assert names(group_events([task], "Europe/Moscow", NOW))[1] == ["Позже"]
    assert not expired(
        EventData.model_validate({k: getattr(task, k) for k in EventData.model_fields}),
        "Europe/Moscow",
        NOW + timedelta(days=100),
    )


async def test_past_disputed_date_remains_visible(database, users):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        item = await repo.create_event(
            EventData(title="Спор", due_at="2026-10-01T00:00:00+03:00", date_precision="day")
        )
        await repo.create_conflict(
            item.id,
            EventData(title="Спор", due_at="2026-10-02T00:00:00+03:00", date_precision="day"),
        )
        cards, summary = await build_digest(
            repo, "Europe/Moscow", now=NOW, include_conflict_cards=True
        )
        assert cards and "Дата требует подтверждения" in summary
        assert "1 октября" in summary and "2 октября" in summary
        assert "НЕОПРЕДЕЛЁННОСТИ" in summary


async def test_conflicting_conditions_do_not_repeat_identical_unknown_dates(database, users):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        item = await repo.create_event(EventData(title="Факультатив", submission_conditions="Очно"))
        await repo.create_conflict(
            item.id, EventData(title="Факультатив", submission_conditions="Онлайн")
        )
        cards, summary = await build_digest(
            repo, "Europe/Moscow", now=NOW, include_conflict_cards=True
        )
        assert cards and "условия требуют подтверждения" in summary
        assert "Вариант: дата уточняется" not in summary
        assert "Дата требует подтверждения" not in summary


class ValidHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []

    def handle_starttag(self, tag, attrs):
        assert tag in {"a", "b"}
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag


async def test_large_summary_preserves_atomic_events_links_and_html(database, users):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        for number in range(35):
            await repo.create_event(
                EventData(
                    title=f"Задание {number:02} " + '<&"' * 80,
                    due_at="2026-10-20T18:00:00+03:00",
                    date_precision="exact",
                    submission_conditions="<&" * 1200,
                    penalties="<&" * 800,
                    location="<&" * 200,
                )
            )
        _, summary = await build_digest(repo, "Europe/Moscow", now=NOW)
        assert len(summary.chunks) > 3
        for chunk in summary.chunks:
            assert units(chunk) <= 3500
            parser = ValidHTML()
            parser.feed(chunk)
            assert parser.stack == []
        for block in summary.blocks:
            assert any(block in chunk for chunk in summary.chunks)
        for number in range(35):
            assert sum(f"Задание {number:02} " in chunk for chunk in summary.chunks) == 1


def test_today_past_hour_and_ongoing_multi_day_event_stay_today():
    tasks = [
        event("Уже было сегодня", "2026-10-19T10:00:00+03:00", "exact"),
        event(
            "Идёт несколько дней", "2026-10-18T00:00:00+03:00", end_at="2026-10-20T00:00:00+03:00"
        ),
    ]
    assert set(names(group_events(tasks, "Europe/Moscow", NOW))[0]) == {
        "Уже было сегодня",
        "Идёт несколько дней",
    }


def test_day_range_in_conflicts_and_picker_does_not_invent_hours():
    from deadline_assistant.services.digest import render_date

    data = EventData(
        title="Период",
        due_at="2026-10-19T00:00:00+03:00",
        end_at="2026-10-21T00:00:00+03:00",
        date_precision="day",
    )
    assert render_date(data, "Europe/Moscow") == "19.10.2026 — 21.10.2026"


def test_old_unconfirmed_deadline_is_hidden_but_active_grace_remains():
    tasks = [
        event("КР № 2", "2025-12-19T00:00:00+03:00", needs_clarification=True),
        event("Нет даты", needs_clarification=True),
        event(
            "Есть досдача",
            "2025-12-19T00:00:00+03:00",
            needs_clarification=True,
            resubmission_end="2026-11-01T00:00:00+03:00",
        ),
    ]
    visible = [name for group in names(group_events(tasks, "Europe/Moscow", NOW)) for name in group]
    assert set(visible) == {"Нет даты", "Есть досдача"}


async def test_approximate_calendar_range_does_not_display_invented_hours(database, users):
    from deadline_assistant.services.digest import render_date

    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        single = EventData(
            title="Контрольная",
            due_at="2026-10-24T00:00:00+03:00",
            end_at="2026-10-24T23:59:59+03:00",
            date_precision="approximate",
            needs_clarification=True,
        )
        multiple = EventData(
            title="Тестирование",
            due_at="2026-12-09T00:00:00+03:00",
            end_at="2026-12-15T23:59:00+03:00",
            date_precision="approximate",
        )
        await repo.create_event(single)
        await repo.create_event(multiple)
        _, summary = await build_digest(repo, "Europe/Moscow", now=NOW)
        assert "Примерно 24 октября 2026" in summary
        assert "Примерно 9 декабря 2026 — 15 декабря 2026" in summary
        assert "00:00" not in summary and "23:59" not in summary
        assert render_date(single, "Europe/Moscow") == "24.10.2026 (приблизительно)"


async def test_generic_control_titles_show_their_source_courses(database, users):
    from deadline_assistant.domain import MessageData

    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        for number, title in enumerate(("Матан ПИ", "HSE-SE-DM"), 1):
            task = await repo.create_event(EventData(title="Контрольная работа", event_type="test"))
            source = await repo.source(
                MessageData(
                    chat_id=-number,
                    message_id=1,
                    chat_title=title,
                    text="Контрольная",
                    timestamp=NOW,
                )
            )
            await repo.attach_source(task.id, source.id)
        _, summary = await build_digest(repo, "Europe/Moscow", now=NOW)
        assert "Матан ПИ — Контрольная работа" in summary
        assert "HSE-SE-DM — Контрольная работа" in summary


async def test_uncertainties_are_separate_compact_and_link_to_the_latest_evidence(database, users):
    from deadline_assistant.domain import MessageData

    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        definite = await repo.create_event(
            EventData(
                title="Алгебра — КР № 1",
                due_at="2026-10-21T00:00:00+03:00",
                date_precision="day",
                location=None,
            )
        )
        vague = await repo.create_event(
            EventData(
                title="Дискретка — контрольная",
                description="На следующей неделе; точный день не объявлен.",
                needs_clarification=True,
            )
        )
        approximate = await repo.create_event(
            EventData(
                title="Матан — тест",
                due_at="2026-10-26T00:00:00+03:00",
                end_at="2026-11-01T23:59:59+03:00",
                date_precision="approximate",
            )
        )
        for number, task in enumerate((definite, vague, approximate), 1):
            source = await repo.source(
                MessageData(
                    chat_id=-100123,
                    message_id=number,
                    text=task.title,
                    timestamp=NOW,
                    source_url=f"https://t.me/c/123/{number}",
                )
            )
            await repo.attach_source(task.id, source.id)
        cards, summary = await build_digest(repo, "Europe/Moscow", now=NOW)
        confirmed, uncertainties = summary.split("❔ НЕОПРЕДЕЛЁННОСТИ")
        assert not cards
        assert "Алгебра — КР № 1" in confirmed and "Алгебра — КР № 1" not in uncertainties
        assert "Дискретка — контрольная" not in confirmed
        assert "Матан — тест" not in confirmed
        assert "На следующей неделе; точный день не объявлен." in uncertainties
        assert '<a href="https://t.me/c/123/2">ссылка</a>' in uncertainties
        assert "00:00" not in summary and "23:59" not in summary
        assert "Место:" not in summary and "Нет предстоящих событий" not in summary
        assert all(summary.count(task.title) == 1 for task in (definite, vague, approximate))

        alternative = await repo.source(
            MessageData(
                chat_id=-100123,
                message_id=4,
                text="КР перенесли, но есть противоречащие подтверждения",
                timestamp=NOW + timedelta(hours=1),
                source_url="https://t.me/c/123/4?x=1&y=2",
            )
        )
        await repo.create_conflict(
            definite.id,
            EventData(
                title=definite.title,
                due_at="2026-10-22T00:00:00+03:00",
                date_precision="day",
            ),
            [alternative.id],
        )
        cards, summary = await build_digest(repo, "Europe/Moscow", now=NOW)
        assert not cards and "📅 НА ЭТОЙ НЕДЕЛЕ" not in summary
        assert '<a href="https://t.me/c/123/4?x=1&amp;y=2">ссылка</a>' in summary
        assert "21 октября 2026 / 22 октября 2026" in summary
        cards, _ = await build_digest(repo, "Europe/Moscow", now=NOW, include_conflict_cards=True)
        assert len(cards) == 1 and "Вариант 2" in cards[0].text


async def test_resolved_uncertainty_moves_to_calendar_and_empty_block_disappears(database, users):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        task = await repo.create_event(EventData(title="Контрольная", needs_clarification=True))
        _, summary = await build_digest(repo, "Europe/Moscow", now=NOW)
        assert "НЕОПРЕДЕЛЁННОСТИ" in summary
        await repo.write_event(
            task,
            EventData(
                title="Контрольная",
                due_at="2026-10-21T00:00:00+03:00",
                date_precision="day",
            ),
        )
        _, summary = await build_digest(repo, "Europe/Moscow", now=NOW)
        assert "НЕОПРЕДЕЛЁННОСТИ" not in summary
        assert "📅 НА ЭТОЙ НЕДЕЛЕ" in summary
        assert summary.count("Контрольная") == 1


async def test_uncertainty_does_not_invent_missing_or_unsafe_source_links(database, users):
    from deadline_assistant.domain import MessageData

    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        task = await repo.create_event(EventData(title="<Тест & ДЗ>"))
        source = await repo.source(
            MessageData(
                chat_id=-1,
                message_id=1,
                text="Когда-то будет тест",
                timestamp=NOW,
                source_url="javascript:alert(1)",
            )
        )
        await repo.attach_source(task.id, source.id)
        _, summary = await build_digest(repo, "Europe/Moscow", now=NOW)
        assert "&lt;Тест &amp; ДЗ&gt;" in summary
        assert "ссылка недоступна" in summary
        assert "javascript:" not in summary and "<a " not in summary
