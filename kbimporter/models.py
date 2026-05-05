from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
)


def parse_datetime(value: str) -> datetime:
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class NoteRecord:
    """Simplified note model used in recipe sync (upsert flow)."""
    external_id: str
    title: str
    body: str
    video_url_from_property: str | None = None
    biji_updated_at: str | None = None


@dataclass(slots=True)
class GetNote:
    """Full note model used in import flow (AI links + audio cards)."""
    note_id: str
    title: str
    content: str
    note_type: str
    tags: list[str]
    topics: list[dict[str, Any]]
    created_at: str
    updated_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api_note(cls, raw: dict[str, Any]) -> "GetNote":
        return cls(
            note_id=str(raw.get("note_id") or raw.get("id") or ""),
            title=raw.get("title") or "",
            content=raw.get("content") or "",
            note_type=raw.get("note_type") or "",
            tags=_normalize_tags(raw.get("tags")),
            topics=_normalize_topics(raw.get("topics")),
            created_at=raw.get("created_at") or "",
            updated_at=raw.get("updated_at"),
            raw=raw,
        )

    @property
    def created_datetime(self) -> datetime:
        return parse_datetime(self.created_at)

    def web_page_url(self) -> str:
        page = self.raw.get("web_page")
        return page.get("url", "") if isinstance(page, dict) else ""

    def web_page_content(self) -> str:
        page = self.raw.get("web_page")
        if not isinstance(page, dict):
            return ""
        return page.get("content") or page.get("excerpt") or ""


def _normalize_tags(value: Any) -> list[str]:
    if not value:
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("tag_name") or item.get("title")
            if name:
                result.append(str(name))
    return result


def _normalize_topics(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            result.append({"name": item})
        elif isinstance(item, dict):
            result.append(item)
    return result
