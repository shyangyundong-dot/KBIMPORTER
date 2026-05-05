from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

import requests
from bs4 import BeautifulSoup


BIJI_SHARE_RE = re.compile(
    r"https?://biji\.com/note/share_note/[A-Za-z0-9_-]+/?(?:\?[^\s]*)?(?:#[^\s]*)?",
    re.IGNORECASE,
)
DOUYIN_SHORT_RE = re.compile(
    r"https?://(?:www\.)?v\.douyin\.com/[A-Za-z0-9_-]+/?",
    re.IGNORECASE,
)
DOUYIN_VIDEO_PAGE_RE = re.compile(
    r"https?://(?:www\.)?douyin\.com/video/\d+/?(?:\?[^\s]*)?",
    re.IGNORECASE,
)


class LinkSource(str, Enum):
    PROPERTY = "属性"
    BODY_SHARE_PAGE = "分享页解析"
    BODY_DIRECT = "正文直链"
    MISSING = "未解析"


@dataclass(frozen=True)
class ResolveResult:
    douyin_url: str | None
    biji_share_url: str | None
    source: LinkSource


def _scan_douyin_fragment(t: str) -> str | None:
    if not t:
        return None
    m = DOUYIN_SHORT_RE.search(t)
    if m:
        return m.group(0).rstrip("/").split("?")[0].rstrip("/")
    m = DOUYIN_VIDEO_PAGE_RE.search(t)
    if m:
        return m.group(0).rstrip("/").split("?")[0].rstrip("/")
    return None


def find_first_douyin_in_text(text: str | None) -> str | None:
    if not text or not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    hit = _scan_douyin_fragment(s)
    if hit:
        return hit
    for m in re.finditer(r"\]\(\s*(https?://[^)\s]+)\s*\)", s):
        hit = _scan_douyin_fragment(m.group(1))
        if hit:
            return hit
    for m in re.finditer(r'''(?is)href\s*=\s*(["'])(https?://(?:(?!\1).)+)\1''', s):
        hit = _scan_douyin_fragment(m.group(2))
        if hit:
            return hit
    for m in re.finditer(r'(?is)href\s*=\s*(https?://[^\s<>"\']+)', s):
        hit = _scan_douyin_fragment(m.group(1))
        if hit:
            return hit
    return None


def _first_biji_share(body: str) -> str | None:
    m = BIJI_SHARE_RE.search(body or "")
    return m.group(0) if m else None


def _douyin_from_note_link_line(html: str) -> str | None:
    text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    for line in text.splitlines():
        if "笔记链接地址" not in line:
            continue
        found = find_first_douyin_in_text(line)
        if found:
            return found
    return None


def _douyin_from_top_anchors(soup: BeautifulSoup) -> str | None:
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        found = find_first_douyin_in_text(href)
        if found:
            return found
    return None


def fetch_douyin_from_biji_share(
    share_url: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 20.0,
) -> str | None:
    sess = session or requests.Session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    r = sess.get(share_url, headers=headers, timeout=timeout)
    r.raise_for_status()
    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    from_line = _douyin_from_note_link_line(html)
    if from_line:
        return from_line
    from_anchor = _douyin_from_top_anchors(soup)
    if from_anchor:
        return from_anchor
    return find_first_douyin_in_text(html)


def resolve_douyin_url(
    *,
    property_video_url: str | None,
    body: str,
    session: requests.Session | None = None,
    fetch_share: bool = True,
) -> ResolveResult:
    if property_video_url:
        n = find_first_douyin_in_text(property_video_url)
        if n:
            return ResolveResult(douyin_url=n, biji_share_url=None, source=LinkSource.PROPERTY)
    share = _first_biji_share(body)
    if share and fetch_share:
        try:
            d = fetch_douyin_from_biji_share(share, session=session)
            if d:
                return ResolveResult(douyin_url=d, biji_share_url=share, source=LinkSource.BODY_SHARE_PAGE)
        except requests.RequestException:
            pass
    if share and not fetch_share:
        return ResolveResult(douyin_url=None, biji_share_url=share, source=LinkSource.MISSING)
    direct = find_first_douyin_in_text(body or "")
    if direct:
        return ResolveResult(douyin_url=direct, biji_share_url=share, source=LinkSource.BODY_DIRECT)
    return ResolveResult(douyin_url=None, biji_share_url=share, source=LinkSource.MISSING)
