# digest.py
import os
from datetime import datetime
from dateutil import tz

from src.news import fetch_last_24h, categorize
from src.slack import post
from src.edgar import edgar_private_price_analysis, guess_ticker_from_text


def should_run_now_stockholm() -> bool:
    """
    If your workflow runs twice (06:00 and 07:00 UTC), this ensures you post only once
    at 08:00 Europe/Stockholm local time (handles DST cleanly).
    """
    now = datetime.now(tz=tz.gettz("Europe/Stockholm"))
    return now.hour == 8


def slack_link(url: str, title: str) -> str:
    """
    Slack "pretty link" format: <url|text>
    Keeps messages compact and avoids long raw URLs.
    """
    title = (title or "").replace("\n", " ").strip()
    if len(title) > 95:
        title = title[:92] + "..."
    return f"<{url}|{title}>"


def vc_takeaway_stub(category: str) -> str:
    """
    Minimal, safe placeholder takeaway per category.
    (You can later replace with a more sophisticated summarizer.)
    """
    mapping = {
        "💰 Financings": "VC takeaway: Watch pricing + syndicate quality; financings signal where conviction is returning.",
        "🚀 IPOs / Public markets": "VC takeaway: Compare IPO terms vs last private round to gauge step-up/down and liquidity conditions.",
        "🤝 M&A and licensing": "VC takeaway: Deal flow reveals pharma priorities; milestones/structure often matter more than headline value.",
        "🧪 Clinical readouts / safety": "VC takeaway: Readouts reprice quickly—focus on durability, safety, and competitive differentiation.",
        "🏛️ FDA / EMA regulatory": "VC takeaway: Regulatory decisions compress timelines; track labels, post-mkt commitments, and next filings.",
        "💊 Pharma / big biotech": "VC takeaway: Macro moves in big pharma shape partnering and exit appetite for venture-backed assets.",
        "🌍 Nordic / European biotech": "VC takeaway: Regional signal is often early—financings/partnerships can prefigure global attention.",
        "🗞️ Other notable biotech": "VC takeaway: Use quieter headlines to spot emerging themes and second-order effects.",
    }
    return mapping.get(category, "VC takeaway: Track the implications for valuation, BD appetite, and upcoming catalysts.")


def build_digest_text() -> str:
    ua = os.environ["SEC_USER_AGENT"]
    items = fetch_last_24h()
    cats = categorize(items)

    now_local = datetime.now(tz=tz.gettz("Europe/Stockholm"))
    lines: list[str] = []
    lines.append(f"*Daily Biotech Digest* — {now_local.strftime('%Y-%m-%d')} (last ~24h)")
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

            # Improved ticker guessing (no longer depends on (TICKER) in headline)
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
                # If you later parse IPO range/final from news, pass ipo_low/high/final into edgar_private_price_analysis()
                # and then print analysis['step_up_down_pct'] here.
            else:
                lines.append(f"  ↳ EDGAR ({ticker}): could not reliably extract last private price/share (conf {conf:.2f}).")
                if furl:
                    lines.append(f"  ↳ Filing: {slack_link(furl, 'SEC filing')}")

    lines.append(f"_{vc_takeaway_stub(ipo_cat)}_")
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
                lines.append(f"• {slack_link(it['link'], it['title'])}")
        lines.append(f"_{vc_takeaway_stub(cat)}_")
        lines.append("")

    return "\n".join(lines).strip()


def main():
    # Keep your “only at 08:00 Stockholm” gate.
    # For manual testing, you can comment this out temporarily.
    if not should_run_now_stockholm():
        print("Not 08:00 Stockholm time, exiting.")
        return

    slack_url = os.environ["SLACK_WEBHOOK_URL"]
    text = build_digest_text()
    post(slack_url, text)
    print("Posted digest to Slack.")


if __name__ == "__main__":
    main()
