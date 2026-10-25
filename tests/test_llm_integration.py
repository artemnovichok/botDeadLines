"""Explicit opt-in; normal test runs never contact LowRouter or spend credits."""

import os
from datetime import datetime

import pytest

from deadline_assistant.config import Settings
from deadline_assistant.domain import AnalysisContext, CreateEvent, MessageData
from deadline_assistant.integrations.llm import OpenAIAnalyzer

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_LLM_INTEGRATION") != "1"
        or not (os.getenv("LOWROUTER_API_KEY") or os.getenv("LLM_API_KEY")),
        reason="Нужны RUN_LLM_INTEGRATION=1 и LOWROUTER_API_KEY в окружении",
    ),
]


@pytest.mark.parametrize(
    "sent,text,expected",
    [
        ("2026-10-12T10:00:00+03:00", "Сдать лабораторную через неделю", "2026-10-19"),
        ("2026-10-31T10:00:00+03:00", "Завтра контрольная по алгебре", "2026-11-01"),
        ("2026-12-31T10:00:00+03:00", "Через два дня встреча по проекту", "2027-01-02"),
    ],
)
async def test_real_lowrouter_relative_dates(sent, text, expected):
    from zoneinfo import ZoneInfo

    settings = Settings(_env_file=None)
    analyzer = OpenAIAnalyzer(settings)
    messages = [
        MessageData(
            chat_id=-1,
            chat_title="Тестовый учебный чат",
            message_id=1,
            timestamp=sent,
            author_name="Преподаватель",
            author_role="teacher",
            text=text,
        )
    ]
    context = AnalysisContext(current_time=datetime.fromisoformat(sent))
    try:
        output = await analyzer.analyze(messages, [], "Europe/Moscow", context)
        creations = [o for o in output.result.operations if isinstance(o, CreateEvent)]
        assert len(creations) == 1
        due = creations[0].event.due_at
        assert due is not None
        assert due.astimezone(ZoneInfo("Europe/Moscow")).date().isoformat() == expected
    finally:
        await analyzer.close()
