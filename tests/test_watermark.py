from __future__ import annotations

import json
import pytest
from pathlib import Path

from kbimporter.watermark import WatermarkState, WatermarkStore


def _store(tmp_path: Path, state: dict | None = None) -> WatermarkStore:
    path = tmp_path / "watermark.json"
    if state is not None:
        path.write_text(json.dumps(state), encoding="utf-8")
    return WatermarkStore(path)


def _note(note_id: str, created_at: str):
    from kbimporter.models import GetNote
    return GetNote(
        note_id=note_id,
        title="t",
        content="",
        note_type="",
        tags=[],
        topics=[],
        created_at=created_at,
    )


# ── should_skip ───────────────────────────────────────────────────────────────

class TestShouldSkip:
    def test_no_watermark_never_skips(self, tmp_path):
        store = _store(tmp_path)
        assert store.should_skip(_note("100", "2026-05-01 00:00:00")) is False

    def test_created_before_watermark_is_skipped(self, tmp_path):
        store = _store(tmp_path, {
            "last_created_at": "2026-05-12 22:53:19",
            "last_note_id": "1000",
            "processed_ids": ["1000"],
        })
        assert store.should_skip(_note("500", "2026-05-12 22:50:00")) is True

    def test_created_after_watermark_is_not_skipped(self, tmp_path):
        store = _store(tmp_path, {
            "last_created_at": "2026-05-12 22:53:19",
            "last_note_id": "1000",
            "processed_ids": ["1000"],
        })
        assert store.should_skip(_note("2000", "2026-05-12 23:00:00")) is False

    def test_same_timestamp_in_processed_ids_is_skipped(self, tmp_path):
        store = _store(tmp_path, {
            "last_created_at": "2026-05-12 22:53:19",
            "last_note_id": "1000",
            "processed_ids": ["1000"],
        })
        assert store.should_skip(_note("1000", "2026-05-12 22:53:19")) is True

    def test_same_timestamp_not_in_processed_ids_is_not_skipped(self, tmp_path):
        store = _store(tmp_path, {
            "last_created_at": "2026-05-12 22:53:19",
            "last_note_id": "1000",
            "processed_ids": ["1000"],
        })
        assert store.should_skip(_note("1001", "2026-05-12 22:53:19")) is False

    def test_higher_id_older_timestamp_not_yet_processed_is_not_skipped(self, tmp_path):
        """ID 比水位大但 created_at 更早的笔记（ID 非单调场景），尚未处理，不应跳过。"""
        store = _store(tmp_path, {
            "last_created_at": "2026-05-12 22:53:19",
            "last_note_id": "1909754630112552160",
            "processed_ids": ["1909754630112552160"],
        })
        note = _note("1909758555713248168", "2026-05-12 22:52:53")
        assert store.should_skip(note) is False

    def test_higher_id_older_timestamp_already_processed_is_skipped(self, tmp_path):
        """同一 ID 非单调场景，但该笔记已在 processed_ids 中，应跳过。"""
        store = _store(tmp_path, {
            "last_created_at": "2026-05-12 22:53:19",
            "last_note_id": "1909754630112552160",
            "processed_ids": ["1909754630112552160", "1909758555713248168"],
        })
        note = _note("1909758555713248168", "2026-05-12 22:52:53")
        assert store.should_skip(note) is True


# ── advance ───────────────────────────────────────────────────────────────────

class TestAdvance:
    def test_advance_newer_note_updates_watermark(self, tmp_path):
        store = _store(tmp_path, {
            "last_created_at": "2026-05-12 22:53:19",
            "last_note_id": "1000",
            "processed_ids": ["1000"],
        })
        store.advance(_note("2000", "2026-05-12 23:00:00"), "written")
        state = store.load()
        assert state.last_created_at == "2026-05-12 23:00:00"
        assert state.last_note_id == "2000"
        assert state.processed_ids == ["2000"]

    def test_advance_same_timestamp_appends_id(self, tmp_path):
        store = _store(tmp_path, {
            "last_created_at": "2026-05-12 22:53:19",
            "last_note_id": "1000",
            "processed_ids": ["1000"],
        })
        store.advance(_note("1001", "2026-05-12 22:53:19"), "written")
        state = store.load()
        assert state.last_note_id == "1001"
        assert "1001" in (state.processed_ids or [])

    def test_advance_higher_id_older_timestamp_adds_to_processed_ids(self, tmp_path):
        """ID 非单调场景：advance 后 processed_ids 收录该笔记，last_created_at 不变。"""
        store = _store(tmp_path, {
            "last_created_at": "2026-05-12 22:53:19",
            "last_note_id": "1909754630112552160",
            "processed_ids": ["1909754630112552160"],
        })
        store.advance(_note("1909758555713248168", "2026-05-12 22:52:53"), "written")
        state = store.load()
        assert "1909758555713248168" in (state.processed_ids or [])
        assert state.last_created_at == "2026-05-12 22:53:19"

    def test_advance_ignored_for_unknown_result(self, tmp_path):
        store = _store(tmp_path, {
            "last_created_at": "2026-05-12 22:53:19",
            "last_note_id": "1000",
            "processed_ids": ["1000"],
        })
        store.advance(_note("2000", "2026-05-12 23:00:00"), "error")
        state = store.load()
        assert state.last_created_at == "2026-05-12 22:53:19"
