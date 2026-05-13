from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from notion_client import Client

from kbimporter.models import GetNote
from kbimporter.recipe_writer import resolve_notion_database_context


MAX_BLOCKS_PER_REQUEST = 100
_CHUNK = 2000


# ── Notion block 构建 ─────────────────────────────────────────────────────────

def _rich_text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": {"content": text[:_CHUNK]}}


def _rich_text_items(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for chunk, is_bold in _split_bold_runs(text):
        while len(chunk) > _CHUNK:
            items.append(_annotated_rich_text(chunk[:_CHUNK], is_bold))
            chunk = chunk[_CHUNK:]
        if chunk:
            items.append(_annotated_rich_text(chunk, is_bold))
    return items or [_rich_text("")]


def _annotated_rich_text(text: str, is_bold: bool) -> dict[str, Any]:
    value = _rich_text(_strip_inline_md(text))
    if is_bold:
        value["annotations"] = {"bold": True}
    return value


def _split_bold_runs(text: str) -> list[tuple[str, bool]]:
    parts: list[tuple[str, bool]] = []
    cursor = 0
    for match in re.finditer(r"\*\*(.+?)\*\*", text):
        if match.start() > cursor:
            parts.append((text[cursor : match.start()], False))
        parts.append((match.group(1), True))
        cursor = match.end()
    if cursor < len(text):
        parts.append((text[cursor:], False))
    return parts or [(text, False)]


def _strip_inline_md(text: str) -> str:
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text).strip()


def _heading_block(text: str, level: int = 2) -> dict[str, Any]:
    block_type = f"heading_{min(max(level, 1), 3)}"
    return {"object": "block", "type": block_type, block_type: {"rich_text": _rich_text_items(text)}}


def _paragraph_block(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich_text_items(text)}}


def _bulleted_block(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rich_text_items(text)}}


def _numbered_block(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "numbered_list_item", "numbered_list_item": {"rich_text": _rich_text_items(text)}}


def _chunk_text(text: str, limit: int = 1900) -> list[str]:
    paragraphs = [item.strip() for item in text.split("\n\n") if item.strip()]
    chunks: list[str] = []
    for para in paragraphs or [text]:
        while len(para) > limit:
            chunks.append(para[:limit])
            para = para[limit:]
        if para:
            chunks.append(para)
    return chunks


