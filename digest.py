# digest.py
import os
import re
from datetime import datetime
from dateutil import tz

from src.news import fetch_last_24h
from src.slack import post
from src.extract import extract_article_text

from src.ai import ai_cluster_headlines, ai_extract_structured

from src.edgar import (
    guess_ticker_from_text,
    extract_company_name_from_headline,
    edgar_private_price_analysis,
    edgar_private_price_analysis_from_company,
)

# -----------------------
# Quality controls (BLOCKLIST approach)
# -----------------------
BLOCKED_DOMAINS = {
    # low-signal / spammy / clickbait finance wrappers
    "stocktitan.net",
    "investing.com",       # mostly analyst notes / price targets
    "marketscreener.com",
    "seekingalpha.com",
    "benzinga.com",
    "zacks.com",
    "tipranks.com",
    "simplywall.st",

    # misc/low relevance that leaked in your example
    "mva.org",
    "newsonair.gov.in",
    "medwatch.com",
    "businessreport.co.za",

    # google wrapper (we want the underlying source)
    

    # any other recurring low-quality sources you notice
    "parameter.io",
}

# -----------------------
# Scheduling / gating
# -----------------------
def should_run_now_stockholm() -> bool:
    now = datetime.now(tz=tz.gettz("Europe/Stockholm"))
    return now.hour == 8


def is_force_send() -> bool:
    return os.environ.get("FORCE_SEND", "").lower() in ("1", "true", "yes", "y")


# -----------------------
# Helpers
# -----------------------
def host_from_url(url: str) -> str:
    try:
        return url.split("/")[2].lower()
    except Exception:
        return ""


def slack_link(url: str, title: str) -> str:
    title = (title or "").replace("\n", " ").strip()
    if len(title) > 95:
        title = title[:92] + "..."
    return f"<{url}|{title}>"


def slack_source_link(url: str) -> str:
    host = host_from_url(url) or "source"
    return f"<{url}|{host}>"


def section_name(ai_category: str) -> str:
    mapping = {
        "Financings": "💰 Financings",
        "IPOs/Public markets": "🚀 IPOs / Public markets",
        "M&A/Licensing": "🤝 M&A and licensing",
        "Clinical readouts/Safety": "🧪 Clinical readouts / safety",
        "FDA/EMA Regulatory": "🏛️ FDA / EMA regulatory",
        "Pharma/Big biotech": "💊 Pharma / big biotech",
        "Nordic/European biotech": "🌍 Nordic / European biotech",
        "Other": "🗞️ Other notable biotech",
    }
    return mapping.get(ai_category, "🗞️ Other notable biotech")


def is_ipo_category(ai_category: str) -> bool:
    return ai_category.strip().lower() == "ipos/public markets".lower()


# -----------------------
# Deal staleness / recap detection (heuristic, free)
# -----------------------
_RESURFACED_PHRASES = [
    "previously announced",
    "was announced",
    "announced last week",
    "following last week's announcement",
    "earlier this week",
    "earlier this month",
    "last month",
    "last week",
    "weeks ago",
    "previously disclosed",
    "already announced",
    "reiterated",
    "recap",
    "analysis",
    "explainer",
    "background",
    "context",
    "in a note",
]

_ANNOUNCED_ON_DATE_RE = re.compile(
    r"\bannounced\s+on\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+\d{1,2}\b",
    re.IGNORECASE,
)

