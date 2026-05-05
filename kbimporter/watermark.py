from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from kbimporter.models import GetNote, parse_datetime


COMPLETED_RESULTS = {"written", "skipped_duplicate"}


@dataclass(slots=True)
class WatermarkState:
    last_created_at: str | None = None
    last_note_id: str | None = None
    processed_ids: list[str] | None = None

    @classmethod
    def empty(cls) -> "WatermarkState":
        return cls(last_created_at=None, last_note_id=None, processed_ids=[])

    def as_dict(self) -> dict[str, object]:
        return {
            "last_created_at": self.last_created_at,
            "last_note_id": self.last_note_id,
            "processed_ids": self.processed_ids or [],
        }


class WatermarkStore:
    def __init__(self, path: Path, max_processed_ids: int = 500) -> None:
        self.path = path
        self.max_processed_ids = max_processed_ids

    def load(self) -> WatermarkState:
        if not self.path.exists():
            return WatermarkState.empty()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return WatermarkState(
            last_created_at=data.get("last_created_at"),
            last_note_id=data.get("last_note_id"),
            processed_ids=list(data.get("processed_ids") or []),
        )

    def save(self, state: WatermarkState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(state.as_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def should_skip(self, note: GetNote, state: WatermarkState | None = None) -> bool:
        state = state or self.load()
        if not state.last_created_at:
            return False
        note_created = note.created_datetime
        watermark_created = parse_datetime(state.last_created_at)
        if note_created < watermark_created:
            return True
        if note_created == watermark_created:
            return note.note_id in set(state.processed_ids or [])
        return False

    def advance(self, note: GetNote, result: str) -> WatermarkState:
        state = self.load()
        if result not in COMPLETED_RESULTS:
            return state

        processed_ids = list(state.processed_ids or [])
        if note.note_id not in processed_ids:
            processed_ids.append(note.note_id)

        if not state.last_created_at or note.created_datetime > parse_datetime(state.last_created_at):
            processed_ids = [note.note_id]
            state.last_created_at = note.created_at
            state.last_note_id = note.note_id
        elif note.created_at == state.last_created_at:
            state.last_note_id = note.note_id

        state.processed_ids = processed_ids[-self.max_processed_ids :]
        self.save(state)
        return state
