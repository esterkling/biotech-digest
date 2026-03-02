# src/news.py
from __future__ import annotations

import os
import re
import html
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import requests


# ----------------------------
# Feed list (FREE sources)
# ----------------------------
BASE_FEEDS = [
    "https://www.fiercebiotech.com/rss/xml",
    "https://www.biopharmadive.com/feeds/news/",
    # If these work for you, keep them. They often block GitHub runners.
    # "https://www.globenewswire.com/RssFeed/subjectcode/HEA",
    # "https://www.businesswire.com/rss/home/?rss=G1QFDERJXkJeGVtRX0I=",
]

GOOGLE_NEWS_QUERIES = [
    "biotech acquisition deal",
    "biotech licensing deal upfront",
    "biotech partnership deal",
    "pharma acquisition deal",
    "FDA approval biotech",
    "EMA CHMP positive opinion biotech",
    "clinical trial readout Phase 2",
    "clinical trial readout Phase 3",
    "biotech safety hold FDA",
    "biotech financing Series A",
    "biotech financing Series B",
    "biotech IPO filing S-1",
    "Nordic biotech financing",
    "Sweden biotech",
    "Denmark biotech",
    "Norway biotech",
    "Finland biotech",
    "European biotech licensing deal",
]

def _google_news_rss_url(query: str) -> str:
    q = quote_plus(f"{query} when:1d")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

FEEDS = BASE_FEEDS + [_google_news_rss_url(q) for q in GOOGLE_NEWS_QUERIES]


# ----------------------------
# Helpers
# ----------------------------
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

DEBUG = os.environ.get("DEBUG_NEWS", "").lower() in ("1", "true", "yes", "y")


def _debug(*args):
    if DEBUG:
        print("[news]", *args)


def _clean_text(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _parse_dt(dt_str: str | None) -> datetime | None:
    """
    Parse common RSS/Atom datetime strings.
    Returns timezone-aware UTC datetime, or None if parsing fails.
    """
    if not dt_str:
        return None
    dt_str = dt_str.strip()

    # RFC 2822 (RSS pubDate)
    try:
        dt = parsedate_to_datetime(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass

    # ISO 8601 (Atom updated/published)
    try:
        s = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _get_text(elem: ET.Element, tag_names: list[str]) -> str | None:
    """
    Get text for first matching tag name in elem (direct child search).
    Handles namespaces by matching tag suffix.
    """
    for child in list(elem):
        suffix = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if suffix in tag_names:
            if child.text:
                return child.text.strip()
    return None


def _unwrap_google_news(url: str, timeout_s: int = 10) -> str:
    """
    Google News RSS often returns links like:
      https://news.google.com/rss/articles/CBMi...
    In many environments this does NOT 302 redirect to the publisher.
    This function:
      1) tries redirects
      2) if still on news.google.com, parses HTML to extract the publisher URL
    """
    if "news.google.com" not in (url or ""):
        return url

    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        r = requests.get(url, headers=headers, timeout=timeout_s, allow_redirects=True)

        # If we actually ended up on a non-Google domain, great.
        final = (r.url or "").strip()
        if final and "news.google.com" not in final:
            return final

        html_text = r.text or ""

        # Common pattern: ...&url=https%3A%2F%2Fwww.biospace.com%2F...
        m = re.search(r"[?&]url=(https%3A%2F%2F[^&\"'>]+)", html_text)
        if m:
            try:
                from urllib.parse import unquote
                real = unquote(m.group(1))
                if real.startswith("https://") and "news.google.com" not in real:
                    return real
            except Exception:
                pass

        # Fallback: find the first external https:// link that isn't Google
        m2 = re.search(r'href="(https://[^"]+)"', html_text)
        if m2:
            candidate = m2.group(1)
            if "news.google.com" not in candidate and "google.com" not in candidate:
                return candidate

        # Another fallback: any https://... in page text
        m3 = re.search(r"(https://(?!news\.google\.com|www\.google\.com)[^\s\"'<>]+)", html_text)
        if m3:
            return m3.group(1)

        return url
    except Exception:
        return url


def _parse_rss_or_atom(xml_bytes: bytes) -> list[dict]:
    """
    Parses RSS2 / Atom feeds.
    Returns list of {title, link, published_dt}
    """
    root = ET.fromstring(xml_bytes)
    root_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

    items: list[dict] = []

    # ---------- RSS ----------
    if root_tag.lower() == "rss":
        channel = root.find(".//{*}channel")
        if channel is None:
            return []

        for item in channel.findall(".//{*}item"):
            title = _get_text(item, ["title"]) or ""
            link = _get_text(item, ["link"]) or ""
            pub = _get_text(item, ["pubDate", "date", "published", "updated"])
            published_dt = _parse_dt(pub)

            title = _clean_text(title)
            link = _clean_text(link)

            # ✅ unwrap Google News wrapper links
            if "news.google.com" in link:
                link = _unwrap_google_news(link)

            if title and link:
                items.append({"title": title, "link": link, "published_dt": published_dt})

        return items

    # ---------- Atom ----------
    for entry in root.findall(".//{*}entry"):
        title = _get_text(entry, ["title"]) or ""
        pub = _get_text(entry, ["published", "updated"])
        published_dt = _parse_dt(pub)

        link = ""
        for child in list(entry):
            suffix = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if suffix == "link":
                href = child.attrib.get("href", "")
                rel = child.attrib.get("rel", "")
                if href and (rel in ("", "alternate")):
                    link = href
                    break

        title = _clean_text(title)
        link = _clean_text(link)

        if "news.google.com" in link:
            link = _unwrap_google_news(link)

        if title and link:
            items.append({"title": title, "link": link, "published_dt": published_dt})

    return items


def _fetch_feed(url: str, timeout_s: int = 25) -> list[dict]:
    headers = {
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        r = requests.get(url, headers=headers, timeout=timeout_s, allow_redirects=True)
        ct = (r.headers.get("Content-Type") or "").lower()
        _debug("GET", url, "->", r.status_code, ct)

        r.raise_for_status()

        items = _parse_rss_or_atom(r.content)
        _debug("items:", len(items), "from", url)
        return items
    except Exception as e:
        _debug("Feed failed:", url, "err:", repr(e))
        return []


def _dedupe(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for it in items:
        t = (it.get("title") or "").strip().lower()
        l = (it.get("link") or "").strip()
        if not t or not l:
            continue
        key = (t, l)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


# ----------------------------
# Public API
# ----------------------------
def fetch_last_24h(limit_per_feed: int = 40) -> list[dict]:
    """
    Returns list of:
      { "title": str, "link": str, "published_dt": datetime|None }
    Keeps items even if published_dt couldn't be parsed.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    all_items: list[dict] = []

    _debug("Feeds:", len(FEEDS))
    for feed_url in FEEDS:
        feed_items = _fetch_feed(feed_url)
        if feed_items:
            all_items.extend(feed_items[:limit_per_feed])

    all_items = _dedupe(all_items)
    _debug("Total collected:", len(all_items))

    recent_items: list[dict] = []
    for it in all_items:
        published_dt = it.get("published_dt")

        # ✅ Only filter if we successfully parsed a date
        if published_dt is not None and published_dt < cutoff:
            continue

        recent_items.append(it)

    # Newest first; undated items last
    recent_items.sort(
        key=lambda x: x["published_dt"] or datetime(1970, 1, 1, tzinfo=timezone.utc),
        reverse=True,
    )

    _debug("After 24h filter:", len(recent_items))
    return recent_items
