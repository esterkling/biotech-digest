# src/news.py
import re
import feedparser
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import urlparse, parse_qs, unquote

RSS = [
    "https://news.google.com/rss/search?q=biotech+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=pharma+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=biotech+IPO+S-1+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=FDA+approval+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=EMA+CHMP+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=biotech+acquires+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=licensing+deal+biotech+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=clinical+trial+readout+biotech+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=Series+A+biotech+raises+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=Nordic+biotech+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=European+biotech+when:1d&hl=en-US&gl=US&ceid=US:en",
]

IPO_RE = re.compile(r"\b(IPO|S-1|S-1/A|F-1|F-1/A|424B4|priced its IPO|prices IPO|sets terms)\b", re.IGNORECASE)
DEAL_RE = re.compile(r"\b(acquire|acquisition|merger|M&A|buyout|tender offer|license|licensing|partner|partnership|collaboration|deal)\b", re.IGNORECASE)
FIN_RE  = re.compile(r"\b(Series\s+[A-Z]|financing|raises|raised|funding|private placement|public offering|follow-on|PIPE)\b", re.IGNORECASE)
CLIN_RE = re.compile(r"\b(Phase\s+[123]|topline|readout|met endpoint|trial results|interim data|clinical hold|SAE|safety)\b", re.IGNORECASE)
REG_RE  = re.compile(r"\b(FDA|EMA|CHMP|approval|CRL|PDUFA|BLA|NDA|MAA|complete response|clinical hold)\b", re.IGNORECASE)

NORDIC_EU_RE = re.compile(r"\b(Nordic|Sweden|Norway|Denmark|Finland|Iceland|European|Europe|EU|EMA)\b", re.IGNORECASE)
PHARMA_RE = re.compile(r"\b(Novartis|Roche|Pfizer|AstraZeneca|GSK|Sanofi|Merck|BMS|J&J|Johnson\s*&\s*Johnson|AbbVie|Lilly|Novo|Takeda|Bayer)\b", re.IGNORECASE)

MONEY_RE = re.compile(r"\$\s?([0-9]+(?:\.[0-9]+)?)\s?(B|bn|billion|M|m|million)?", re.IGNORECASE)

def _canonicalize_url(url: str) -> str:
    """
    Try to unwrap Google News URLs to their underlying article URL when present.
    """
    try:
        p = urlparse(url)
        qs = parse_qs(p.query)
        if "url" in qs and qs["url"]:
            return unquote(qs["url"][0])
        return url
    except Exception:
        return url

def _normalize_title(title: str) -> str:
    """
    Remove source suffixes and normalize punctuation/spaces.
    """
    t = (title or "").strip()
    # Remove " - Outlet" suffix (Google News often appends publisher)
    t = re.split(r"\s+-\s+", t, maxsplit=1)[0].strip()
    t = t.lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def _is_near_duplicate(a: str, b: str, threshold: float = 0.92) -> bool:
    return SequenceMatcher(None, a, b).ratio() >= threshold

def fetch_last_24h(limit_per_feed: int = 40) -> list[dict]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    items: list[dict] = []
    for url in RSS:
        d = feedparser.parse(url)
        for e in d.entries[:limit_per_feed]:
            title = getattr(e, "title", "").strip()
            link = getattr(e, "link", "").strip()
            published = getattr(e, "published_parsed", None)

            if not title or not link:
                continue

            if published:
                dt = datetime(*published[:6], tzinfo=timezone.utc)
                if dt < cutoff:
                    continue

            items.append({
                "title": title,
                "link": _canonicalize_url(link),
                "norm_title": _normalize_title(title),
            })

    # First pass: exact de-dupe (same normalized title OR same canonical URL)
    seen_title = set()
    seen_url = set()
    dedup1 = []
    for it in items:
        if it["norm_title"] in seen_title:
            continue
        if it["link"] in seen_url:
            continue
        seen_title.add(it["norm_title"])
        seen_url.add(it["link"])
        dedup1.append(it)

    # Second pass: fuzzy de-dupe (near-identical titles)
    dedup2: list[dict] = []
    for it in dedup1:
        if any(_is_near_duplicate(it["norm_title"], j["norm_title"]) for j in dedup2):
            continue
        dedup2.append(it)

    # Strip helper field before returning
    out = [{"title": it["title"], "link": it["link"]} for it in dedup2]
    return out

def categorize(items: list[dict], max_per_category: int = 6) -> dict[str, list[dict]]:
    cats: dict[str, list[dict]] = {
        "💰 Financings": [],
        "🚀 IPOs / Public markets": [],
        "🤝 M&A and licensing": [],
        "🧪 Clinical readouts / safety": [],
        "🏛️ FDA / EMA regulatory": [],
        "💊 Pharma / big biotech": [],
        "🌍 Nordic / European biotech": [],
        "🗞️ Other notable biotech": [],
    }

    for it in items:
        t = it["title"]
        if IPO_RE.search(t):
            cats["🚀 IPOs / Public markets"].append(it)
        elif DEAL_RE.search(t):
            cats["🤝 M&A and licensing"].append(it)
        elif FIN_RE.search(t):
            cats["💰 Financings"].append(it)
        elif REG_RE.search(t):
            cats["🏛️ FDA / EMA regulatory"].append(it)
        elif CLIN_RE.search(t):
            cats["🧪 Clinical readouts / safety"].append(it)
        elif NORDIC_EU_RE.search(t):
            cats["🌍 Nordic / European biotech"].append(it)
        elif PHARMA_RE.search(t):
            cats["💊 Pharma / big biotech"].append(it)
        else:
            cats["🗞️ Other notable biotech"].append(it)

    for k in list(cats.keys()):
        cats[k] = cats[k][:max_per_category]
    return cats

def _money_to_usd_millions(title: str) -> float:
    vals = []
    for m in MONEY_RE.finditer(title or ""):
        num = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit in ("b", "bn", "billion"):
            vals.append(num * 1000.0)
        elif unit in ("m", "million"):
            vals.append(num)
    return max(vals) if vals else 0.0

def materiality_score(title: str, category: str) -> int:
    t = (title or "").lower()
    score = 0
    if category.startswith("🤝"):
        score += 35
    elif category.startswith("🏛️"):
        score += 30
    elif category.startswith("🧪"):
        score += 25
    elif category.startswith("🚀"):
        score += 22
    elif category.startswith("💰"):
        score += 18
    elif category.startswith("💊"):
        score += 10
    elif category.startswith("🌍"):
        score += 8
    else:
        score += 5

    if "acquire" in t or "acquisition" in t or "merger" in t:
        score += 20
    if "license" in t or "licensing" in t or "collaboration" in t or "partnership" in t:
        score += 12
    if "approval" in t or "approved" in t:
        score += 18
    if "complete response" in t or "crl" in t:
        score += 18
    if "clinical hold" in t:
        score += 18
    if "phase 3" in t:
        score += 12
    if "topline" in t or "met endpoint" in t or "readout" in t:
        score += 10
    if "ipo" in t and ("priced" in t or "prices" in t):
        score += 10

    mm = _money_to_usd_millions(title)
    if mm >= 1000:
        score += 25
    elif mm >= 500:
        score += 18
    elif mm >= 100:
        score += 12
    elif mm >= 50:
        score += 6

    return score
