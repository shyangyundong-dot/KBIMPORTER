from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from kbimporter.biji_api import (
    debug_note_list_first_page,
    fetch_note_detail,
    harvest_links_from_note_dict,
    iter_import_notes,
    iter_recipe_notes,
    load_fixture_notes,
    max_biji_time_string,
    note_activity_raw_for_state,
)
from kbimporter.cleaner import CorrectionCleaner
from kbimporter.config import load_biji_config, load_env, load_notion_api_key, load_recipe_notion_config
from kbimporter.correction_dict import CorrectionDictionary
from kbimporter.douyin_resolver import resolve_douyin_url
from kbimporter.import_writer import NotionImportWriter
from kbimporter.models import GetNote
from kbimporter.recipe_writer import RecipeNotionWriter
from kbimporter.router import NoteRouter, RouteError
from kbimporter.watermark import WatermarkStore


ROOT = Path.cwd()
CONFIG_DIR = ROOT / "config"
STATE_DIR = ROOT / "state"
OUTPUT_FAILED_DIR = ROOT / "output" / "failed"

_DEFAULT_RECIPE_STATE_FILE = STATE_DIR / "recipe_state.json"
_DEFAULT_AI_LINK_WATERMARK_FILE = STATE_DIR / "ai_link_watermark.json"
_DEFAULT_AUDIO_CARD_WATERMARK_FILE = STATE_DIR / "audio_card_watermark.json"


# ── 统一同步（菜谱 + AI链接） ────────────────────────────────────────────────────

