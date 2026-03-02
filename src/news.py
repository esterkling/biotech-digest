# src/news.py
import re
import feedparser
from datetime import datetime, timedelta, timezone

# Broad coverage with “last 24h” via Google News RSS queries
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

# Categorization regexes
IPO_RE = re.compile(r"\b(IPO|S-1|S-1/A|F-1|F-1/A|424B4|priced its IPO|prices IPO|sets terms)\b", re.IGNORECASE)
DEAL_RE = re.compile(r"\b(acquire|acquisition|merger|M&A|buyout|tender offer|license|licensing|partner|partnership|collaboration|deal)\b", re.IGNORECASE)
FIN_RE  = re.compile(r"\b(Series\s+[A-Z]|financing|raises|raised|funding|private placement|public offering|follow-on|PIPE)\b", re.IGNORECASE)
CLIN_RE = re.compile(r"\b(Phase\s+[123]|topline|readout|met endpoint|trial results|interim data|clinical hold|SAE|safety)\b", re.IGNORECASE)
REG_RE  = re.compile(r"\b(FDA|EMA|CHMP|approval|CRL|PDUFA|BLA|NDA|MAA|complete response|clinical hold)\b", re.IGNORECASE)

NORDIC_EU_RE = re.compile(r"\b(Nordic|Sweden|Norway|Denmark|Finland|Iceland|European|Europe|EU|EMA)\b", re.IGNORECASE)
PHARMA_RE = re.compile(r"\b(Novartis|Roche|Pfizer|AstraZeneca|GSK|Sanofi|Merck|BMS|J&J|Johnson\s*&\s*Johnson|AbbVie|Lilly|Novo|Takeda|Bayer)\b", re.IGNORECASE)

# Money detection for “materiality”
MONEY_RE = re.compile(r"\$\s?([0-9]+(?:\.[0-9]+)?)\s?(B|bn|billion|M|m|million)?", re.IGNORECASE)

def fetch_last_24h(limit_per_feed: int = 40) -> list[dict]:
    """
    Pull items from RSS feeds and best-effort filter to last 24 hours if timestamps are available.
    Returns list of {title, link}.
    """
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

            items.append({"title": title, "link": link})

    # De-duplicate
    seen = set()
    out = []
    for it in items:
        key = (it["title"], it["link"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)

    return out

def categorize(items: list[dict], max_per_category: int = 6) -> dict[str, list[dict]]:
    """
    Categorize items into digest sections.
    """
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

    # Keep Slack concise
    for k in list(cats.keys()):
        cats[k] = cats[k][:max_per_category]

    return cats

def _money_to_usd_millions(title: str) -> float:
    """
    Roughly parse the largest $ amount in the title and convert to millions.
    """
    vals = []
    for m in MONEY_RE.finditer(title):
        num = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit in ("b", "bn", "billion"):
            vals.append(num * 1000.0)
        elif unit in ("m", "million"):
            vals.append(num)
        else:
            # ambiguous if no unit; ignore to reduce false positives
            continue
    return max(vals) if vals else 0.0

def materiality_score(title: str, category: str) -> int:
    """
    Simple heuristic scoring so we can pick “Top 3”.
    Higher = more material.
    """
    t = title.lower()
    score = 0

    # Category base weights
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

    # Big signal keywords
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

    # Money boosts
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
