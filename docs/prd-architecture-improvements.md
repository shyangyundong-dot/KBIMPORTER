# PRD: Architecture Improvements — KBImporter

## Problem Statement

During an architecture review of KBImporter, five defects were identified that affect reliability, performance, and operational clarity. The issues range from hard stops that block processing pipelines entirely, to silent failures that accumulate duplicate data in Notion without any warning. As the note volume grows, these defects become increasingly costly.

## Solution

A targeted set of fixes applied to the routing, import iteration, and audio card flow. Each fix addresses a specific failure mode without introducing new dependencies or changing the external CLI interface. All changes are backwards-compatible with existing state files and config.

## User Stories

1. As a user running `sync`, I want notes with unrecognised tags to be silently skipped (not block the pipeline), so that new or misconfigured notes in Get 笔记 don't interrupt the entire run.
2. As a user running `audio`, I want notes with unrecognised tags to be silently skipped in the same way, so that the interactive flow is not interrupted by stray notes.
3. As a user, I want a summary at the end of each run listing any notes that could not be matched to a route, so that I can review and fix tag configuration without losing the rest of the run's output.
4. As a user, I want unmatched notes to advance the watermark, so that I am not shown the same unmatched note on every subsequent run.
5. As a user running `sync` incrementally, I want the import flow to start fetching notes from the watermark position rather than from the beginning, so that API calls do not grow linearly with the total note count.
6. As a user, I want recipe notes to be excluded from the import flow based on their knowledge-base topic, not just by a manually applied tag, so that a recipe note missing the "菜谱" tag does not leak into the AI-link or audio-card pipelines.
7. As a user, I want the topic exclusion to use the same `BIJI_TOPIC_ID_RECIPE` value that is already configured, so that I do not need to maintain the same information in two places.
8. As a user writing to a Notion database that lacks a source_id field, I want an explicit info message telling me that dedup relies on the watermark, so that I understand the dedup model and am not surprised if a crash causes a rare duplicate.
9. As a user running `audio`, I want the list of available Notion databases to be fetched once at startup, so that each note does not trigger a separate Notion API call during the interactive review loop.
10. As a user running `audio` when the Notion API is unavailable, I want the command to fail early with a clear error, so that I do not enter the review loop only to discover I cannot write at the end.
11. As a user, I want the `audio` command to always show a fresh database list based on the current Notion state, so that newly created databases appear without requiring manual edits to any config file.
12. As a developer, I want `NoteRouter` to receive its excluded topics via constructor injection rather than reading them from `routes.json`, so that the CLI's env-var knowledge is not duplicated in a config file.

## Implementation Decisions

### NoteRouter — topic-level exclusion

`NoteRouter` accepts an optional `exclude_topics` list at construction time. The list is normalised to lowercase strings. Before evaluating tag-based rules, `route()` checks whether any of the note's topic fields (alias, slug, id, name, etc.) match the exclusion list. A match returns `RouteResult("skip", "exclude_topics")`.

The CLI injects the recipe topic alias and numeric ID from the already-loaded recipe config, with a graceful fallback to an empty list when recipe config is not available (e.g., in a pure audio-card-only setup).

### NoteRouter — lenient fallback for unmatched notes

`RouteError` is no longer raised to the caller. Both `cmd_sync` and `cmd_audio` catch it, collect the note in a per-run list, advance the watermark with `"skipped_duplicate"`, and continue. At the end of the run, a warning block is printed listing every unmatched note with its `note_id`, title, and tags.

### iter_import_notes — server-side watermark filtering

`iter_import_notes` reads the watermark state before beginning iteration and passes `last_note_id` and `last_created_at` to `iter_raw_notes` as `start_since_id` and `start_since_updated_at`. `iter_raw_notes` already implements dual-watermark logic: notes with ID below the watermark are only dropped if their activity timestamp is also within the watermark window, handling out-of-order IDs safely.

### cmd_audio — single database fetch at startup

`cmd_audio` fetches the Notion database list immediately after constructing `NotionImportWriter`, filters out the AI-link database, and holds the result in memory for the duration of the session. `_select_audio_database` is simplified to accept this pre-fetched list directly. If the fetch fails, the command exits before entering the note loop. The `notion_databases` static list in `targets.json` is no longer consulted.

### NotionImportWriter — explicit source_id warning

When `_find_source_id_property` returns `None` (no matching column in the Notion database schema), a one-line info message is printed explaining that dedup falls back to the watermark. Writing proceeds normally. No error is raised; the watermark is the primary dedup mechanism and the Notion check is a secondary safety net.

## Testing Decisions

**What makes a good test:** Tests should verify the external behaviour of a module given inputs, not assert on internal state or call sequences. A test should remain valid if the implementation is refactored.

**NoteRouter** is the highest-value module to test. It is a pure, stateless function with no I/O: given a `GetNote` and a config dict, it returns a `RouteResult` or raises. The following behaviours should be covered:

- A note with a `global_exclude` tag returns `skip`
- A note whose topic matches the injected `exclude_topics` list returns `skip`
- A note matching a tag rule returns the corresponding route
- A note matching no rule and not excluded raises `RouteError` (the router itself still raises; the CLI catches it)
- Rules are evaluated in order; the first match wins
- Topic matching is case-insensitive and checks multiple topic fields (alias, id, name)

**WatermarkStore** is worth testing in isolation: `should_skip` with timestamps before, at, and after the watermark; `advance` correctly updating `last_created_at` and `processed_ids`.

There are currently no test files in the codebase, so any new tests will establish the testing pattern. Use plain `pytest` with in-memory fixtures (small `dict` payloads for notes, `tmp_path` for watermark files).

## Out of Scope

- Retry logic for `output/failed/` items — transient failures are already retried by the watermark mechanism; truly permanent failures are left for manual review.
- Changing Notion database schemas to add source_id fields.
- Automatic scheduling or notification for `sync` failures.
- Any UI beyond the existing CLI.

## Implementation Status

Last updated: 2026-05-11

| # | Implementation Decision | Status | Notes |
|---|------------------------|--------|-------|
| 1 | NoteRouter — topic-level exclusion via `exclude_topics` constructor param | ✅ Done | `cli.py` injects `_recipe_topics` into both `cmd_sync` and `cmd_audio` |
| 2 | NoteRouter — lenient fallback + end-of-run unmatched warning | ✅ Done | Both `cmd_sync` and `cmd_audio` collect `unmatched_*` list and print warning |
| 3 | `iter_import_notes` — server-side watermark filtering | ✅ Done | `watermark=watermark` passed; `iter_raw_notes` uses `start_since_id` |
| 4 | `cmd_audio` — single DB fetch at startup, fail-fast if unavailable | ✅ Done | DB list fetched once; on fetch failure prints `[error]` and returns 1 |
| 5 | `NotionImportWriter` — explicit source_id warning | ✅ Done | `[info]` printed when `source_id` prop absent; seen in both `write_ai_link` and `write_audio_card` |


## Further Notes

- `CONTEXT.md` was created at the project root during this session, capturing the domain glossary and key relationships between the three note types and two commands.
- `docs/adr/0001-sync-audio-split-by-automation.md` records the decision to split commands by automation level rather than note type.
- The `notion_databases` field in `targets.json` is now unused by the audio flow. It can be removed from the file at any time without breaking anything.