def cmd_sync(args: argparse.Namespace) -> int:
    load_env()
    bcfg = load_biji_config()
    sess = requests.Session()

    # ── 菜谱同步 ──────────────────────────────────────────────────────────────
    print("── 菜谱同步 ──────────────────────────────────────", file=sys.stderr)
    rcfg = load_recipe_notion_config()
    topic = rcfg.topic_id
    state_file = Path(args.state_file) if args.state_file else _DEFAULT_RECIPE_STATE_FILE

    start_since_id, start_since_updated_at = _load_recipe_state(state_file, topic)

    if not args.no_state and str(start_since_id) not in ("0", "") and start_since_updated_at is None:
        try:
            seed = fetch_note_detail(sess, api_key=bcfg.api_key, client_id=bcfg.client_id,
                                     note_id=str(start_since_id))
            start_since_updated_at = note_activity_raw_for_state(seed)
        except Exception as ex:
            print(f"[warn] 无法拉取水位笔记详情（仍用 ID 水位）：{ex}", file=sys.stderr)

    if start_since_id and str(start_since_id) != "0":
        print(f"增量同步：笔记 ID > {start_since_id}…", file=sys.stderr, flush=True)
    else:
        print("全量同步菜谱知识库…", file=sys.stderr, flush=True)

    recipe_writer: RecipeNotionWriter | None = None
    if not args.dry_run:
        recipe_writer = RecipeNotionWriter(rcfg)

    n_recipe = 0
    max_note_id = str(start_since_id)
    max_updated_at: str | None = start_since_updated_at

    for rec, merged in iter_recipe_notes(
        sess,
        api_key=bcfg.api_key,
        client_id=bcfg.client_id,
        topic_id=topic,
        topic_numeric_id=rcfg.topic_numeric_id,
        fetch_detail=not args.list_only,
        start_since_id=start_since_id,
        start_since_updated_at=start_since_updated_at,
    ):
        n_recipe += 1
        try:
            if int(rec.external_id) > int(max_note_id or 0):
                max_note_id = rec.external_id
        except ValueError:
            pass
        max_updated_at = max_biji_time_string(max_updated_at, rec.biji_updated_at)

        resolved = resolve_douyin_url(
            property_video_url=rec.video_url_from_property,
            body=rec.body,
            session=sess,
            fetch_share=not args.no_fetch,
        )

        if args.dry_run:
            print(json.dumps({
                "id": rec.external_id, "title": rec.title,
                "douyin_url": resolved.douyin_url,
                "biji_share_url": resolved.biji_share_url,
                "source": resolved.source.value,
            }, ensure_ascii=False), flush=True)
            continue

        assert recipe_writer is not None
        page_id = recipe_writer.upsert(rec, resolved)
        print(f"{rec.external_id}\t{page_id}", flush=True)

    if n_recipe == 0:
        if str(start_since_id) != "0":
            print("菜谱：没有新笔记（已是最新）。", file=sys.stderr)
        else:
            print("菜谱：未拉到任何笔记：请检查 BIJI_API_KEY / BIJI_CLIENT_ID / BIJI_TOPIC_ID_RECIPE",
                  file=sys.stderr)

    if not args.dry_run and not args.no_state and max_note_id and max_note_id != "0":
        _save_recipe_state(state_file, topic, max_note_id, max_updated_at)
        print(f"菜谱状态已保存（last_note_id={max_note_id}）→ {state_file}", file=sys.stderr)

    # ── AI链接同步 ────────────────────────────────────────────────────────────
    print("\n── AI链接同步 ──────────────────────────────────────", file=sys.stderr)
    targets_config = _load_targets_config()
    watermark = WatermarkStore(_DEFAULT_AI_LINK_WATERMARK_FILE)
    _recipe_topics = [rcfg.topic_id] + ([rcfg.topic_numeric_id] if rcfg.topic_numeric_id else [])
    router = NoteRouter(CONFIG_DIR / "routes.json", exclude_topics=_recipe_topics)

    ai_link_writer: NotionImportWriter | None = None
    if not args.dry_run:
        try:
            api_key = load_notion_api_key()
            ai_link_writer = NotionImportWriter(api_key)
        except RuntimeError as e:
            print(f"[warn] {e}，AI链接笔记将无法写入 Notion。", file=sys.stderr)

    if args.fixture:
        notes_iter = load_fixture_notes(args.fixture)
    else:
        notes_iter = iter_import_notes(
            sess,
            api_key=bcfg.api_key,
            client_id=bcfg.client_id,
            topic_id=args.topic or "",
            created_after=args.created_after,
            watermark=watermark,
        )

    n_ai = 0
    unmatched_ai: list[GetNote] = []
    for note in notes_iter:
        try:
            route_result = router.route(note)
        except RouteError:
            unmatched_ai.append(note)
            watermark.advance(note, "skipped_duplicate")
            continue

        if route_result.is_skip or route_result.route != "ai-link":
            # 录音卡和全局排除项在本水位里标记为已见，留给 audio 命令处理
            watermark.advance(note, "skipped_duplicate")
            continue

        n_ai += 1
        if args.dry_run:
            print(json.dumps({"note_id": note.note_id, "title": note.title,
                               "route": "ai-link"}, ensure_ascii=False), flush=True)
            continue

        _process_ai_link(note, ai_link_writer, targets_config, watermark)

    if n_ai == 0:
        print("AI链接：没有待处理的笔记。", file=sys.stderr)
    else:
        print(f"\nAI链接：共处理 {n_ai} 条。", file=sys.stderr)

    if unmatched_ai:
        print(f"\n[警告] {len(unmatched_ai)} 条笔记无法匹配路由规则（已跳过并推进水位）：", file=sys.stderr)
        for n in unmatched_ai:
            print(f"  - {n.note_id}「{n.title}」标签：{n.tags}", file=sys.stderr)

    return 0


def _load_recipe_state(state_file: Path, topic: str) -> tuple[str, str | None]:
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        ent = state.get(topic) or {}
        nid = str(ent.get("last_note_id") or "0")
        lu = ent.get("last_updated_at")
        if isinstance(lu, str) and lu.strip():
            return nid, lu.strip()
        return nid, None
    except (FileNotFoundError, json.JSONDecodeError):
        return "0", None


