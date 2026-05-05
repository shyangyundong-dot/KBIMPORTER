from __future__ import annotations

import json
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path


MAX_AUTO_LEARN_CHARS = 12


@dataclass(slots=True)
class DictionaryUpdate:
    learned: dict[str, str] = field(default_factory=dict)
    manual_confirm: dict[str, dict[str, str]] = field(default_factory=dict)
    conflicts: dict[str, dict[str, str]] = field(default_factory=dict)
    ignored: list[str] = field(default_factory=list)

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)


class CorrectionDictionary:
    def __init__(self, json_path: Path, markdown_path: Path | None = None) -> None:
        self.json_path = json_path
        self.markdown_path = markdown_path or json_path.with_suffix(".md")

    def load(self) -> dict[str, object]:
        if not self.json_path.exists():
            return {"auto_replace": {}, "manual_confirm": {}, "whitelist": []}
        return json.loads(self.json_path.read_text(encoding="utf-8"))

    def save(self, data: dict[str, object]) -> None:
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        self.json_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.sync_markdown(data)

    def update_from_edit(self, before: str, after: str) -> DictionaryUpdate:
        data = self.load()
        update = infer_mappings(before, after, data)
        if update.conflicts:
            return update
        auto_replace = _dict(data, "auto_replace")
        manual_confirm = _dict(data, "manual_confirm")
        auto_replace.update(update.learned)
        manual_confirm.update(update.manual_confirm)
        data["auto_replace"] = auto_replace
        data["manual_confirm"] = manual_confirm
        self.save(data)
        return update

    def sync_markdown(self, data: dict[str, object] | None = None) -> None:
        data = data or self.load()
        lines = ["# 纠错词表", "", "## 自动替换", ""]
        for wrong, correct in sorted(_dict(data, "auto_replace").items()):
            lines.append(f"- `{wrong}` -> `{correct}`")
        lines.extend(["", "## 待确认", ""])
        for wrong, detail in sorted(_dict(data, "manual_confirm").items()):
            if isinstance(detail, dict):
                suggested = detail.get("suggested", "")
                risk = detail.get("risk", "")
                lines.append(f"- `{wrong}` -> `{suggested}`：{risk}")
            else:
                lines.append(f"- `{wrong}`")
        lines.extend(["", "## 白名单", ""])
        for item in sorted(data.get("whitelist") or []):
            lines.append(f"- `{item}`")
        self.markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def infer_mappings(before: str, after: str, dictionary: dict[str, object]) -> DictionaryUpdate:
    update = DictionaryUpdate()
    auto_replace = _dict(dictionary, "auto_replace")
    whitelist = set(dictionary.get("whitelist") or [])

    matcher = SequenceMatcher(a=before, b=after)
    for tag, start_a, end_a, start_b, end_b in matcher.get_opcodes():
        if tag == "equal":
            continue
        wrong = before[start_a:end_a]
        correct = after[start_b:end_b]

        if not _is_safe_mapping(wrong, correct):
            update.ignored.append(f"{wrong!r}->{correct!r}")
            continue
        if wrong in whitelist:
            update.manual_confirm[wrong] = {"suggested": correct, "risk": "hit whitelist"}
            continue
        existing = auto_replace.get(wrong)
        if existing and existing != correct:
            update.conflicts[wrong] = {"existing": existing, "new": correct}
        elif _looks_low_confidence(wrong, correct):
            update.manual_confirm[wrong] = {"suggested": correct, "risk": "low confidence edit"}
        else:
            update.learned[wrong] = correct
    return update


def _is_safe_mapping(wrong: str, correct: str) -> bool:
    if not wrong or not correct:
        return False
    if len(wrong) > MAX_AUTO_LEARN_CHARS or len(correct) > MAX_AUTO_LEARN_CHARS:
        return False
    if "\n" in wrong or "\n" in correct:
        return False
    return True


def _looks_low_confidence(wrong: str, correct: str) -> bool:
    punctuation = set("，。！？；：,.!?;:")
    return bool(set(wrong) & punctuation or set(correct) & punctuation)


def _dict(data: dict[str, object], key: str) -> dict:
    value = data.get(key) or {}
    return dict(value) if isinstance(value, dict) else {}
