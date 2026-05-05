from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ReplacementRecord:
    wrong: str
    correct: str
    start: int
    end: int
    context: str


@dataclass(slots=True)
class PendingConfirmation:
    wrong: str
    suggested: str | None
    reason: str
    context: str


@dataclass(slots=True)
class CleanResult:
    original_text: str
    cleaned_text: str
    replacements: list[ReplacementRecord] = field(default_factory=list)
    pending_confirmations: list[PendingConfirmation] = field(default_factory=list)


class CorrectionCleaner:
    def __init__(self, dictionary: dict[str, object], context_window: int = 24) -> None:
        self.dictionary = dictionary
        self.context_window = context_window

    def clean(self, text: str) -> CleanResult:
        result = CleanResult(original_text=text, cleaned_text=text)
        whitelist = set(self.dictionary.get("whitelist") or [])

        for wrong, correct in self._auto_replace().items():
            if wrong in whitelist:
                continue
            self._apply_mapping(result, wrong, str(correct))

        for wrong, detail in self._manual_confirm().items():
            for start, end, context in _find_occurrences(result.cleaned_text, wrong, self.context_window):
                suggested = detail.get("suggested") if isinstance(detail, dict) else None
                reason = detail.get("risk") if isinstance(detail, dict) else "manual confirmation"
                result.pending_confirmations.append(
                    PendingConfirmation(wrong, suggested, reason or "", context)
                )

        return result

    def _auto_replace(self) -> dict[str, str]:
        value = self.dictionary.get("auto_replace") or {}
        if not isinstance(value, dict):
            return {}
        return {str(k): str(v) for k, v in value.items()}

    def _manual_confirm(self) -> dict[str, object]:
        value = self.dictionary.get("manual_confirm") or {}
        return value if isinstance(value, dict) else {}

    def _apply_mapping(self, result: CleanResult, wrong: str, correct: str) -> None:
        cursor = 0
        while True:
            index = result.cleaned_text.find(wrong, cursor)
            if index < 0:
                break
            end = index + len(wrong)
            context = _context(result.cleaned_text, index, end, self.context_window)
            result.cleaned_text = result.cleaned_text[:index] + correct + result.cleaned_text[end:]
            result.replacements.append(
                ReplacementRecord(wrong, correct, index, index + len(correct), context)
            )
            cursor = index + len(correct)


def _find_occurrences(text: str, needle: str, window: int) -> list[tuple[int, int, str]]:
    result: list[tuple[int, int, str]] = []
    cursor = 0
    while True:
        index = text.find(needle, cursor)
        if index < 0:
            return result
        end = index + len(needle)
        result.append((index, end, _context(text, index, end, window)))
        cursor = end


def _context(text: str, start: int, end: int, window: int) -> str:
    return text[max(0, start - window) : min(len(text), end + window)]
