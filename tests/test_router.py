from __future__ import annotations

import json
import pytest
from pathlib import Path

from kbimporter.models import GetNote
from kbimporter.router import NoteRouter, RouteError, RouteResult


# ── fixtures ──────────────────────────────────────────────────────────────────

def _make_note(
    *,
    tags: list[str] | None = None,
    topics: list[dict] | None = None,
    note_id: str = "1",
) -> GetNote:
    return GetNote(
        note_id=note_id,
        title="test",
        content="",
        note_type="",
        tags=tags or [],
        topics=topics or [],
        created_at="2026-01-01 00:00:00",
    )


def _make_router(tmp_path: Path, config: dict, exclude_topics: list[str] | None = None) -> NoteRouter:
    cfg_file = tmp_path / "routes.json"
    cfg_file.write_text(json.dumps(config), encoding="utf-8")
    return NoteRouter(cfg_file, exclude_topics=exclude_topics)


STANDARD_CONFIG = {
    "global_exclude": ["菜谱"],
    "rules": [
        {"match": {"has_tag": "AI链接笔记"}, "route": "ai-link"},
        {"match": {"has_tag": "录音卡笔记"}, "route": "audio-card"},
    ],
}


# ── global_exclude ────────────────────────────────────────────────────────────

def test_global_exclude_returns_skip(tmp_path: Path) -> None:
    router = _make_router(tmp_path, STANDARD_CONFIG)
    note = _make_note(tags=["菜谱"])
    result = router.route(note)
    assert result.is_skip
    assert "global_exclude" in result.reason


def test_global_exclude_takes_priority_over_rules(tmp_path: Path) -> None:
    router = _make_router(tmp_path, STANDARD_CONFIG)
    note = _make_note(tags=["菜谱", "AI链接笔记"])
    result = router.route(note)
    assert result.is_skip


# ── exclude_topics ────────────────────────────────────────────────────────────

def test_topic_excluded_by_name(tmp_path: Path) -> None:
    router = _make_router(tmp_path, STANDARD_CONFIG, exclude_topics=["recipe-topic"])
    note = _make_note(topics=[{"name": "recipe-topic"}])
    result = router.route(note)
    assert result.is_skip
    assert result.reason == "exclude_topics"


def test_topic_excluded_by_alias(tmp_path: Path) -> None:
    router = _make_router(tmp_path, STANDARD_CONFIG, exclude_topics=["pn5wNaO0"])
    note = _make_note(topics=[{"alias": "pn5wNaO0"}])
    result = router.route(note)
    assert result.is_skip


def test_topic_excluded_by_id(tmp_path: Path) -> None:
    router = _make_router(tmp_path, STANDARD_CONFIG, exclude_topics=["42"])
    note = _make_note(topics=[{"id": "42"}])
    result = router.route(note)
    assert result.is_skip


def test_topic_exclusion_is_case_insensitive(tmp_path: Path) -> None:
    router = _make_router(tmp_path, STANDARD_CONFIG, exclude_topics=["RecipeTopic"])
    note = _make_note(topics=[{"alias": "recipetopic"}])
    result = router.route(note)
    assert result.is_skip


def test_topic_excluded_takes_priority_over_rules(tmp_path: Path) -> None:
    router = _make_router(tmp_path, STANDARD_CONFIG, exclude_topics=["recipe"])
    note = _make_note(tags=["AI链接笔记"], topics=[{"name": "recipe"}])
    result = router.route(note)
    assert result.is_skip


def test_non_matching_topic_does_not_skip(tmp_path: Path) -> None:
    router = _make_router(tmp_path, STANDARD_CONFIG, exclude_topics=["recipe"])
    note = _make_note(tags=["AI链接笔记"], topics=[{"name": "other-topic"}])
    result = router.route(note)
    assert result.route == "ai-link"


def test_no_exclude_topics_injected(tmp_path: Path) -> None:
    router = _make_router(tmp_path, STANDARD_CONFIG, exclude_topics=None)
    note = _make_note(tags=["AI链接笔记"], topics=[{"name": "recipe"}])
    result = router.route(note)
    assert result.route == "ai-link"


# ── tag rules ─────────────────────────────────────────────────────────────────

def test_ai_link_tag_routes_to_ai_link(tmp_path: Path) -> None:
    router = _make_router(tmp_path, STANDARD_CONFIG)
    note = _make_note(tags=["AI链接笔记"])
    assert router.route(note).route == "ai-link"


def test_audio_card_tag_routes_to_audio_card(tmp_path: Path) -> None:
    router = _make_router(tmp_path, STANDARD_CONFIG)
    note = _make_note(tags=["录音卡笔记"])
    assert router.route(note).route == "audio-card"


def test_first_matching_rule_wins(tmp_path: Path) -> None:
    # AI链接笔记 rule is listed before 录音卡笔记; note has both tags
    router = _make_router(tmp_path, STANDARD_CONFIG)
    note = _make_note(tags=["AI链接笔记", "录音卡笔记"])
    assert router.route(note).route == "ai-link"


def test_unknown_tag_raises_route_error(tmp_path: Path) -> None:
    router = _make_router(tmp_path, STANDARD_CONFIG)
    note = _make_note(tags=["未知标签"])
    with pytest.raises(RouteError):
        router.route(note)


def test_no_tags_raises_route_error(tmp_path: Path) -> None:
    router = _make_router(tmp_path, STANDARD_CONFIG)
    note = _make_note(tags=[])
    with pytest.raises(RouteError):
        router.route(note)


# ── match conditions ──────────────────────────────────────────────────────────

def test_not_has_tag_blocks_match(tmp_path: Path) -> None:
    config = {
        "global_exclude": [],
        "rules": [
            {"match": {"has_tag": "AI链接笔记", "not_has_tag": "草稿"}, "route": "ai-link"},
        ],
    }
    router = _make_router(tmp_path, config)
    assert router.route(_make_note(tags=["AI链接笔记"])).route == "ai-link"
    with pytest.raises(RouteError):
        router.route(_make_note(tags=["AI链接笔记", "草稿"]))


def test_has_any_tag_matches_when_one_present(tmp_path: Path) -> None:
    config = {
        "global_exclude": [],
        "rules": [
            {"match": {"has_any_tag": ["tag-a", "tag-b"]}, "route": "target"},
        ],
    }
    router = _make_router(tmp_path, config)
    assert router.route(_make_note(tags=["tag-b"])).route == "target"
    with pytest.raises(RouteError):
        router.route(_make_note(tags=["tag-c"]))


def test_has_all_tags_requires_all(tmp_path: Path) -> None:
    config = {
        "global_exclude": [],
        "rules": [
            {"match": {"has_all_tags": ["tag-a", "tag-b"]}, "route": "target"},
        ],
    }
    router = _make_router(tmp_path, config)
    assert router.route(_make_note(tags=["tag-a", "tag-b"])).route == "target"
    with pytest.raises(RouteError):
        router.route(_make_note(tags=["tag-a"]))
