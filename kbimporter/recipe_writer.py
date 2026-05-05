from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from notion_client import Client

from kbimporter.config import RecipeNotionConfig, format_notion_id
from kbimporter.douyin_resolver import ResolveResult
from kbimporter.models import NoteRecord


def resolve_notion_database_context(
    client: Client,
    database_id: str,
) -> tuple[dict[str, Any], str | None, dict[str, Any]]:
    """读取数据库列定义，兼容 Notion 新版 data_sources。

    返回 (properties 映射, data_source_id, database_raw)。
    """
    db = client.databases.retrieve(database_id=database_id)
    ds_id = _first_data_source_id(db.get("data_sources"))

    raw = db.get("properties")
    if isinstance(raw, Mapping) and len(raw) > 0:
        return dict(raw), ds_id, db

    if ds_id:
        ds = client.data_sources.retrieve(data_source_id=ds_id)
        inner = ds.get("properties") or {}
        if isinstance(inner, Mapping) and len(inner) > 0:
            return dict(inner), ds_id, db

    return {}, ds_id, db


def _first_data_source_id(ds_list: Any) -> str | None:
    if not isinstance(ds_list, list) or not ds_list:
        return None
    first = ds_list[0]
    if isinstance(first, str) and first.strip():
        return format_notion_id(first.strip())
    if isinstance(first, Mapping):
        rid = first.get("id") or first.get("data_source_id")
        if rid:
            return format_notion_id(str(rid))
    return None


def _title_prop(text: str) -> dict[str, Any]:
    return {"title": [{"type": "text", "text": {"content": text[:2000]}}]}


def _url_prop(url: str | None) -> dict[str, Any]:
    if not url:
        return {"url": None}
    return {"url": url}


def _rich_text_prop(text: str | None) -> dict[str, Any]:
    if not text:
        return {"rich_text": []}
    chunks: list[dict[str, Any]] = []
    rest = text
    while rest:
        piece = rest[:2000]
        rest = rest[2000:]
        chunks.append({"type": "text", "text": {"content": piece}})
    return {"rich_text": chunks}


_CHUNK = 2000


def _split_chunks(s: str, size: int) -> list[str]:
    return [s[i : i + size] for i in range(0, len(s), size)]


def _paragraph_to_rich_text(text: str) -> list[dict[str, Any]]:
    parts = re.split(r"(\*\*[^*]+\*\*)", text)
    out: list[dict[str, Any]] = []
    for p in parts:
        if not p:
            continue
        if len(p) >= 4 and p.startswith("**") and p.endswith("**"):
            inner = p[2:-2]
            for piece in _split_chunks(inner, _CHUNK):
                out.append({"type": "text", "text": {"content": piece}, "annotations": {"bold": True}})
        else:
            for piece in _split_chunks(p, _CHUNK):
                out.append({"type": "text", "text": {"content": piece}})
    return out or [{"type": "text", "text": {"content": " "}}]


def _looks_like_md_table(para: str) -> bool:
    lines = [ln for ln in para.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    return sum(1 for ln in lines if "|" in ln) >= 2


def _markdown_table_code_block(para: str) -> dict[str, Any]:
    rich: list[dict[str, Any]] = []
    rest = para
    while rest:
        piece = rest[:_CHUNK]
        rest = rest[_CHUNK:]
        rich.append({"type": "text", "text": {"content": piece}})
    if not rich:
        rich = [{"type": "text", "text": {"content": " "}}]
    return {"object": "block", "type": "code", "code": {"caption": [], "rich_text": rich, "language": "markdown"}}


def _rich_paragraph_block(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _paragraph_to_rich_text(text)}}


def _heading_block(level: int, text: str) -> dict[str, Any]:
    level = max(1, min(3, level))
    key = f"heading_{level}"
    return {"object": "block", "type": key, key: {"rich_text": _paragraph_to_rich_text(text[:_CHUNK])}}


