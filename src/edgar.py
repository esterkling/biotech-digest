# src/edgar.py
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple

import requests
from bs4 import BeautifulSoup

import difflib

# --- Ticker detection helpers (NEW) ---

# Common patterns in headlines
_TICKER_PARENS_RE = re.compile(r"\(([A-Z]{1,6})\)")
_TICKER_EXCH_RE = re.compile(r"\b(?:NASDAQ|Nasdaq|NYSE|Nyse|AMEX|Amex)\s*:\s*([A-Z]{1,6})\b")
_TICKER_DASH_RE = re.compile(r"\b([A-Z]{1,6})\s*-\s*(?:NASDAQ|NYSE|AMEX)\b")

# IPO-ish phrases for company-name extraction
_IPO_PHRASE_SPLIT_RE = re.compile(
    r"\b(prices|priced|sets terms|files|filed|launches|plans|seeks|targets)\b.*\b(ipo|s-1|f-1|nasdaq|nyse)\b",
    re.IGNORECASE
)

# Cache SEC ticker map in memory for the run
_TICKER_MAP_CACHE = None  # type: ignore

def _load_sec_ticker_map(user_agent: str):
    """
    Returns list of dicts with: ticker, name, cik
    Cached per run.
    """
    global _TICKER_MAP_CACHE
    if _TICKER_MAP_CACHE is not None:
        return _TICKER_MAP_CACHE

    data = _sec_get(SEC_TICKER_MAP_URL, user_agent).json()
    rows = []
    for _, row in data.items():
        name = (row.get("title") or "").strip()
        ticker = (row.get("ticker") or "").strip().upper()
        cik = row.get("cik_str")
        if name and ticker and cik:
            rows.append({"ticker": ticker, "name": name, "cik": str(cik).zfill(10)})

    _TICKER_MAP_CACHE = rows
    return rows

def _normalize_name(s: str) -> str:
    s = s.lower()
    # drop punctuation-ish chars
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    # collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    # remove common suffixes
    for suf in ["inc", "incorporated", "ltd", "limited", "plc", "corp", "corporation", "ag", "sa", "ab", "bv", "nv", "holdings", "group"]:
        s = re.sub(rf"\b{suf}\b", "", s).strip()
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _extract_company_name_from_headline(title: str) -> str | None:
    """
    Best-effort: take the chunk before typical IPO verbs/phrases,
    or before separators like ':' or ' - '.
    """
    # Remove source suffixes sometimes embedded in Google News titles: " - Outlet"
    base = re.split(r"\s+-\s+", title, maxsplit=1)[0].strip()

    # If "Company XYZ prices IPO..." exists, take left side before the IPO phrase
    m = _IPO_PHRASE_SPLIT_RE.search(base)
    if m:
        left = base[:m.start()].strip()
        if 2 <= len(left) <= 80:
            return left

    # Otherwise split on ':' (e.g., "Generate Biomedicines: files S-1")
    if ":" in base:
        left = base.split(":", 1)[0].strip()
        if 2 <= len(left) <= 80:
            return left

    # Otherwise use first ~6 words as a rough company candidate
    words = base.split()
    if len(words) >= 2:
        return " ".join(words[:6]).strip()

    return None

def _fuzzy_name_to_ticker(company_name: str, user_agent: str) -> str | None:
    """
    Fuzzy match company name against SEC company names.
    Uses stdlib difflib (no external dependency).
    """
    rows = _load_sec_ticker_map(user_agent)
    target = _normalize_name(company_name)
    if not target or len(target) < 3:
        return None

    # Build list of normalized names to match against
    names = [_normalize_name(r["name"]) for r in rows]

    # difflib returns close matches by sequence similarity
    matches = difflib.get_close_matches(target, names, n=1, cutoff=0.88)
    if not matches:
        return None

    best_norm = matches[0]
    idx = names.index(best_norm)
    return rows[idx]["ticker"]

def guess_ticker_from_text(title: str, user_agent: str) -> str | None:
    """
    Best-effort ticker extraction from a headline.
    1) (TICKER)
    2) NASDAQ:TICKER / NYSE:TICKER
    3) TICKER - NASDAQ
    4) Fuzzy match company name -> ticker via SEC map
    """
    # 1) (TICKER)
    m = _TICKER_PARENS_RE.search(title)
    if m:
        return m.group(1).upper()

    # 2) NASDAQ:TICKER
    m = _TICKER_EXCH_RE.search(title)
    if m:
        return m.group(1).upper()

    # 3) TICKER - NASDAQ
    m = _TICKER_DASH_RE.search(title)
    if m:
        return m.group(1).upper()

    # 4) Fuzzy company name
    cname = _extract_company_name_from_headline(title)
    if cname:
        return _fuzzy_name_to_ticker(cname, user_agent)

    return None

SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"

# Priority order for IPO-related filings
IPO_FORMS_PRIORITY = ["424B4", "S-1/A", "S-1", "F-1/A", "F-1"]

# Regex helpers
_MONEY_RE = re.compile(r"\$\s?([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?)")
_PER_SHARE_RE = re.compile(r"\bper\s+share\b", re.IGNORECASE)
_PREFERRED_RE = re.compile(r"\b(preferred\s+stock|series\s+[a-z0-9\-]+)\b", re.IGNORECASE)
_COMMON_RE = re.compile(r"\bcommon\s+stock\b", re.IGNORECASE)

# Extra clues that often appear in the “Recent Sales…” section
_RECENT_SALES_CLUES = re.compile(r"\b(recent\s+sales|unregistered|sold|issued)\b", re.IGNORECASE)


def _sec_get(url: str, user_agent: str, sleep_s: float = 0.25) -> requests.Response:
    """
    SEC requires a descriptive User-Agent with contact info.
    Keep requests modest; increase sleep_s if you batch many names.
    """
    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
    }
    r = requests.get(url, headers=headers, timeout=30)
    time.sleep(sleep_s)
    r.raise_for_status()
    return r


def ticker_to_cik10(ticker: str, user_agent: str) -> str:
    """
    Convert a stock ticker to 10-digit CIK string via SEC ticker map.
    """
    t = ticker.strip().upper()
    data = _sec_get(SEC_TICKER_MAP_URL, user_agent).json()
    for _, row in data.items():
        if row.get("ticker", "").upper() == t:
            return str(row["cik_str"]).zfill(10)
    raise ValueError(f"CIK not found for ticker: {ticker}")


def get_company_submissions(cik10: str, user_agent: str) -> Dict[str, Any]:
    """
    Pull SEC submissions JSON for a company.
    """
    url = f"https://data.sec.gov/submissions/CIK{cik10}.json"
    return _sec_get(url, user_agent).json()


