import hashlib
import json
from uuid import UUID

from sqlalchemy import select

from deadline_assistant.db.models import AnalysisBatch
from deadline_assistant.db.session import Database
from deadline_assistant.domain import AnalysisContext, AnalysisOutput, AnalysisResult, MessageData
from deadline_assistant.repositories.users import UserRepository
from deadline_assistant.services.analysis import validate_result
from deadline_assistant.services.interfaces import Analyzer


async def analyze_batch(
    database: Database,
    analyzer: Analyzer,
    user_id: UUID,
    messages: list[MessageData],
    timezone: str,
    context: AnalysisContext,
) -> AnalysisOutput:
    from deadline_assistant.integrations.llm import SYSTEM_PROMPT

    memory = context.model_dump(mode="json", exclude={"current_time"})
    for collection in ("events", "sources", "corrections"):
        memory[collection] = sorted(memory[collection], key=lambda entry: entry["id"])
    payload = {
        "protocol": SYSTEM_PROMPT,
        "schema": AnalysisResult.model_json_schema(),
        "model": analyzer.model,
        "configuration": getattr(analyzer, "cache_namespace", None)
        if isinstance(getattr(analyzer, "cache_namespace", None), str)
        else None,
        "timezone": timezone,
        "messages": [m.model_dump(mode="json") for m in messages],
        # Relative dates are anchored to message timestamps; wall clock alone must not bill again.
        "context": memory,
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    async with database.lock(f"batch:{user_id}:{fingerprint}", timeout=600):
        async with database.transaction() as session:
            saved = await session.scalar(
                select(AnalysisBatch).where(
                    AnalysisBatch.user_id == user_id,
                    AnalysisBatch.fingerprint == fingerprint,
                )
            )
            if saved:
                return AnalysisOutput(
                    result=AnalysisResult.model_validate(saved.output),
                    token_usage={"cached_batches": 1},
                )
        output = await analyzer.analyze(messages, [], timezone, context)
        validate_result(output.result, messages, context)
        async with database.transaction() as session:
            await UserRepository(session).require(user_id)
            session.add(
                AnalysisBatch(
                    user_id=user_id,
                    fingerprint=fingerprint,
                    output=output.result.model_dump(mode="json"),
                    token_usage=output.token_usage,
                )
            )
        return output
