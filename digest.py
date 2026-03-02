import os
import re
from datetime import datetime
from dateutil import tz

from src.news import fetch_last_24h
from src.slack import post
from src.edgar import edgar_private_price_analysis

IPO_HINT = re.compile(r"\b(IPO|S-1|S-1/A|F-1|F-1/A|424B4|prices IPO|priced its IPO|sets terms)\b", re.IGNORECASE)
TICKER_RE = re.compile(r"\(([A-Z]{2,6})\)")

def should_run_now_stockholm() -> bool:
    now = datetime.now(tz=tz.gettz("Europe/Stockholm"))
    return now.hour == 8

def main():
    # Run twice/day in Actions; only post once at 08:00 Stockholm time
    if not should_run_now_stockholm():
        print("Not 08:00 Stockholm time, exiting.")
        return

    ua = os.environ["SEC_USER_AGENT"]
    slack_url = os.environ["SLACK_WEBHOOK_URL"]

    items = fetch_last_24h()
    ipo_items = [it for it in items if IPO_HINT.search(it["title"])]

    lines = []
    lines.append(f"*Daily Biotech Digest* — {datetime.now().strftime('%Y-%m-%d')} (last ~24h)")
    lines.append("")

    # IPO section (with EDGAR enrichment)
    lines.append("*IPOs / Public Markets (with EDGAR S-1 check)*")
    if ipo_items:
        for it in ipo_items[:5]:
            lines.append(f"• {it['title']} — {it['link']}")
            m = TICKER_RE.search(it["title"])
            if m:
                ticker = m.group(1)
                analysis = edgar_private_price_analysis(ticker, ua)
                p = analysis.get("last_private_round_price_per_share")
                c = analysis.get("extraction_confidence", 0.0)
                if p:
                    lines.append(f"  ↳ Last private price/share (best-effort): *${p}* (conf {c:.2f})")
                    # If your news parsing also provides IPO range/final, you can pass them in and print step-up math here.
                    if analysis.get("step_up_down_pct"):
                        step = analysis["step_up_down_pct"]
                        parts = []
                        if "vs_ipo_low_pct" in step: parts.append(f"vs low: {step['vs_ipo_low_pct']}%")
                        if "vs_ipo_high_pct" in step: parts.append(f"vs high: {step['vs_ipo_high_pct']}%")
                        if "vs_ipo_final_pct" in step: parts.append(f"vs final: {step['vs_ipo_final_pct']}%")
                        if parts:
                            lines.append(f"  ↳ Step-up/down: " + ", ".join(parts))
                else:
                    lines.append("  ↳ Could not reliably extract last private price/share from filing.")
            else:
                lines.append("  ↳ No ticker detected in headline (add (TICKER) format for EDGAR enrichment).")
        lines.append("")
    else:
        lines.append("• None detected in last 24h.")
        lines.append("")

    # Headline dump (simple version)
    lines.append("*Top biotech/pharma headlines (last 24h)*")
    for it in items[:12]:
        lines.append(f"• {it['title']} — {it['link']}")

    text = "\n".join(lines)

    post(slack_url, text)
    print("Posted digest to Slack.")

if __name__ == "__main__":
    main()