def pick_latest_ipo_filing(submissions_json: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Select the latest IPO-relevant filing with priority: 424B4 > S-1/A > S-1 > F-1/A > F-1.
    Returns dict: {form, accession, date, primary_doc}
    """
    recent = submissions_json.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])

    filings = []
    for form, acc, dt, doc in zip(forms, accessions, dates, primary_docs):
        filings.append({"form": form, "accession": acc, "date": dt, "primary_doc": doc})

    # newest first
    filings.sort(key=lambda x: x["date"], reverse=True)

    for preferred_form in IPO_FORMS_PRIORITY:
        for f in filings:
            if f["form"] == preferred_form:
                return f

    return filings[0] if filings else None


def filing_primary_doc_url(cik10: str, accession: str, primary_doc: str) -> str:
    """
    Build the SEC Archives URL for the filing primary document (HTML).
    """
    acc_nodashes = accession.replace("-", "")
    # SEC uses numeric CIK in the path
    cik_int = int(cik10)
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodashes}/{primary_doc}"


def download_filing_html(cik10: str, accession: str, primary_doc: str, user_agent: str) -> str:
    url = filing_primary_doc_url(cik10, accession, primary_doc)
    return _sec_get(url, user_agent).text


def _parse_float_money(token: str) -> Optional[float]:
    try:
        return float(token.replace(",", ""))
    except Exception:
        return None


def extract_last_private_round_price(html: str) -> Dict[str, Any]:
    """
    Best-effort heuristic extraction:
    - Find lines mentioning Preferred Stock + 'per share' + $X
    - Choose the *last* plausible candidate in document order
    Return:
      {
        price: float|None,
        currency: "USD",
        confidence: 0..1,
        snippet: str|None,
        rationale: str
      }
    Notes:
    - This is not perfect; filings vary. We include confidence + snippet for audit.
    """
    soup = BeautifulSoup(html, "lxml")
    # keep line breaks for scan
    text = soup.get_text("\n", strip=True)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    candidates: list[Tuple[float, str, float, str]] = []  # (price, snippet, score, rationale)

    for ln in lines:
        # must have $ and per share
        if not _MONEY_RE.search(ln):
            continue
        if not _PER_SHARE_RE.search(ln):
            continue

        # Strong preference: preferred/series mentioned
        has_pref = bool(_PREFERRED_RE.search(ln))
        has_common = bool(_COMMON_RE.search(ln))

        # Extract monetary values
        monies = []
        for m in _MONEY_RE.finditer(ln):
            val = _parse_float_money(m.group(1))
            if val is None:
                continue
            monies.append(val)

        # Filter to plausible per-share numbers (avoid $400,000,000 etc.)
        monies = [v for v in monies if 0.01 < v < 500]
        if not monies:
            continue

        # heuristic: pick smallest plausible $ on the line as per-share
        price = min(monies)

        # Score confidence
        score = 0.35
        rationale_bits = []

        if has_pref:
            score += 0.30
            rationale_bits.append("mentions preferred/series")
        if _RECENT_SALES_CLUES.search(ln):
            score += 0.20
            rationale_bits.append("recent sales / sold / issued clue")
        if "conversion" in ln.lower():
            score += 0.05
            rationale_bits.append("mentions conversion")
        if has_common and not has_pref:
            # common stock per-share sometimes reflects option exercises, etc.
            score -= 0.10
            rationale_bits.append("common stock mention without preferred")

        # Bound
        score = max(0.05, min(score, 0.95))
        rationale = ", ".join(rationale_bits) if rationale_bits else "per-share $ found"

        candidates.append((price, ln[:500], score, rationale))

    if not candidates:
        return {
            "price": None,
            "currency": "USD",
            "confidence": 0.0,
            "snippet": None,
            "rationale": "no preferred-per-share candidates found",
        }

    # Choose the last candidate (later in doc often corresponds to later round disclosures)
    price, snippet, score, rationale = candidates[-1]
    return {
        "price": price,
        "currency": "USD",
        "confidence": score,
        "snippet": snippet,
        "rationale": rationale,
    }


def compute_step_change(private_price: float,
                        ipo_low: Optional[float] = None,
                        ipo_high: Optional[float] = None,
                        ipo_final: Optional[float] = None) -> Dict[str, float]:
    """
    Compute percentage change from last private share price to IPO low/high/final.
    """
    def pct(new: float) -> float:
        return round((new / private_price - 1.0) * 100.0, 1)

    out: Dict[str, float] = {}
    if ipo_low is not None:
        out["vs_ipo_low_pct"] = pct(ipo_low)
    if ipo_high is not None:
        out["vs_ipo_high_pct"] = pct(ipo_high)
    if ipo_final is not None:
        out["vs_ipo_final_pct"] = pct(ipo_final)
    return out


def edgar_private_price_analysis(
    ticker: str,
    user_agent: str,
    ipo_low: Optional[float] = None,
    ipo_high: Optional[float] = None,
    ipo_final: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Main callable for your daily digest.

    Returns a dict with:
      - selected filing info (form, accession, date, url)
      - extracted last private round price/share (best-effort)
      - optional step-up/down vs IPO pricing inputs
      - confidence + snippet for auditing
    """
    ticker_u = ticker.strip().upper()

    try:
        cik10 = ticker_to_cik10(ticker_u, user_agent)
        subs = get_company_submissions(cik10, user_agent)
        filing = pick_latest_ipo_filing(subs)

        if not filing:
            return {
                "ticker": ticker_u,
                "error": "No filings found",
                "last_private_round_price_per_share": None,
            }

        form = filing["form"]
        accession = filing["accession"]
        filing_date = filing["date"]
        primary_doc = filing["primary_doc"]
        url = filing_primary_doc_url(cik10, accession, primary_doc)

        html = download_filing_html(cik10, accession, primary_doc, user_agent)
        extracted = extract_last_private_round_price(html)

        step = {}
        if extracted["price"] is not None and any(v is not None for v in [ipo_low, ipo_high, ipo_final]):
            step = compute_step_change(extracted["price"], ipo_low=ipo_low, ipo_high=ipo_high, ipo_final=ipo_final)

        return {
            "ticker": ticker_u,
            "cik": cik10,
            "filing_form_used": form,
            "filing_accession": accession,
            "filing_date": filing_date,
            "filing_url": url,
            "last_private_round_price_per_share": extracted["price"],
            "currency": extracted["currency"],
            "extraction_confidence": extracted["confidence"],
            "extraction_rationale": extracted["rationale"],
            "supporting_snippet": extracted["snippet"],
            "step_up_down_pct": step,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        return {
            "ticker": ticker_u,
            "error": str(e),
            "last_private_round_price_per_share": None,
        }