def is_resurfaced_deal(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    if any(p in t for p in _RESURFACED_PHRASES):
        return True
    if _ANNOUNCED_ON_DATE_RE.search(text):
        return True
    return False


# -----------------------
# EDGAR enrichment (IPO only)
# -----------------------
def edgar_for_ipo(title: str, user_agent: str) -> dict:
    ticker = guess_ticker_from_text(title, user_agent)
    if ticker:
        return edgar_private_price_analysis(ticker, user_agent)

    cname = extract_company_name_from_headline(title)
    if cname:
        return edgar_private_price_analysis_from_company(cname, user_agent)

    return {"error": "Could not infer ticker or company name", "last_private_round_price_per_share": None}


# -----------------------
# AI pipeline
# -----------------------
def build_clusters(raw_items: list[dict]) -> tuple[list[dict], dict[int, list[int]]]:
    """
    Returns:
      reps: representative items [{id,title,url,source}, ...]
      rep_to_other_ids: rep_id -> other raw item ids in same cluster
    """
    cluster_input = []
    for idx, it in enumerate(raw_items):
        cluster_input.append(
            {
                "id": idx,
                "title": it["title"],
                "url": it["link"],
                "source": host_from_url(it["link"]),
            }
        )

    out = ai_cluster_headlines(cluster_input)
    clusters = out.get("clusters", [])

    if not clusters:
        reps = cluster_input
        rep_to_other_ids = {it["id"]: [] for it in reps}
        return reps, rep_to_other_ids

    reps = []
    rep_to_other_ids: dict[int, list[int]] = {}
    seen = set()

    for c in clusters:
        rep_id = int(c.get("representative_id"))
        ids = [int(x) for x in c.get("item_ids", []) if isinstance(x, int) or str(x).isdigit()]
        if rep_id < 0 or rep_id >= len(cluster_input):
            continue
        if rep_id in seen:
            continue
        seen.add(rep_id)

        reps.append(cluster_input[rep_id])
        rep_to_other_ids[rep_id] = [i for i in ids if i != rep_id and 0 <= i < len(cluster_input)]

    if not reps:
        reps = cluster_input
        rep_to_other_ids = {it["id"]: [] for it in reps}

    return reps, rep_to_other_ids


def build_structured(
    reps: list[dict],
    snippet_chars: int = 1600,
    max_items: int = 30,
) -> tuple[dict[int, dict], dict[int, str]]:
    """
    One batch call: categorization + one-line summary + VC takeaway.
    Also returns snippet_by_id so we can apply resurfaced-deal heuristics without refetching.
    """
    reps = reps[:max_items]
    extract_input = []
    snippet_by_id: dict[int, str] = {}

    for r in reps:
        txt = extract_article_text(r["url"]) or ""
        snippet = txt.strip().replace("\n", " ")
        snippet = snippet[:snippet_chars]
        snippet_by_id[r["id"]] = snippet

        extract_input.append(
            {
                "id": r["id"],
                "title": r["title"],
                "url": r["url"],
                "source": r.get("source", ""),
                "snippet": snippet,
            }
        )

    out = ai_extract_structured(extract_input)
    items = out.get("items", [])
    structured_by_id = {int(x["id"]): x for x in items if isinstance(x, dict) and "id" in x}

    return structured_by_id, snippet_by_id


# -----------------------
# Digest builder
# -----------------------
def build_digest_text() -> str:
    ua = os.environ["SEC_USER_AGENT"]

    raw_items = fetch_last_24h()
    if not raw_items:
        return "*Daily Biotech Digest* — (no items found in last ~24h)"

    # Blocklist filter (pre-AI)
    raw_items = [it for it in raw_items if host_from_url(it.get("link", "")) not in BLOCKED_DOMAINS]

    if not raw_items:
        return "*Daily Biotech Digest* — (items found, but all were filtered by blocklist)"

    # A) Deduplicate via AI clustering
    reps, rep_to_others = build_clusters(raw_items)

    # B) AI categorize + summarize + VC takeaway (batch)
    structured_by_id, snippet_by_id = build_structured(reps)

    # Bucket order
    bucket_order = [
        "IPOs/Public markets",
        "Financings",
        "M&A/Licensing",
        "Clinical readouts/Safety",
        "FDA/EMA Regulatory",
        "Pharma/Big biotech",
        "Nordic/European biotech",
        "Other",
    ]
    buckets = {k: [] for k in bucket_order}

    # Deal recaps separately
    resurfaced_deals: list[dict] = []

    # Score for Top 3
    scored = []
    for r in reps[:30]:
        s = structured_by_id.get(r["id"], {})
        cat = s.get("category", "Other")

        # Keep recap/analysis deals out of "new M&A"
        if cat == "M&A/Licensing":
            snip = snippet_by_id.get(r["id"], "")
            if is_resurfaced_deal(snip):
                resurfaced_deals.append(r)
                continue

        if cat not in buckets:
            cat = "Other"
        buckets[cat].append(r)

        mat = (s.get("materiality") or "medium").strip().lower()
        score = {"high": 3, "medium": 2, "low": 1}.get(mat, 2)
        scored.append((score, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    top3 = [r for _, r in scored[:3]]

    now_local = datetime.now(tz=tz.gettz("Europe/Stockholm"))
    lines: list[str] = []
    lines.append(f"*Daily Biotech Digest* — {now_local.strftime('%Y-%m-%d')} (last ~24h)")
    lines.append("")

    # Top 3
    lines.append("*🔥 Top 3 most material*")
    if not top3:
        lines.append("• (No major items surfaced.)")
    else:
        for r in top3:
            s = structured_by_id.get(r["id"], {})
            cat = s.get("category", "Other")
            summary = (s.get("one_line_summary") or "").strip()
            takeaway = (s.get("vc_takeaway") or "").strip()
            mat = (s.get("materiality") or "medium").strip()

            lines.append(f"• [{section_name(cat)}] {slack_link(r['url'], r['title'])}")
            if summary:
                lines.append(f"  ↳ {summary}")
            if takeaway:
                lines.append(f"  ↳ *VC Takeaway:* {takeaway} ({mat})")

            other_ids = rep_to_others.get(r["id"], [])
            if other_ids:
                alt_urls = []
                for oid in other_ids[:2]:
                    try:
                        alt_urls.append(raw_items[oid]["link"])
                    except Exception:
                        pass
                if alt_urls:
                    lines.append("  ↳ Also covered by: " + " / ".join([slack_source_link(u) for u in alt_urls]))
    lines.append("")

    # Sections
    for cat in bucket_order:
        lines.append(f"*{section_name(cat)}*")
        bucket = buckets.get(cat, [])
        if not bucket:
            lines.append("• (No major items surfaced in this window.)")
            lines.append("")
            continue

        for r in bucket:
            s = structured_by_id.get(r["id"], {})
            summary = (s.get("one_line_summary") or "").strip()
            takeaway = (s.get("vc_takeaway") or "").strip()
            mat = (s.get("materiality") or "medium").strip()

            lines.append(f"• {slack_link(r['url'], r['title'])}")
            if summary:
                lines.append(f"  ↳ {summary}")
            if takeaway:
                lines.append(f"  ↳ *VC Takeaway:* {takeaway} ({mat})")

            # IPO EDGAR enrichment
            if is_ipo_category(cat):
                try:
                    ed = edgar_for_ipo(r["title"], ua)
                    p = ed.get("last_private_round_price_per_share")
                    conf = ed.get("extraction_confidence", 0.0)
                    furl = ed.get("filing_url")
                    err = ed.get("error")

                    if p is not None:
                        lines.append(f"  ↳ *EDGAR:* last private price/share (best-effort) *${p}* (conf {conf:.2f})")
                    elif err:
                        lines.append(f"  ↳ *EDGAR:* {err}")
                    else:
                        lines.append(f"  ↳ *EDGAR:* could not extract last private price/share (conf {conf:.2f}).")

                    if furl:
                        lines.append(f"  ↳ Filing: {slack_link(furl, 'SEC filing')}")
                except Exception:
                    lines.append("  ↳ EDGAR enrichment unavailable.")

            # Also covered by
            other_ids = rep_to_others.get(r["id"], [])
            if other_ids:
                alt_urls = []
                for oid in other_ids[:2]:
                    try:
                        alt_urls.append(raw_items[oid]["link"])
                    except Exception:
                        pass
                if alt_urls:
                    lines.append("  ↳ Also covered by: " + " / ".join([slack_source_link(u) for u in alt_urls]))

        lines.append("")

    # Deal recaps / resurfaced items section
    if resurfaced_deals:
        lines.append("*📌 Earlier but resurfaced (deal recap / analysis)*")
        for r in resurfaced_deals[:12]:
            s = structured_by_id.get(r["id"], {})
            summary = (s.get("one_line_summary") or "").strip()
            takeaway = (s.get("vc_takeaway") or "").strip()
            lines.append(f"• {slack_link(r['url'], r['title'])}")
            if summary:
                lines.append(f"  ↳ {summary}")
            if takeaway:
                lines.append(f"  ↳ *VC Takeaway (recap lens):* {takeaway}")
        lines.append("")

    return "\n".join(lines).strip()


def main():
    # Only post at 08:00 Stockholm unless forced
    if not is_force_send() and not should_run_now_stockholm():
        print("Not 08:00 Stockholm time and FORCE_SEND not set; exiting.")
        return

    # Required env vars (fail fast)
    _ = os.environ["SLACK_WEBHOOK_URL"]
    _ = os.environ["SEC_USER_AGENT"]
    _ = os.environ["GROQ_API_KEY"]

    text = build_digest_text()
    post(os.environ["SLACK_WEBHOOK_URL"], text)
    print("Posted digest to Slack.")


if __name__ == "__main__":
    main()
