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
    # Trade / industry
    "https://www.fiercebiotech.com/rss/xml",
    "https://www.biopharmadive.com/feeds/news/",
    # Press wires (often catch financings, deals, trial updates)
    "https://www.globenewswire.com/RssFeed/subjectcode/HEA",
    "https://www.businesswire.com/rss/home/?rss=G1QFDERJXkJeGVtRX0I=",  # Healthcare
]

GOOGLE_NEWS_QUERIES = [
    # Big-ticket items you said you don't want to miss
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
    # Europe/Nordics
    "Nordic biotech financing",
    "Sweden biotech",
    "Denmark biotech",
    "Norway biotech",
    "Finland biotech",
    "European biotech licensing deal",
]

def _google_news_rss_url(query: str) -> str:
    # when:1d attempts to limit to last day, but we also apply our own cutoff logic
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

    # Try RFC 2822 via email.utils
    try:
        dt = parsedate_to_datetime(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass

    # Try ISO-ish strings (Atom often uses 2026-03-02T06:12:00Z)
    try:
        # normalize Z
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
        t = child.tag
        suffix = t.split("}")[-1] if "}" in t else t
        if suffix in tag_names:
            if child.text:
                return child.text.strip()
    return None


def _parse_rss_or_atom(xml_bytes: bytes) -> list[dict]:
    """
    Parses RSS2 / Atom feeds.
    Returns list of {title, link, published_dt}
    """
    root = ET.fromstring(xml_bytes)

    # Figure out if RSS or Atom
    root_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

    items: list[dict] = []

    if root_tag.lower() == "rss":
        channel = root.find("./channel")
        if channel is None:
            return []

        for item in channel.findall("./item"):
            title = _get_text(item, ["title"]) or ""
            link = _get_text(item, ["link"]) or ""
            pub = _get_text(item, ["pubDate", "date", "published", "updated"])
            published_dt = _parse_dt(pub)

            title = _clean_text(title)
            link = _clean_text(link)

            if title and link:
                items.append({"title": title, "link": link, "published_dt": published_dt})

    else:
        # Atom usually: <feed><entry>...
        # entries may have <link href="..."/>
        for entry in root.findall(".//{*}entry"):
            title = _get_text(entry, ["title"]) or ""
            pub = _get_text(entry, ["published", "updated"])
            published_dt = _parse_dt(pub)

            link = ""
            # Atom link is attribute href
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

            if title and link:
                items.append({"title": title, "link": link, "published_dt": published_dt})

    return items


def _fetch_feed(url: str, timeout_s: int = 20) -> list[dict]:
    headers = {
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        r = requests.get(url, headers=headers, timeout=timeout_s, allow_redirects=True)
        ct = (r.headers.get("Content-Type") or "").lower()
        if DEBUG:
            print("[news] GET", url, "->", r.status_code, ct)

        r.raise_for_status()

        items = _parse_rss_or_atom(r.content)
        if DEBUG:
            print("[news] items:", len(items), "from", url)

        return items
    except Exception as e:
        if DEBUG:
            print("[news] Feed failed:", url, "err:", repr(e))
        return []


def _dedupe(items: list[dict]) -> list[dict]:
    """
    Basic dedupe by normalized (title, link).
    AI clustering later will handle deeper dedupe.
    """
    seen = set()
    out = []
    for it in items:
        t = (it.get("title") or "").strip().lower()
        l = (it.get("link") or "").strip()
        key = (t, l)
        if not t or not l:
            continue
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
    Returns a list of dicts:
      { "title": str, "link": str, "published_dt": datetime|None }
    Notes:
      - If published_dt can't be parsed, the item is kept (important).
      - We do our own last-24h filter; Google News query includes when:1d but we still filter.
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

        # ✅ key fix: only filter if we successfully parsed date
        if published_dt is not None and published_dt < cutoff:
            continue

        recent_items.append(it)

    # Sort newest first; undated items go last
    recent_items.sort(key=lambda x: x["published_dt"] or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)

    _debug("After 24h filter:", len(recent_items))
    return recent_items