def plain_body_to_notion_blocks(body: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    raw = body.strip()
    if not raw:
        return blocks
    for para in re.split(r"\n\s*\n+", raw):
        para = para.strip()
        if not para:
            continue
        lines = para.split("\n")
        first = lines[0]
        rest_lines = lines[1:]
        if first.startswith("# ") and not rest_lines:
            blocks.append(_heading_block(1, first[2:].strip()))
            continue
        if first.startswith("## ") and not rest_lines:
            blocks.append(_heading_block(2, first[3:].strip()))
            continue
        if first.startswith("### ") and not rest_lines:
            blocks.append(_heading_block(3, first[4:].strip()))
            continue
        if first.startswith("#### ") and not rest_lines:
            blocks.append(_heading_block(3, first[5:].strip()))
            continue
        if _looks_like_md_table(para):
            blocks.append(_markdown_table_code_block(para))
            continue
        blocks.append(_rich_paragraph_block(para))
    return blocks


def _clear_block_children(client: Client, block_id: str) -> None:
    cursor: str | None = None
    while True:
        kwargs: dict[str, Any] = {"block_id": block_id}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = client.blocks.children.list(**kwargs)
        for block in resp.get("results") or []:
            bid = block.get("id")
            if isinstance(bid, str):
                try:
                    client.blocks.delete(block_id=bid)
                except Exception:
                    pass
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")


def _append_blocks_in_batches(
    client: Client,
    page_id: str,
    children: list[dict[str, Any]],
    batch_size: int = 100,
) -> None:
    for i in range(0, len(children), batch_size):
        client.blocks.children.append(block_id=page_id, children=children[i : i + batch_size])


def replace_page_body_blocks(client: Client, page_id: str, body: str) -> None:
    blocks = plain_body_to_notion_blocks(body)
    if not blocks:
        return
    _clear_block_children(client, page_id)
    _append_blocks_in_batches(client, page_id, blocks)


def _discover_title_property(schema: Mapping[str, Any]) -> str:
    for name, meta in schema.items():
        if isinstance(meta, Mapping) and meta.get("type") == "title":
            return name
    raise RuntimeError("Notion 数据库中找不到 title 类型的主列")


def _resolve_external_id_property(schema: Mapping[str, Any], configured: str) -> str:
    configured = configured.strip()
    seen: list[str] = []
    for name in [configured, "Get笔记ID", "Get笔记 id", "笔记ID", "外部 ID", "External ID", "Biji笔记ID"]:
        if not name or name in seen:
            continue
        seen.append(name)
        meta = schema.get(name)
        if isinstance(meta, Mapping) and meta.get("type") == "rich_text":
            return name
    rich_cols = [n for n, m in schema.items() if isinstance(m, Mapping) and m.get("type") == "rich_text"]
    raise RuntimeError(
        "需要一列「文本」类型字段保存 Get 笔记 ID（用于去重更新）。"
        "请在数据库新建一列文本「Get笔记ID」，或设置 NOTION_PROP_EXTERNAL_ID 指向专用文本字段；"
        f"当前库中的「文本」列：{', '.join(rich_cols) or '（无）'}"
    )


class RecipeNotionWriter:
    def __init__(self, cfg: RecipeNotionConfig) -> None:
        self._cfg = cfg
        self._client = Client(auth=cfg.api_key)
        self._schema, self._data_source_id, _db_meta = resolve_notion_database_context(
            self._client, cfg.database_id
        )
        if not self._schema:
            raise RuntimeError(
                "无法读取数据库列定义。请确认 Integration 已连接菜谱数据库。"
            )
        self._title_prop_name = _discover_title_property(self._schema)
        self._external_id_prop = _resolve_external_id_property(self._schema, cfg.prop_external_id)

    def _prop_type(self, name: str) -> str | None:
        meta = self._schema.get(name)
        if not isinstance(meta, Mapping):
            return None
        return str(meta.get("type") or "")

    def _has_prop(self, name: str) -> bool:
        return name in self._schema

    def _find_page_id_by_external_id(self, external_id: str) -> str | None:
        pid = self._external_id_prop
        if not self._has_prop(pid) or self._prop_type(pid) != "rich_text":
            return None
        flt: dict[str, Any] = {"property": pid, "rich_text": {"equals": external_id}}
        if self._data_source_id:
            resp = self._client.data_sources.query(self._data_source_id, filter=flt, page_size=1)
        else:
            resp = self._client.request(
                path=f"databases/{self._cfg.database_id}/query",
                method="POST",
                body={"filter": flt, "page_size": 1},
            )
        results = resp.get("results") or []
        if not results:
            return None
        return str(results[0]["id"])

    def _properties_payload(self, record: NoteRecord, resolved: ResolveResult) -> dict[str, Any]:
        cfg = self._cfg
        props: dict[str, Any] = {}

        if self._has_prop(self._title_prop_name) and self._prop_type(self._title_prop_name) == "title":
            props[self._title_prop_name] = _title_prop(record.title)

        if self._has_prop(self._external_id_prop) and self._prop_type(self._external_id_prop) == "rich_text":
            props[self._external_id_prop] = _rich_text_prop(record.external_id)

        if cfg.prop_link_source and self._has_prop(cfg.prop_link_source):
            if self._prop_type(cfg.prop_link_source) == "rich_text":
                props[cfg.prop_link_source] = _rich_text_prop(resolved.source.value)

        if (
            cfg.prop_biji_share
            and self._has_prop(cfg.prop_biji_share)
            and self._prop_type(cfg.prop_biji_share) == "url"
        ):
            props[cfg.prop_biji_share] = _url_prop(resolved.biji_share_url)

        if self._has_prop(cfg.prop_douyin_url) and self._prop_type(cfg.prop_douyin_url) == "url":
            props[cfg.prop_douyin_url] = _url_prop(resolved.douyin_url)

        if cfg.prop_body and self._has_prop(cfg.prop_body) and self._prop_type(cfg.prop_body) == "rich_text":
            props[cfg.prop_body] = _rich_text_prop(record.body)

        return props

    def upsert(self, record: NoteRecord, resolved: ResolveResult) -> str:
        props = self._properties_payload(record, resolved)
        if self._title_prop_name not in props:
            raise RuntimeError("无法写入 Notion：缺少可用的 title 列")

        existing = self._find_page_id_by_external_id(record.external_id)
        if existing:
            self._client.pages.update(existing, properties=props)
            self._maybe_sync_page_body(existing, record)
            return existing

        if self._data_source_id:
            parent: dict[str, Any] = {"type": "data_source_id", "data_source_id": self._data_source_id}
        else:
            parent = {"type": "database_id", "database_id": self._cfg.database_id}
        created = self._client.pages.create(parent=parent, properties=props)
        page_id = str(created["id"])
        self._maybe_sync_page_body(page_id, record)
        return page_id

    def _maybe_sync_page_body(self, page_id: str, record: NoteRecord) -> None:
        if not self._cfg.sync_page_body:
            return
        body = (record.body or "").strip()
        if not body:
            return
        replace_page_body_blocks(self._client, page_id, record.body)
