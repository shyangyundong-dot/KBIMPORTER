from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from kbimporter.models import GetNote


class RouteError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RouteResult:
    route: str
    reason: str

    @property
    def is_skip(self) -> bool:
        return self.route == "skip"


class NoteRouter:
    def __init__(self, config_path: Path, exclude_topics: list[str] | None = None) -> None:
        self.config = json.loads(config_path.read_text(encoding="utf-8"))
        self._exclude_topics = [t.strip().lower() for t in (exclude_topics or []) if t.strip()]

    def route(self, note: GetNote) -> RouteResult:
        tags = set(note.tags)
        for excluded in self.config.get("global_exclude", []):
            if excluded in tags:
                return RouteResult("skip", f"global_exclude:{excluded}")
        if self._exclude_topics and self._topic_excluded(note):
            return RouteResult("skip", "exclude_topics")
        for rule in self.config.get("rules", []):
            match = rule.get("match", {})
            if self._matches(tags, match):
                return RouteResult(str(rule["route"]), "matched_rule")
        raise RouteError(f"No route matched note {note.note_id}: tags={note.tags}")

    def _topic_excluded(self, note: GetNote) -> bool:
        for topic in note.topics:
            for field in ("alias", "topic_alias", "slug", "short_id", "id", "topic_id", "name"):
                val = topic.get(field)
                if isinstance(val, str) and val.strip().lower() in self._exclude_topics:
                    return True
        return False

    @staticmethod
    def _matches(tags: set[str], match: dict[str, object]) -> bool:
        has_tag = match.get("has_tag")
        if has_tag and str(has_tag) not in tags:
            return False
        not_has_tag = match.get("not_has_tag")
        if not_has_tag and str(not_has_tag) in tags:
            return False
        has_any_tag = match.get("has_any_tag")
        if isinstance(has_any_tag, list) and not any(str(tag) in tags for tag in has_any_tag):
            return False
        has_all_tags = match.get("has_all_tags")
        if isinstance(has_all_tags, list) and not all(str(tag) in tags for tag in has_all_tags):
            return False
        return True