def markdown_to_blocks(text: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    paragraph_lines: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph_lines:
            return
        paragraph = "\n".join(paragraph_lines).strip()
        paragraph_lines.clear()
        for part in _chunk_text(paragraph):
            blocks.append(_paragraph_block(part))

    for raw_line in text.replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            level = min(len(heading.group(1)), 3)
            blocks.append(_heading_block(_strip_inline_md(heading.group(2)), level=level))
            continue
        bullet = re.match(r"^[-*]\s+(.+)$", stripped)
        if bullet:
            flush_paragraph()
            blocks.append(_bulleted_block(bullet.group(1)))
            continue
        numbered = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if numbered:
            flush_paragraph()
            blocks.append(_numbered_block(numbered.group(1)))
            continue
        paragraph_lines.append(stripped)

    flush_paragraph()
    return blocks


# ── 属性映射 ──────────────────────────────────────────────────────────────────

def _find_title_property(properties: dict[str, Any]) -> str | None:
    for name, value in properties.items():
        if value.get("type") == "title":
            return name
    return None


def _find_named_property(properties: dict[str, Any], candidates: tuple[str, ...]) -> str | None:
    lower_lookup = {name.lower(): name for name in properties}
    for candidate in candidates:
        found = lower_lookup.get(candidate.lower())
        if found:
            return found
    return None


def _find_source_id_property(properties: dict[str, Any]) -> str | None:
    candidates = (
        "source_id", "Source ID", "Source", "SourceId", "Note ID", "Get ID",
        "Get Note ID", "Get 笔记 ID", "Get笔记ID", "来源ID", "笔记ID",
    )
    found = _find_named_property(properties, candidates)
    if found and properties.get(found, {}).get("type") == "rich_text":
        return found
    for name, schema in properties.items():
        if schema.get("type") != "rich_text":
            continue
        compact = name.replace(" ", "").replace("_", "").lower()
        if "sourceid" in compact or "noteid" in compact or "来源id" in compact or "笔记id" in compact:
            return name
    return None


def _property_value(schema: dict[str, Any], value: Any) -> dict[str, Any]:
    prop_type = schema.get("type")
    if prop_type == "title":
        return {"title": [_rich_text(str(value))]}
    if prop_type == "rich_text":
        return {"rich_text": [_rich_text(str(value))]}
    if prop_type == "url":
        return {"url": str(value)}
    if prop_type == "date":
        return {"date": {"start": str(value)[:10]}}
    if prop_type == "status":
        return {"status": {"name": str(value)}}
    if prop_type == "select":
        return {"select": {"name": str(value)}}
    if prop_type == "multi_select":
        values = value if isinstance(value, list) else [value]
        return {"multi_select": [{"name": str(item)} for item in values]}
    if prop_type == "number":
        return {"number": float(value)}
    if prop_type == "checkbox":
        return {"checkbox": bool(value)}
    return {"rich_text": [_rich_text(str(value))]}


def _resolve_mapping(schema: dict[str, Any], database_id: str, targets_config: dict[str, Any]) -> dict[str, str | None]:
    overrides = targets_config.get("database_overrides") or {}
    db_override = overrides.get(database_id) or {}
    mapping = dict(db_override.get("properties") or db_override)

    mapping.setdefault("title", _find_title_property(schema))
    mapping.setdefault("source_id", _find_source_id_property(schema))
    mapping.setdefault("status", _find_named_property(schema, ("Status",)))
    mapping.setdefault("tags", _find_named_property(schema, ("Tags", "Topics")))
    mapping.setdefault("source_created_at", _find_named_property(schema, ("Created At", "Source Created At", "Date")))
    mapping.setdefault("source_url", _find_named_property(schema, ("Source URL",)))
    return mapping


def _build_properties(
    schema: dict[str, Any],
    mapping: dict[str, str | None],
    note: GetNote,
    status: str | None,
) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    title_prop = mapping.get("title")
    if title_prop and title_prop in schema:
        properties[title_prop] = _property_value(schema[title_prop], note.title or note.note_id)
    source_id_prop = mapping.get("source_id")
    if source_id_prop and source_id_prop in schema:
        properties[source_id_prop] = _property_value(schema[source_id_prop], note.note_id)
    status_prop = mapping.get("status")
    if status_prop and status and status_prop in schema:
        properties[status_prop] = _property_value(schema[status_prop], status)
    tags_prop = mapping.get("tags")
    if tags_prop and note.tags and tags_prop in schema:
        properties[tags_prop] = _property_value(schema[tags_prop], note.tags)
    date_prop = mapping.get("source_created_at")
    if date_prop and note.created_at and date_prop in schema:
        properties[date_prop] = _property_value(schema[date_prop], note.created_at)
    url_prop = mapping.get("source_url")
    if url_prop and note.web_page_url() and url_prop in schema:
        properties[url_prop] = _property_value(schema[url_prop], note.web_page_url())
    return properties


# ── Notion 写入 ───────────────────────────────────────────────────────────────

def _create_page(
    client: Client,
    database_id: str,
    data_source_id: str | None,
    properties: dict[str, Any],
    children: list[dict[str, Any]],
) -> str:
    if data_source_id:
        parent: dict[str, Any] = {"type": "data_source_id", "data_source_id": data_source_id}
    else:
        parent = {"type": "database_id", "database_id": database_id}
    page = client.pages.create(
        parent=parent,
        properties=properties,
        children=children[:MAX_BLOCKS_PER_REQUEST],
    )
    page_id = str(page["id"])
    for i in range(MAX_BLOCKS_PER_REQUEST, len(children), MAX_BLOCKS_PER_REQUEST):
        client.blocks.children.append(
            block_id=page_id,
            children=children[i : i + MAX_BLOCKS_PER_REQUEST],
        )
    return page_id


class NotionImportWriter:
    def __init__(self, api_key: str) -> None:
        self._client = Client(auth=api_key)

    def list_databases(self) -> list[dict[str, str]]:
        """列出 Integration 有权访问的所有数据库。"""
        resp = self._client.search(filter={"property": "object", "value": "data_source"})
        result = []
        for item in resp.get("results") or []:
            if item.get("object") not in ("database", "data_source"):
                continue
            title = "".join(p.get("plain_text", "") for p in item.get("title") or [])
            result.append({"id": str(item["id"]), "title": title})
        return result

    def has_source_id(self, database_id: str, prop_name: str, source_id: str, data_source_id: str | None = None) -> bool:
        flt = {"property": prop_name, "rich_text": {"equals": source_id}}
        if data_source_id:
            resp = self._client.data_sources.query(data_source_id, filter=flt, page_size=1)
        else:
            resp = self._client.request(
                path=f"databases/{database_id}/query",
                method="POST",
                body={"filter": flt, "page_size": 1},
            )
        return bool(resp.get("results"))

    def write_ai_link(self, note: GetNote, targets_config: dict[str, Any]) -> str:
        """写入 AI 链接笔记，返回 page_id。已存在则返回空字符串。"""
        db_id = (targets_config.get("ai_link") or {}).get("database_id")
        if not db_id:
            raise RuntimeError("config/targets.json 中未配置 ai_link.database_id")
        status = str((targets_config.get("ai_link") or {}).get("status") or "Inbox")

        schema, ds_id, _ = resolve_notion_database_context(self._client, db_id)
        mapping = _resolve_mapping(schema, db_id, targets_config)

        source_id_prop = mapping.get("source_id")
        if not source_id_prop:
            import sys
            print(f"[info] AI链接数据库无 source_id 字段，依赖水位文件去重（db={db_id}）", file=sys.stderr)
        elif self.has_source_id(db_id, source_id_prop, note.note_id, ds_id):
            return ""

        properties = _build_properties(schema, mapping, note, status=status)
        body = note.content or note.web_page_content()
        children = markdown_to_blocks(body) or [_paragraph_block("(empty)")]

        return _create_page(self._client, db_id, ds_id, properties, children)

    def write_audio_card(
        self,
        note: GetNote,
        database_id: str,
        cleaned_content: str,
        cleaning_log: str,
        targets_config: dict[str, Any],
    ) -> str:
        """写入录音卡笔记，返回 page_id。已存在则返回空字符串。"""
        schema, ds_id, _ = resolve_notion_database_context(self._client, database_id)
        mapping = _resolve_mapping(schema, database_id, targets_config)

        source_id_prop = mapping.get("source_id")
        if not source_id_prop:
            import sys
            print(f"[info] 录音卡目标数据库无 source_id 字段，依赖水位文件去重（db={database_id}）", file=sys.stderr)
        elif self.has_source_id(database_id, source_id_prop, note.note_id, ds_id):
            return ""

        properties = _build_properties(schema, mapping, note, status=None)
        children = _build_audio_card_blocks(cleaned_content, cleaning_log, note)

        return _create_page(self._client, database_id, ds_id, properties, children)


def _build_audio_card_blocks(
    cleaned_content: str,
    cleaning_log: str,
    note: GetNote,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    blocks.extend(markdown_to_blocks(cleaned_content))
    if cleaning_log:
        blocks.append(_heading_block("替换记录", level=2))
        blocks.extend(markdown_to_blocks(cleaning_log))
    blocks.append(_heading_block("导入信息", level=2))
    import_info = f"Get笔记ID: {note.note_id}\n来源创建时间: {note.created_at}"
    blocks.extend(markdown_to_blocks(import_info))
    return blocks
