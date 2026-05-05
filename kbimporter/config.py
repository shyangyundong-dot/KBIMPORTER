from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def load_env(path: str | Path = ".env") -> None:
    load_dotenv(dotenv_path=Path(path), override=False)


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v


def _env_bool(name: str, default: bool) -> bool:
    raw = (_env(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def format_notion_id(raw: str) -> str:
    """32位hex补全为UUID连字符格式。"""
    s = raw.strip().replace("-", "")
    if len(s) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", s):
        return f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}"
    return raw.strip()


@dataclass(frozen=True)
class BijiConfig:
    api_key: str
    client_id: str


@dataclass(frozen=True)
class RecipeNotionConfig:
    api_key: str
    database_id: str
    topic_id: str
    topic_numeric_id: str | None
    prop_douyin_url: str
    prop_external_id: str
    prop_biji_share: str | None
    prop_link_source: str | None
    prop_body: str | None
    sync_page_body: bool


def load_biji_config() -> BijiConfig:
    key = (_env("BIJI_API_KEY") or "").strip()
    cid = (_env("BIJI_CLIENT_ID") or "").strip()
    if not key or not cid:
        raise RuntimeError("请设置 BIJI_API_KEY 与 BIJI_CLIENT_ID（来自 Get 笔记开放平台）")
    return BijiConfig(api_key=key, client_id=cid)


def load_notion_api_key() -> str:
    key = (_env("NOTION_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("请设置 NOTION_API_KEY")
    return key


def load_recipe_notion_config() -> RecipeNotionConfig:
    api_key = load_notion_api_key()
    database_id = (_env("NOTION_DATABASE_ID_RECIPE") or "").strip()
    if not database_id:
        raise RuntimeError("请设置 NOTION_DATABASE_ID_RECIPE（菜谱 Notion 数据库 ID）")
    topic_id = (_env("BIJI_TOPIC_ID_RECIPE") or "").strip()
    if not topic_id:
        raise RuntimeError("请设置 BIJI_TOPIC_ID_RECIPE（菜谱知识库 topic 别名，如 pn5wNaO0）")
    topic_numeric_id = (_env("BIJI_TOPIC_NUMERIC_ID_RECIPE") or "").strip() or None
    return RecipeNotionConfig(
        api_key=api_key,
        database_id=format_notion_id(database_id),
        topic_id=topic_id,
        topic_numeric_id=topic_numeric_id,
        prop_douyin_url=_env("NOTION_PROP_DOUYIN_URL", "视频链接") or "视频链接",
        prop_external_id=_env("NOTION_PROP_EXTERNAL_ID", "Get笔记ID") or "Get笔记ID",
        prop_biji_share=(_env("NOTION_PROP_BIJI_SHARE") or "").strip() or None,
        prop_link_source=(_env("NOTION_PROP_LINK_SOURCE") or "").strip() or None,
        prop_body=(_env("NOTION_PROP_BODY") or "").strip() or None,
        sync_page_body=_env_bool("NOTION_SYNC_PAGE_BODY", True),
    )