def _save_recipe_state(state_file: Path, topic: str, last_note_id: str, last_updated_at: str | None) -> None:
    state: dict = {}
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    state.setdefault(topic, {})
    state[topic]["last_note_id"] = last_note_id
    if last_updated_at:
        state[topic]["last_updated_at"] = last_updated_at
    state[topic]["synced_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 录音卡（交互式，手动触发） ────────────────────────────────────────────────────

def cmd_audio(args: argparse.Namespace) -> int:
    load_env()
    targets_config = _load_targets_config()
    watermark = WatermarkStore(_DEFAULT_AUDIO_CARD_WATERMARK_FILE)
    try:
        _rcfg = load_recipe_notion_config()
        _audio_exclude_topics = [_rcfg.topic_id] + ([_rcfg.topic_numeric_id] if _rcfg.topic_numeric_id else [])
    except RuntimeError:
        _audio_exclude_topics = []
    router = NoteRouter(CONFIG_DIR / "routes.json", exclude_topics=_audio_exclude_topics)
    correction_dict_path = CONFIG_DIR / "correction_dict.json"

    writer: NotionImportWriter | None = None
    available_dbs: list[dict[str, str]] = []
    try:
        api_key = load_notion_api_key()
        writer = NotionImportWriter(api_key)
        ai_link_id = (targets_config.get("ai_link") or {}).get("database_id", "")
        print("正在从 Notion 获取数据库列表…", file=sys.stderr, flush=True)
        all_dbs = writer.list_databases()
        available_dbs = [db for db in all_dbs if db["id"] != ai_link_id]
        if not available_dbs:
            print("[warn] 未找到可用的目标数据库，请检查 Notion Integration 权限。", file=sys.stderr)
    except RuntimeError as e:
        print(f"[warn] {e}，录音卡笔记将无法写入 Notion。", file=sys.stderr)

    sess = requests.Session()

    if args.fixture:
        notes_iter = load_fixture_notes(args.fixture)
    else:
        bcfg = load_biji_config()
        notes_iter = iter_import_notes(
            sess,
            api_key=bcfg.api_key,
            client_id=bcfg.client_id,
            topic_id=args.topic or "",
            created_after=args.created_after,
            watermark=watermark,
        )

    n_processed = 0
    unmatched_audio: list[GetNote] = []
    for note in notes_iter:
        try:
            route_result = router.route(note)
        except RouteError:
            unmatched_audio.append(note)
            watermark.advance(note, "skipped_duplicate")
            continue

        if route_result.is_skip or route_result.route != "audio-card":
            # AI链接和全局排除项在本水位里标记为已见，留给 sync 命令处理
            watermark.advance(note, "skipped_duplicate")
            continue

        n_processed += 1
        _process_audio_card(note, writer, available_dbs, targets_config, correction_dict_path, watermark)

    if n_processed == 0:
        print("没有待处理的录音卡笔记。")
    else:
        print(f"\n共处理 {n_processed} 条录音卡笔记。")

    if unmatched_audio:
        print(f"\n[警告] {len(unmatched_audio)} 条笔记无法匹配路由规则（已跳过并推进水位）：")
        for n in unmatched_audio:
            print(f"  - {n.note_id}「{n.title}」标签：{n.tags}")
    return 0


# ── 共用处理函数 ──────────────────────────────────────────────────────────────

def _process_ai_link(
    note: GetNote,
    writer: NotionImportWriter | None,
    targets_config: dict[str, Any],
    watermark: WatermarkStore,
) -> None:
    print(f"[AI链接] {note.note_id}「{note.title}」", end=" ", flush=True)
    if writer is None:
        print("跳过（未配置 NOTION_API_KEY）")
        return
    try:
        page_id = writer.write_ai_link(note, targets_config)
        if page_id == "":
            print("已跳过（source_id 重复）")
            watermark.advance(note, "skipped_duplicate")
        else:
            print(f"已写入 {page_id}")
            watermark.advance(note, "written")
    except Exception as exc:
        _save_failed(note, str(exc))
        print(f"写入失败：{exc}")


def _process_audio_card(
    note: GetNote,
    writer: NotionImportWriter | None,
    available_dbs: list[dict[str, str]],
    targets_config: dict[str, Any],
    correction_dict_path: Path,
    watermark: WatermarkStore,
) -> None:
    print(f"\n[录音卡] {note.note_id}「{note.title}」")

    # 1. 本地字典清洗
    dict_data = CorrectionDictionary(correction_dict_path).load()
    cleaner = CorrectionCleaner(dict_data)
    clean_result = cleaner.clean(note.content)

    if clean_result.replacements:
        print(f"  自动替换 {len(clean_result.replacements)} 处：" +
              "、".join(f"{r.wrong}→{r.correct}" for r in clean_result.replacements))
    if clean_result.pending_confirmations:
        print(f"  ⚠️  待确认项 {len(clean_result.pending_confirmations)} 处：" +
              "、".join(p.wrong for p in clean_result.pending_confirmations))

    # 2. 写临时文件，用 $EDITOR 打开
    tmp_path = Path(tempfile.mktemp(suffix=".md", prefix=f"kbimporter_{note.note_id}_"))
    _write_review_file(tmp_path, note, clean_result)
    editor = os.environ.get("EDITOR", "vi")
    try:
        subprocess.run([editor, str(tmp_path)], check=True)
    except KeyboardInterrupt:
        tmp_path.unlink(missing_ok=True)
        print("  已中断，跳过此笔记。")
        return

    # 3. 读回编辑后内容
    edited_content = _read_review_file(tmp_path)
    tmp_path.unlink(missing_ok=True)

    # 4. 更新纠错词表
    if edited_content != clean_result.cleaned_text:
        update = CorrectionDictionary(correction_dict_path).update_from_edit(
            clean_result.cleaned_text, edited_content
        )
        if update.learned:
            print(f"  词表已学习：{list(update.learned.keys())}")
        if update.conflicts:
            print(f"  ⚠️  词表冲突，请手动处理：{list(update.conflicts.keys())}")
            print(f"  冲突详情：{json.dumps(update.conflicts, ensure_ascii=False)}")

    # 5. 选择目标数据库
    if writer is None:
        print("  跳过写入（未配置 NOTION_API_KEY）。")
        return

    database_id = _select_audio_database(available_dbs)
    if database_id is None:
        print("  已跳过写入。")
        return

    # 6. 构建清洗记录
    cleaning_log = _format_cleaning_log(clean_result)

    # 7. 写入 Notion
    try:
        page_id = writer.write_audio_card(note, database_id, edited_content, cleaning_log, targets_config)
        if page_id == "":
            print(f"  已跳过（source_id 重复）")
            watermark.advance(note, "skipped_duplicate")
        else:
            print(f"  已写入 Notion：{page_id}")
            watermark.advance(note, "written")
    except Exception as exc:
        _save_failed(note, str(exc))
        print(f"  写入失败：{exc}")


def _write_review_file(path: Path, note: GetNote, clean_result: Any) -> None:
    from kbimporter.cleaner import CleanResult
    assert isinstance(clean_result, CleanResult)

    pending_lines = ""
    if clean_result.pending_confirmations:
        items = [f"  - {p.wrong}（建议：{p.suggested or '?'}）：{p.reason}" for p in clean_result.pending_confirmations]
        pending_lines = "⚠️ 待确认项（请在正文中手动处理）：\n" + "\n".join(items) + "\n"

    replace_lines = ""
    if clean_result.replacements:
        items = [f"  - {r.wrong} → {r.correct}" for r in clean_result.replacements]
        replace_lines = "已自动替换：\n" + "\n".join(items) + "\n"

    content = f"""\
---
note_id: "{note.note_id}"
title: "{note.title}"
created_at: "{note.created_at}"
tags: {json.dumps(note.tags, ensure_ascii=False)}
{replace_lines}{pending_lines}
---

{clean_result.cleaned_text}

<!--
=== 原始智能总结（只读参考）===
{clean_result.original_text}
-->
"""
    path.write_text(content, encoding="utf-8")


def _read_review_file(path: Path) -> str:
    """读回用户编辑后的正文（两个 --- 之间为元信息，之后到 <!-- 之前为正文）。"""
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) >= 3:
        body_and_rest = parts[2]
    else:
        body_and_rest = text
    comment_idx = body_and_rest.find("<!--")
    if comment_idx >= 0:
        body_and_rest = body_and_rest[:comment_idx]
    return body_and_rest.strip()


