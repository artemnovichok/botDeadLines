"""Explicit HTML messages and block-aware splitting for the Telegram transport."""

from collections.abc import Iterable
from html import escape
from uuid import UUID

LIMIT = 3500


def units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def truncate(text: str, limit: int) -> str:
    if units(text) <= limit:
        return text
    result = []
    used = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > limit - 1:
            break
        result.append(char)
        used += width
    return "".join(result) + "…"


def escaped(text: str, limit: int) -> str:
    """Truncate before HTML escaping, with a budget for the escaped representation."""
    result: list[str] = []
    used = 0
    for char in text:
        encoded = escape(char)
        width = units(encoded)
        if used + width > limit - 1:
            return "".join(result) + "…"
        result.append(encoded)
        used += width
    return "".join(result)


def pack(blocks: Iterable[str], limit: int = LIMIT) -> list[str]:
    parts: list[str] = []
    current = ""
    for block in blocks:
        if units(block) > limit:
            raise ValueError("Один блок не помещается в сообщение Telegram")
        combined = current + ("\n\n" if current else "") + block
        if units(combined) > limit:
            parts.append(current)
            current = block
        else:
            current = combined
    if current:
        parts.append(current)
    return parts


class HtmlText(str):
    """Complete, escaped HTML fragments; each block is independently sendable."""

    blocks: list[str]
    list_id: UUID | None = None

    def __new__(cls, blocks: list[str]) -> "HtmlText":
        value = super().__new__(cls, "\n\n".join(blocks))
        value.blocks = blocks
        return value

    @property
    def chunks(self) -> list[str]:
        return pack(self.blocks)
