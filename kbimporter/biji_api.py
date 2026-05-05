from __future__ import annotations

import json
import os
import random
import re
import time
from datetime import datetime
from typing import Any, Iterator

import requests

from kbimporter.douyin_resolver import BIJI_SHARE_RE, find_first_douyin_in_text
from kbimporter.models import GetNote, NoteRecord, parse_datetime


OPENAPI_BASE = "https://openapi.biji.com/open/api/v1"


# ── 时间工具 ────────────────────────────────────────────────────────────────

def parse_biji_datetime(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def note_activity_datetime(note: dict[str, Any]) -> datetime | None:
    c = parse_biji_datetime(note.get("created_at"))
    u = parse_biji_datetime(note.get("updated_at"))
    if c and u:
        return max(c, u)
    return u or c


def note_activity_raw_for_state(note: dict[str, Any]) -> str | None:
    for k in ("updated_at", "created_at"):
        v = note.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def max_biji_time_string(a: str | None, b: str | None) -> str | None:
    da, db = parse_biji_datetime(a), parse_biji_datetime(b)
    if da is None:
        return b
    if db is None:
        return a
    return a if da >= db else b


# ── Topic 过滤 ───────────────────────────────────────────────────────────────

def _topic_filter_targets(topic_alias: str, topic_numeric_id: str | None) -> set[str]:
    t: set[str] = set()
    a = (topic_alias or "").strip().lower()
    if a:
        t.add(a)
    n = (topic_numeric_id or "").strip().lower() if topic_numeric_id else ""
    if n:
        t.add(n)
    return t


def _topics_field_matches(topics: Any, targets_lc: set[str]) -> bool:
    if not targets_lc:
        return True

    def str_hits(s: str) -> bool:
        sl = s.strip().lower()
        if not sl:
            return False
        if sl in targets_lc:
            return True
        return any(tok and tok in sl for tok in targets_lc)

    def walk(x: Any) -> bool:
        if isinstance(x, str):
            return str_hits(x)
        if isinstance(x, dict):
            for k in ("alias", "topic_alias", "slug", "short_id", "id", "topic_id", "topicId", "notebook_id"):
                if k in x and walk(x[k]):
                    return True
            for v in x.values():
                if walk(v):
                    return True
            return False
        if isinstance(x, (list, tuple)):
            return any(walk(i) for i in x)
        return False

    if topics is None:
        return False
    return walk(topics)


def _note_matches_topic_filter(
    note: dict[str, Any],
    *,
    topic_alias: str,
    topic_numeric_id: str | None,
) -> bool:
    if (os.environ.get("BIJI_SKIP_TOPIC_FILTER") or "").strip().lower() in ("1", "true", "yes"):
        return True
    targets = _topic_filter_targets(topic_alias, topic_numeric_id)
    if not targets:
        return True
    return _topics_field_matches(note.get("topics"), targets)


# ── HTTP 工具 ─────────────────────────────────────────────────────────────────

def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _sleep_for_429(attempt: int, resp: requests.Response) -> None:
    ra = (resp.headers.get("Retry-After") or "").strip()
    if ra.isdigit():
        delay = min(120.0, float(ra))
    else:
        delay = min(60.0, 1.0 * (2**attempt) + random.random())
    time.sleep(delay)


def _get_with_retry(
    session: requests.Session,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
    max_retries: int = 12,
) -> requests.Response:
    last: requests.Response | None = None
    for attempt in range(max_retries):
        last = session.get(url, params=params, headers=headers, timeout=timeout)
        if last.status_code != 429:
            return last
        if attempt < max_retries - 1:
            _sleep_for_429(attempt, last)
    assert last is not None
    last.raise_for_status()
    return last


def _headers(api_key: str, client_id: str) -> dict[str, str]:
    return {
        "Authorization": api_key.strip(),
        "X-Client-ID": client_id.strip(),
    }


def _note_list_query_params(
    since: str | int,
    *,
    topic_alias: str,
    topic_numeric_id: str | None,
) -> dict[str, Any]:
    p: dict[str, Any] = {"since_id": since}
    alias = (topic_alias or "").strip()
    num = (topic_numeric_id or "").strip() if topic_numeric_id else ""
    if not alias and not num:
        return p
    if num:
        p["topic_id"] = num
        if alias:
            p["topic_alias"] = alias
    else:
        p["topic_id"] = alias
        p["topic_alias"] = alias
    return p


def _unwrap(resp: requests.Response) -> dict[str, Any]:
    resp.raise_for_status()
    payload: dict[str, Any] = resp.json()
    if not payload.get("success"):
        err = payload.get("error") or {}
        raise RuntimeError(
            f"Get 笔记 OpenAPI 错误: {err.get('code')} {err.get('message')} "
            f"(request_id={payload.get('request_id')})"
        )
    data = payload.get("data")
    if data is None:
        return {}
    if not isinstance(data, dict):
        return {"_raw": data}
    return data


# ── 笔记字段提取 ──────────────────────────────────────────────────────────────

def _note_id(note: dict[str, Any]) -> str | None:
    for k in ("id", "note_id", "noteId", "noteID"):
        v = note.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _note_title(note: dict[str, Any]) -> str:
    for k in ("title", "name", "note_title"):
        v = note.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _note_plain_body(note: dict[str, Any]) -> str:
    parts: list[str] = []

    def append_field(val: Any) -> None:
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
        elif isinstance(val, (dict, list)):
            parts.append(json.dumps(val, ensure_ascii=False))

    for k in ("content", "markdown", "text", "body", "summary", "abstract"):
        append_field(note.get(k))
    append_field(note.get("ref_content"))
    append_field(note.get("json_content"))
    return "\n\n".join(parts)


def _video_url_from_note(note: dict[str, Any]) -> str | None:
    for k in ("video_url", "douyin_url", "origin_url", "source_url", "url"):
        v = note.get(k)
        if isinstance(v, str) and v.strip():
            d = find_first_douyin_in_text(v)
            if d:
                return d
    web_page = note.get("web_page")
    if isinstance(web_page, dict):
        u = web_page.get("url")
        if isinstance(u, str):
            d = find_first_douyin_in_text(u)
            if d:
                return d
    atts = note.get("attachments") or note.get("attachment_list")
    if isinstance(atts, list):
        for a in atts:
            if not isinstance(a, dict):
                continue
            u = a.get("url") or a.get("link")
            if isinstance(u, str):
                d = find_first_douyin_in_text(u)
                if d:
                    return d
    return _walk_find_douyin(note)


def _walk_find_douyin(obj: Any, depth: int = 0) -> str | None:
    if depth > 18:
        return None
    if isinstance(obj, str):
        return find_first_douyin_in_text(obj)
    if isinstance(obj, dict):
        for v in obj.values():
            r = _walk_find_douyin(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for it in obj:
            r = _walk_find_douyin(it, depth + 1)
            if r:
                return r
    return None


def harvest_links_from_note_dict(note: dict[str, Any]) -> tuple[str | None, str | None, list[str]]:
    chunks: list[str] = []
    try:
        raw = json.dumps(note, ensure_ascii=False)
        chunks.append(raw.replace("\\/", "/"))
    except Exception:
        pass
    pb = _note_plain_body(note)
    if pb:
        chunks.append(pb)
    mega = "\n".join(chunks)
    if not mega.strip():
        mega = "{}"

    douyin = find_first_douyin_in_text(mega)
    share_m = BIJI_SHARE_RE.search(mega)
    biji_share = share_m.group(0).rstrip("/") if share_m else None

    seen: set[str] = set()
    out_urls: list[str] = []
    for m in re.finditer(r'https?://biji\.com[^\s"\'<>[\](){}]+', mega, flags=re.I):
        u = m.group(0).rstrip("/).,;，。）】\"'\\")
        while u.endswith("\\"):
            u = u[:-1]
        if len(u) <= len("https://biji.com/"):
            continue
        if u not in seen:
            seen.add(u)
            out_urls.append(u)
            if len(out_urls) >= 40:
                break
    for m in re.finditer(r"(?i)(/note/share_note/[A-Za-z0-9_-]+)", mega):
        full = "https://biji.com" + m.group(1)
        norm = full.rstrip("/")
        if norm not in seen:
            seen.add(norm)
            out_urls.append(norm)
            if not biji_share:
                biji_share = norm
            if len(out_urls) >= 40:
                break

    return douyin, biji_share, out_urls


# ── API 调用 ──────────────────────────────────────────────────────────────────

def fetch_note_detail(
    session: requests.Session,
    *,
    api_key: str,
    client_id: str,
    note_id: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    url = f"{OPENAPI_BASE}/resource/note/detail"
    r = _get_with_retry(
        session,
        url,
        headers=_headers(api_key, client_id),
        params={"id": note_id},
        timeout=timeout,
    )
    data = _unwrap(r)
    inner = data.get("note") or data.get("detail") or data.get("c") or data
    if isinstance(inner, dict):
        return inner
    return {}


def debug_note_list_first_page(
    session: requests.Session,
    *,
    api_key: str,
    client_id: str,
    topic_alias: str,
    topic_numeric_id: str | None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    r = _get_with_retry(
        session,
        f"{OPENAPI_BASE}/resource/note/list",
        headers=_headers(api_key, client_id),
        params=_note_list_query_params(0, topic_alias=topic_alias, topic_numeric_id=topic_numeric_id),
        timeout=timeout,
    )
    return r.json()


def iter_raw_notes(
    session: requests.Session,
    *,
    api_key: str,
    client_id: str,
    topic_id: str = "",
    topic_numeric_id: str | None = None,
    fetch_detail: bool = True,
    timeout: float = 30.0,
    start_since_id: str | int = 0,
    start_since_updated_at: str | None = None,
) -> Iterator[dict[str, Any]]:
    """分页拉取笔记，yield 每条合并后的原始 dict。

    首屏 since_id=0，分页游标不等同于水位线；水位过滤在客户端完成。
    同时维护 ID 水位与时间水位，应对 ID 不严格单调递增的情况。
    """
    page_pause = _env_float("BIJI_PAGE_PAUSE_SEC", 0.35)
    detail_pause = _env_float("BIJI_DETAIL_PAUSE_SEC", 0.12)

    watermark: int | None = None
    try:
        w = int(str(start_since_id).strip() or "0")
        if w > 0:
            watermark = w
    except ValueError:
        watermark = None

    time_wm: datetime | None = None
    su = (start_since_updated_at or "").strip()
    if su:
        time_wm = parse_biji_datetime(su)

    since: str | int = 0 if watermark is not None else (start_since_id or 0)
    first_page = True

    while True:
        if not first_page and page_pause > 0:
            time.sleep(page_pause)
        first_page = False

        r = _get_with_retry(
            session,
            f"{OPENAPI_BASE}/resource/note/list",
            headers=_headers(api_key, client_id),
            params=_note_list_query_params(since, topic_alias=topic_id, topic_numeric_id=topic_numeric_id),
            timeout=timeout,
        )
        data = _unwrap(r)
        notes = data.get("notes") or data.get("list") or data.get("items") or []
        if not isinstance(notes, list):
            notes = []

        has_more = bool(data.get("has_more") or data.get("hasMore"))

        for raw in notes:
            if not isinstance(raw, dict):
                continue
            nid = _note_id(raw)
            if not nid:
                continue
            try:
                nid_i = int(nid)
            except ValueError:
                nid_i = 0

            below_id = watermark is not None and nid_i <= watermark
            if below_id and time_wm is None:
                continue
            if below_id and time_wm is not None:
                act_raw = note_activity_datetime(raw)
                if act_raw is not None and act_raw <= time_wm:
                    continue

            merged = dict(raw)
            if fetch_detail:
                if detail_pause > 0:
                    time.sleep(detail_pause)
                try:
                    detail = fetch_note_detail(
                        session,
                        api_key=api_key,
                        client_id=client_id,
                        note_id=nid,
                        timeout=timeout,
                    )
                    if detail:
                        merged = {**raw, **detail}
                except Exception as e:
                    import sys
                    print(f"[warn] 笔记 {nid} 详情获取失败，使用列表数据：{e}", file=sys.stderr)

            if below_id and time_wm is not None:
                act_m = note_activity_datetime(merged)
                if act_m is None or act_m <= time_wm:
                    continue

            if not _note_matches_topic_filter(merged, topic_alias=topic_id, topic_numeric_id=topic_numeric_id):
                continue

            yield merged

        if watermark is not None and notes and time_wm is None:
            tail = notes[-1]
            if isinstance(tail, dict):
                lid = _note_id(tail)
                if lid:
                    try:
                        if int(lid) <= watermark:
                            break
                    except ValueError:
                        pass

        if not has_more or not notes:
            break

        last = notes[-1]
        if not isinstance(last, dict):
            break
        nxt = _note_id(last)
        if not nxt or str(nxt) == str(since):
            break
        since = nxt


def iter_recipe_notes(
    session: requests.Session,
    *,
    api_key: str,
    client_id: str,
    topic_id: str,
    topic_numeric_id: str | None = None,
    fetch_detail: bool = True,
    timeout: float = 30.0,
    start_since_id: str | int = 0,
    start_since_updated_at: str | None = None,
) -> Iterator[tuple[NoteRecord, dict[str, Any]]]:
    """菜谱同步专用：yield (NoteRecord, merged_raw_dict)。"""
    for merged in iter_raw_notes(
        session,
        api_key=api_key,
        client_id=client_id,
        topic_id=topic_id,
        topic_numeric_id=topic_numeric_id,
        fetch_detail=fetch_detail,
        timeout=timeout,
        start_since_id=start_since_id,
        start_since_updated_at=start_since_updated_at,
    ):
        nid = _note_id(merged) or ""
        body = _note_plain_body(merged)
        if not body:
            body = json.dumps(merged, ensure_ascii=False)[:50000]
        rec = NoteRecord(
            external_id=nid,
            title=_note_title(merged) or "（无标题）",
            body=body,
            video_url_from_property=_video_url_from_note(merged),
            biji_updated_at=note_activity_raw_for_state(merged),
        )
        yield rec, merged


def iter_import_notes(
    session: requests.Session,
    *,
    api_key: str,
    client_id: str,
    topic_id: str = "",
    topic_numeric_id: str | None = None,
    fetch_detail: bool = True,
    timeout: float = 30.0,
    created_after: str | None = None,
    watermark: Any | None = None,
) -> Iterator[GetNote]:
    """AI链接/录音卡导入专用：yield GetNote，带水位过滤。"""
    state = watermark.load() if watermark else None
    created_after_dt = parse_datetime(created_after) if created_after else None

    start_since_id = (state.last_note_id or "0") if state else "0"
    start_since_updated_at = state.last_created_at if state else None

    for merged in iter_raw_notes(
        session,
        api_key=api_key,
        client_id=client_id,
        topic_id=topic_id,
        topic_numeric_id=topic_numeric_id,
        fetch_detail=fetch_detail,
        timeout=timeout,
        start_since_id=start_since_id,
        start_since_updated_at=start_since_updated_at,
    ):
        note = GetNote.from_api_note(merged)
        if created_after_dt and note.created_datetime <= created_after_dt:
            continue
        if watermark and watermark.should_skip(note, state):
            continue
        yield note


def load_fixture_notes(fixture_path: str) -> Iterator[GetNote]:
    """从本地 fixture 文件加载笔记（用于测试）。"""
    import json
    from pathlib import Path

    payload = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    raw_notes: list[dict[str, Any]] = []
    if isinstance(payload, list):
        raw_notes = payload
    elif isinstance(payload, dict):
        data = payload.get("data") or payload
        if isinstance(data, dict):
            raw_notes = data.get("notes") or data.get("list") or []
        elif isinstance(data, list):
            raw_notes = data
    for raw in raw_notes:
        if isinstance(raw, dict):
            yield GetNote.from_api_note(raw)