def _format_cleaning_log(clean_result: Any) -> str:
    lines: list[str] = []
    if clean_result.replacements:
        lines.append("自动替换：")
        for r in clean_result.replacements:
            lines.append(f"- `{r.wrong}` → `{r.correct}`")
    if clean_result.pending_confirmations:
        lines.append("\n待确认项：")
        for p in clean_result.pending_confirmations:
            lines.append(f"- `{p.wrong}`（建议：{p.suggested or '?'}）：{p.reason}")
    return "\n".join(lines)


def _select_audio_database(available_dbs: list[dict[str, str]]) -> str | None:
    if not available_dbs:
        print("  ⚠️  没有可用的目标数据库（请检查 Notion Integration 权限）。")
        return None

    print("\n  选择目标 Notion 数据库：")
    for i, db in enumerate(available_dbs, 1):
        print(f"    {i}. {db['title']}  ({db['id']})")
    print("    s. 跳过此笔记")

    while True:
        choice = input("  输入序号：").strip()
        if choice.lower() == "s":
            return None
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(available_dbs):
                return available_dbs[idx]["id"]
        except ValueError:
            pass
        print("  无效输入，请重试。")


def _save_failed(note: GetNote, error: str) -> None:
    OUTPUT_FAILED_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in note.note_id)
    path = OUTPUT_FAILED_DIR / f"{safe_id}_{timestamp}.json"
    path.write_text(
        json.dumps({"note_id": note.note_id, "title": note.title, "error": error},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_targets_config() -> dict[str, Any]:
    path = CONFIG_DIR / "targets.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# ── 调试命令 ──────────────────────────────────────────────────────────────────

def cmd_debug_biji(args: argparse.Namespace) -> int:
    load_env()
    bcfg = load_biji_config()
    sess = requests.Session()
    topic = (args.topic_id or "").strip()
    numeric_id: str | None = None

    if not topic:
        try:
            rcfg = load_recipe_notion_config()
            topic = rcfg.topic_id
            numeric_id = rcfg.topic_numeric_id
        except RuntimeError:
            pass

    raw = debug_note_list_first_page(
        sess,
        api_key=bcfg.api_key,
        client_id=bcfg.client_id,
        topic_alias=topic,
        topic_numeric_id=numeric_id,
    )
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    notes = data.get("notes") or data.get("list") or data.get("items") or []
    first_keys = sorted(str(k) for k in notes[0].keys()) if notes and isinstance(notes[0], dict) else None
    summary = {
        "request_topic_alias": topic,
        "success": raw.get("success"),
        "first_page_note_count": len(notes) if isinstance(notes, list) else None,
        "first_note_keys": first_keys,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.full:
        print(json.dumps(raw, ensure_ascii=False, indent=2))
    return 0


def cmd_schema_recipe(args: argparse.Namespace) -> int:
    load_env()
    from collections.abc import Mapping as MappingABC
    from notion_client import Client
    from kbimporter.recipe_writer import resolve_notion_database_context
    rcfg = load_recipe_notion_config()
    client = Client(auth=rcfg.api_key)
    props, data_source_id, db = resolve_notion_database_context(client, rcfg.database_id)
    if not props:
        print(json.dumps({"error": "未解析到列定义", "database_id": rcfg.database_id,
                          "data_source_id": data_source_id}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    rows = [{"name": name, "type": meta.get("type")} for name, meta in props.items()
            if isinstance(meta, MappingABC)]
    print(json.dumps({"database_id": rcfg.database_id, "data_source_id": data_source_id,
                      "properties": sorted(rows, key=lambda x: x["name"])}, ensure_ascii=False, indent=2))
    return 0


def cmd_export_links(args: argparse.Namespace) -> int:
    load_env()
    bcfg = load_biji_config()
    try:
        rcfg = load_recipe_notion_config()
        topic = args.topic_id or rcfg.topic_id
        numeric_id = rcfg.topic_numeric_id
    except RuntimeError:
        topic = args.topic_id or ""
        numeric_id = None

    sess = requests.Session()
    print("正在从 Get 笔记拉取（含详情，条目多时较久）…", file=sys.stderr, flush=True)
    topic_public = f"https://biji.com/topic/{topic}"
    out = open(args.output, "w", encoding="utf-8") if args.output else None
    stream = out if out else sys.stdout
    n = 0
    try:
        if args.format == "tsv" and args.with_header:
            stream.write("note_id\ttitle\ttopic_url\tdouyin_url\tbiji_share_url\tbiji_urls\n")
        from kbimporter.biji_api import iter_raw_notes
        for merged in iter_raw_notes(
            sess,
            api_key=bcfg.api_key,
            client_id=bcfg.client_id,
            topic_id=topic,
            topic_numeric_id=numeric_id,
            fetch_detail=not args.list_only,
        ):
            from kbimporter.biji_api import _note_id, _note_title
            nid = _note_id(merged) or ""
            title = _note_title(merged) or "（无标题）"
            n += 1
            douyin, biji_share, biji_urls = harvest_links_from_note_dict(merged)
            row = {"note_id": nid, "title": title, "topic_url": topic_public,
                   "douyin_url": douyin, "biji_share_url": biji_share, "biji_urls": biji_urls}
            if args.format == "jsonl":
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            else:
                stream.write("\t".join([nid, _tsv(title), topic_public,
                                        douyin or "", biji_share or "", ";".join(biji_urls)]) + "\n")
            stream.flush()
    finally:
        if out:
            out.close()
    print(f"共导出 {n} 条；知识库入口：{topic_public}", file=sys.stderr)
    return 0 if n > 0 else 2


def _tsv(s: str) -> str:
    return (s or "").replace("\t", " ").replace("\n", " ").replace("\r", " ")


# ── CLI 入口 ──────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="KBImporter：Get 笔记 → Notion 导入工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    # sync
    ps = sub.add_parser("sync", help="同步菜谱知识库 + AI链接笔记到 Notion（可调度）")
    ps.add_argument("--dry-run", action="store_true", help="不写 Notion，只打印会处理的笔记")
    ps.add_argument("--no-fetch", action="store_true", help="不抓取 biji 分享页解析抖音链接")
    ps.add_argument("--list-only", action="store_true", help="菜谱部分只调列表接口，不调详情接口")
    ps.add_argument("--since-id", default=None, metavar="NOTE_ID", help="手动指定菜谱增量起点 ID")
    ps.add_argument("--state-file", default=None, metavar="PATH",
                    help=f"菜谱状态文件路径，默认 {_DEFAULT_RECIPE_STATE_FILE}")
    ps.add_argument("--no-state", action="store_true", help="忽略并不更新菜谱状态文件（全量同步）")
    ps.add_argument("--created-after", default=None, metavar="DATETIME",
                    help="AI链接：只处理此时间之后创建的笔记（格式：2026-05-01 或 2026-05-01 10:00:00）")
    ps.add_argument("--topic", default=None, metavar="TOPIC_ID",
                    help="AI链接：按知识库 topic 过滤（可选）")
    ps.add_argument("--fixture", default=None, metavar="FILE",
                    help="AI链接：从本地 fixture 文件读取笔记（用于测试）")
    ps.set_defaults(func=cmd_sync)

    # audio
    pa = sub.add_parser("audio", help="处理录音卡笔记（交互式，手动触发）")
    pa.add_argument("--created-after", default=None, metavar="DATETIME",
                    help="只处理此时间之后创建的笔记（格式：2026-05-01 或 2026-05-01 10:00:00）")
    pa.add_argument("--topic", default=None, metavar="TOPIC_ID", help="按知识库 topic 过滤（可选）")
    pa.add_argument("--fixture", default=None, metavar="FILE",
                    help="从本地 fixture 文件读取笔记（用于测试）")
    pa.set_defaults(func=cmd_audio)

    # debug-biji
    pd = sub.add_parser("debug-biji", help="打印 Get 笔记列表第一页摘要，确认 topic 过滤是否生效")
    pd.add_argument("--topic-id", default=None)
    pd.add_argument("--full", action="store_true", help="同时打印完整 JSON")
    pd.set_defaults(func=cmd_debug_biji)

    # schema-recipe
    pz = sub.add_parser("schema-recipe", help="打印菜谱 Notion 数据库的列名与类型")
    pz.set_defaults(func=cmd_schema_recipe)

    # export-links
    pe = sub.add_parser("export-links", help="导出菜谱知识库笔记的 note_id、标题及链接")
    pe.add_argument("--topic-id", default=None)
    pe.add_argument("-o", "--output", default=None, metavar="PATH")
    pe.add_argument("--format", choices=("jsonl", "tsv"), default="jsonl")
    pe.add_argument("--with-header", action="store_true")
    pe.add_argument("--list-only", action="store_true")
    pe.set_defaults(func=cmd_export_links)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
