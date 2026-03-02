# src/news.py
from __future__ import annotations

import os
import re
import html
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, unquote
import xml.etree.ElementTree as ET

import requests

# ----------------------------
# Config
# ----------------------------
UA = os.environ.get(
    "NEWS_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
)
DEBUG = os.environ.get("DEBUG_NEWS", "").lower() in ("1", "true", "yes", "y")

GOOGLE_UNWRAP_CAP = int(os.environ.get("GOOGLE_UNWRAP_CAP", "30"))
PER_FEED_LIMIT = int(os.environ.get("PER_FEED_LIMIT", "40"))

REQUEST_TIMEOUT_S = int(os.environ.get("NEWS_TIMEOUT_S", "25"))

# ----------------------------
# "Guaranteed" direct sources
# (official RSS where available)
# ----------------------------
DIRECT_FEEDS = [
    # BioSpace RSS list: https://www.biospace.com/rss-feeds  (All News & category feeds)
    "https://www.biospace.com/all-news.rss",
    "https://www.biospace.com/deals.rss",
    "https://www.biospace.com/fda.rss",
    "https://www.biospace.com/drug-development.rss",
    "https://www.biospace.com/business.rss",

    # BioPharma Dive
    "https://www.biopharmadive.com/feeds/news/",

    # Fierce Biotech RSS list: https://www.fiercebiotech.com/fiercebiotechcom/rss-feeds
    "https://www.fiercebiotech.com/rss/xml",

    # STAT RSS list: https://www.statnews.com/rss-feeds/
    "https://www.statnews.com/feed/",
    "https://www.statnews.com/category/biotech/feed/",
    "https://www.statnews.com/category/pharma/feed/",
]

# ----------------------------
# Endpoints News
# Their /feed/ sometimes blocks bots.
# We'll include multiple options and accept whichever works.
# ----------------------------
ENDPOINTS_FEEDS = [
    # Primary (new domain)
    "https://endpoints.news/feed/",

    # Fallbacks (legacy domain feeds often still work for channels)
    # Example found historically: https://endpts.com/channel/regulatory/feed/
    "https://endpts.com/channel/deals/feed/",
    "https://endpts.com/channel/financing/feed/",
    "https://endpts.com/channel/regulatory/feed/",
    "https://endpts.com/channel/clinical/feed/",
]

# ----------------------------
# Reuters/Bloomberg: fallback only (not “guaranteed”)
# We'll pull headlines via Google News RSS.
# ----------------------------
def _google_news_rss_url(query: str) -> str:
    # when:1d restricts to last day in Google News
    q = quote_plus(f"{query} when:1d")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

FALLBACK_GOOGLE_QUERIES = [
    # Reuters
    "site:reuters.com biotech acquisition",
    "site:reuters.com pharma deal",
    "site:reuters.com FDA approval biotech",
    "site:reuters.com EMA approval drug",

    # Bloomberg
    "site:bloomberg.com biotech acquisition",
    "site:bloomberg.com pharma deal",
    "site:bloomberg.com FDA approval biotech",
]

GOOGLE_FEEDS = [_google_news_rss_url(q) for q in FALLBACK_GOOGLE_QUERIES]

FEEDS = DIRECT_FEEDS + ENDPOINTS_FEEDS + GOOGLE_FEEDS


# ----------------------------
# Logging
# ----------------------------
def _debug(*args):
    if DEBUG:
        print("[news]", *args)


# ----------------------------
# Text + datetime helpers
# ----------------------------
def _clean_text(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _parse_dt(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    dt_str = dt_str.strip()

    # RFC 2822
    try:
        dt = parsedate_to_datetime(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass

    # ISO 8601
    try:
        s = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _get_text(elem: ET.Element, tag_names: list[str]) -> str | None:
    # Namespace-safe search by suffix
    for child in list(elem):
        suffix = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if suffix in tag_names:
            if child.text:
                return child.text.strip()
    return None


# ----------------------------
# RSS/Atom parser
# ----------------------------
def _parse_rss_or_atom(xml_bytes: bytes) -> list[dict]:
    """
    Parses RSS2 / Atom.
    Returns list of {title, link, published_dt}
    (No Google unwrapping here — we do that later lazily)
    """
    root = ET.fromstring(xml_bytes)
    root_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

    items: list[dict] = []

    # RSS
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

            if title and link:
                items.append({"title": title, "link": link, "published_dt": published_dt})
        return items

    # Atom
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

        if title and link:
            items.append({"title": title, "link": link, "published_dt": published_dt})

    return items


# ----------------------------
# Fetcher
# ----------------------------
def _fetch_feed(url: str, timeout_s: int = REQUEST_TIMEOUT_S) -> list[dict]:
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
# Google News unwrapping (lazy)
# ----------------------------
def _unwrap_google_news(url: str, timeout_s: int = 10) -> str:
    """
    Best-effort to turn Google News wrapper into publisher URL.
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

        # Redirects got us off Google
        final = (r.url or "").strip()
        if final and "news.google.com" not in final:
            return final

        html_text = r.text or ""

        # Pattern: url=https%3A%2F%2Fpublisher...
        m = re.search(r"[?&]url=(https%3A%2F%2F[^&\"'>]+)", html_text)
        if m:
            real = unquote(m.group(1))
            if real.startswith("https://") and "news.google.com" not in real:
                return real

        # Fallback: first non-google https:// link in HTML
        m2 = re.search(r'(https://(?!news\.google\.com|www\.google\.com)[^\s"\'<>]+)', html_text)
        if m2:
            return m2.group(1)

        return url
    except Exception:
        return url


def _unwrap_some_google_links(items: list[dict], max_unwrap: int) -> list[dict]:
    if max_unwrap <= 0:
        return items

    count = 0
    for it in items:
        link = it.get("link") or ""
        if "news.google.com" in link:
            it["link"] = _unwrap_google_news(link)
            count += 1
            if count >= max_unwrap:
                break

    _debug(f"Unwrapped {count} Google links (cap={max_unwrap}).")
    return items


# ----------------------------
# Public API
# ----------------------------
def fetch_last_24h(limit_per_feed: int = PER_FEED_LIMIT) -> list[dict]:
    """
    Returns list of {title, link, published_dt}
    - Drops items older than 24h IF they have a parsed published_dt
    - Keeps undated items (some feeds are missing dates)
    - Unwraps some Google News links (Reuters/Bloomberg fallback)
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    _debug("Feeds:", len(FEEDS))

    all_items: list[dict] = []
    for feed_url in FEEDS:
        feed_items = _fetch_feed(feed_url)
        if feed_items:
            all_items.extend(feed_items[:limit_per_feed])

    all_items = _dedupe(all_items)
    _debug("Total collected:", len(all_items))

    recent_items: list[dict] = []
    for it in all_items:
        published_dt = it.get("published_dt")
        if published_dt is not None and published_dt < cutoff:
            continue
        recent_items.append(it)

    # Newest first; undated items last
    recent_items.sort(
        key=lambda x: x["published_dt"] or datetime(1970, 1, 1, tzinfo=timezone.utc),
        reverse=True,
    )

    _debug("After 24h filter:", len(recent_items))

    # Only unwrap a limited number to avoid timeouts
    recent_items = _unwrap_some_google_links(recent_items, GOOGLE_UNWRAP_CAP)

    return recent_items
