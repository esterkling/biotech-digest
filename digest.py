# digest.py
import os
from datetime import datetime
from dateutil import tz

from src.news import fetch_last_24h, categorize, materiality_score
from src.slack import post
from src.edgar import edgar_private_price_analysis, guess_ticker_from_text
from src.ai import ai_summarize_takeaway
from src.extract import extract_article_text
from src.ai import ai_cluster_headlines, ai_extract_structured


def should_run_now_stockholm() -> bool:
    """
    Your workflow runs twice/day; this ensures you post only once at 08:00 Stockholm time (DST-safe).
    """
    now = datetime.now(tz=tz.gettz("Europe/Stockholm"))
    return now.hour == 8


def slack_link(url: str, title: str) -> str:
    """
    Slack "pretty link" format: <url|text>
    """
    title = (title or "").replace("\n", " ").strip()
    if len(title) > 95:
        title = title[:92] + "..."
    return f"<{url}|{title}>"


def item_takeaway(category: str, title: str) -> str:
    """
    One-sentence VC takeaway per item (heuristic).
    """
    t = title.lower()

    article_text = extract_article_text(link)

try:
    ai_data = ai_summarize_takeaway(title, link, cat, article_text)

    summary = ai_data.get("summary", "")
    takeaway = ai_data.get("vc_takeaway", "")
    materiality = ai_data.get("materiality", "medium")

    lines.append(f"  ↳ {summary}")
    lines.append(f"  ↳ *VC Takeaway:* {takeaway} ({materiality})")

except Exception as e:
    lines.append("  ↳ AI summary unavailable.")

def build_top3(cats: dict[str, list[dict]]) -> list[dict]:
    """
    Build a “Top 3 most material” list across all categories using heuristic scoring.
    """
    flat = []
    for cat, bucket in cats.items():
        for it in bucket:
            flat.append({
                "category": cat,
                "title": it["title"],
                "link": it["link"],
                "score": materiality_score(it["title"], cat)
            })
    flat.sort(key=lambda x: x["score"], reverse=True)
    return flat[:3]


def build_digest_text() -> str:
    ua = os.environ["SEC_USER_AGENT"]

    items = fetch_last_24h()
    cats = categorize(items)
    top3 = build_top3(cats)

    now_local = datetime.now(tz=tz.gettz("Europe/Stockholm"))
    lines: list[str] = []
    lines.append(f"*Daily Biotech Digest* — {now_local.strftime('%Y-%m-%d')} (last ~24h)")
    lines.append("")

    # --- Top 3 ---
    lines.append("*🔥 Top 3 most material items*")
    if not top3:
        lines.append("• (No major items surfaced in this feed window.)")
    else:
        for x in top3:
            lines.append(f"• [{x['category']}] {slack_link(x['link'], x['title'])}")
            lines.append(f"  ↳ {item_takeaway(x['category'], x['title'])}")
    lines.append("")

    # --- IPO section first, with EDGAR enrichment ---
    ipo_cat = "🚀 IPOs / Public markets"
    ipo_items = cats.get(ipo_cat, [])

    lines.append(f"*{ipo_cat}*")
    if not ipo_items:
        lines.append("• None detected in the last 24 hours.")
    else:
        for it in ipo_items:
            title, link = it["title"], it["link"]
            lines.append(f"• {slack_link(link, title)}")
            lines.append(f"  ↳ {item_takeaway(ipo_cat, title)}")

            # Improved ticker guessing
            ticker = guess_ticker_from_text(title, ua)
            if not ticker:
                lines.append("  ↳ EDGAR: could not infer ticker from headline.")
                continue

            analysis = edgar_private_price_analysis(ticker, ua)
            p = analysis.get("last_private_round_price_per_share")
            conf = analysis.get("extraction_confidence", 0.0)
            filing = analysis.get("filing_form_used")
            fdate = analysis.get("filing_date")
            furl = analysis.get("filing_url")

            if p:
                lines.append(f"  ↳ EDGAR ({ticker}): last private price/share (best-effort) *${p}* (conf {conf:.2f})")
                if filing and fdate and furl:
                    lines.append(f"  ↳ Source: {filing} filed {fdate} — {slack_link(furl, 'SEC filing')}")
            else:
                lines.append(f"  ↳ EDGAR ({ticker}): could not reliably extract last private price/share (conf {conf:.2f}).")
                if furl:
                    lines.append(f"  ↳ Filing: {slack_link(furl, 'SEC filing')}")
    lines.append("")

    # --- Other categories ---
    order = [
        "💰 Financings",
        "🤝 M&A and licensing",
        "🧪 Clinical readouts / safety",
        "🏛️ FDA / EMA regulatory",
        "💊 Pharma / big biotech",
        "🌍 Nordic / European biotech",
        "🗞️ Other notable biotech",
    ]

    for cat in order:
        bucket = cats.get(cat, [])
        lines.append(f"*{cat}*")
        if not bucket:
            lines.append("• (No major items surfaced in this feed window.)")
        else:
            for it in bucket:
                title, link = it["title"], it["link"]
                lines.append(f"• {slack_link(link, title)}")
                lines.append(f"  ↳ {item_takeaway(cat, title)}")
        lines.append("")

    return "\n".join(lines).strip()


def main():
    force_send = os.environ.get("FORCE_SEND", "").lower() in ("1", "true", "yes")

    if not force_send and not should_run_now_stockholm():
        print("Not 08:00 Stockholm time, exiting.")
        return

    slack_url = os.environ["SLACK_WEBHOOK_URL"]
    text = build_digest_text()
    post(slack_url, text)
    print("Posted digest to Slack.")


if __name__ == "__main__":
    main()
