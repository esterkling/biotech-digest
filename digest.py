# digest.py
import os
from datetime import datetime
from dateutil import tz

from src.news import fetch_last_24h, categorize, materiality_score
from src.slack import post
from src.edgar import edgar_private_price_analysis, guess_ticker_from_text


def should_run_now_stockholm() -> bool:
    """
    #Your workflow runs twice/day; this ensures you post only once at 08:00 Stockholm time (DST-safe).
    """
    #now = datetime.now(tz=tz.gettz("Europe/Stockholm"))
    #return now.hour == 8


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

    if category.startswith("🤝"):
        if "acquir" in t or "merger" in t:
            return "VC takeaway: M&A confirms strategic demand—map likely next targets with near-term catalysts."
        return "VC takeaway: Deal structure (upfront vs milestones) is the real signal of conviction—watch who takes development risk."
    if category.startswith("💰"):
        return "VC takeaway: Financing terms reveal risk appetite—track valuation resets, insider participation, and runway extension."
    if category.startswith("🚀"):
        return "VC takeaway: IPO strength is best judged vs last private price/share and aftermarket—this sets the next crossover bar."
    if category.startswith("🧪"):
        if "phase 3" in t:
            return "VC takeaway: Late-stage clinical data can unlock BD quickly—watch durability, safety, and subgroup consistency."
        if "hold" in t or "safety" in t:
            return "VC takeaway: Safety signals reprice companies fast—expect capital structure stress and partnering renegotiations."
        return "VC takeaway: Readouts are about differentiation vs SOC—endpoint selection and effect size drive valuation, not headlines."
    if category.startswith("🏛️"):
        if "approved" in t or "approval" in t:
            return "VC takeaway: Approval de-risks revenue but shifts focus to launch execution, label, and payer dynamics."
        if "crl" in t or "complete response" in t:
            return "VC takeaway: CRLs often become financing events—watch CMC timelines and whether partners step in or step back."
        return "VC takeaway: Regulatory moves compress timelines—track label details and post-marketing requirements."
    if category.startswith("🌍"):
        return "VC takeaway: Europe/Nordics can be early signal—regional wins often precede global partnering and pricing momentum."
    if category.startswith("💊"):
        return "VC takeaway: Big pharma portfolio moves set partnering demand—follow therapeutic area strategy shifts."
    return "VC takeaway: Track second-order effects on valuation comps, partner behavior, and upcoming catalysts."


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
