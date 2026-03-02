import feedparser
from datetime import datetime, timedelta, timezone

RSS = [
  "https://news.google.com/rss/search?q=biotech+when:1d&hl=en-US&gl=US&ceid=US:en",
  "https://news.google.com/rss/search?q=pharma+when:1d&hl=en-US&gl=US&ceid=US:en",
  "https://news.google.com/rss/search?q=biotech+IPO+S-1+when:1d&hl=en-US&gl=US&ceid=US:en",
  "https://news.google.com/rss/search?q=FDA+approval+when:1d&hl=en-US&gl=US&ceid=US:en",
  "https://news.google.com/rss/search?q=EMA+CHMP+when:1d&hl=en-US&gl=US&ceid=US:en",
  "https://news.google.com/rss/search?q=biotech+acquires+when:1d&hl=en-US&gl=US&ceid=US:en",
  "https://news.google.com/rss/search?q=licensing+deal+biotech+when:1d&hl=en-US&gl=US&ceid=US:en",
  "https://news.google.com/rss/search?q=Nordic+biotech+when:1d&hl=en-US&gl=US&ceid=US:en",
  "https://news.google.com/rss/search?q=European+biotech+when:1d&hl=en-US&gl=US&ceid=US:en",
]

def fetch_last_24h(limit=40):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    items = []
    for url in RSS:
        d = feedparser.parse(url)
        for e in d.entries[:limit]:
            title = getattr(e, "title", "")
            link = getattr(e, "link", "")
            published = getattr(e, "published_parsed", None)
            if published:
                dt = datetime(*published[:6], tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
            items.append({"title": title, "link": link})
    # remove duplicates
    seen = set()
    out = []
    for it in items:
        k = (it["title"], it["link"])
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out
