# src/edgar.py
from __future__ import annotations

import difflib
import json
import re
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple
from urllib.parse import urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup

SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"

# Priority order for IPO-related filings
IPO_FORMS_PRIORITY = ["424B4", "S-1/A", "S-1", "F-1/A", "F-1"]

# Regex helpers
_MONEY_RE = re.compile(r"\$\s?([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?)")
_PER_SHARE_RE = re.compile(r"\bper\s+share\b", re.IGNORECASE)
_PREFERRED_RE = re.compile(r"\b(preferred\s+stock|series\s+[a-z0-9\-]+)\b", re.IGNORECASE)
_COMMON_RE = re.compile(r"\bcommon\s+stock\b", re.IGNORECASE)
_RECENT_SALES_CLUES = re.compile(r"\b(recent\s+sales|unregistered|sold|issued)\b", re.IGNORECASE)

# Headline ticker patterns
_TICKER_PARENS_RE = re.compile(r"\(([A-Z]{1,6})\)")
_TICKER_EXCH_RE = re.compile(r"\b(?:NASDAQ|Nasdaq|NYSE|Nyse|AMEX|Amex)\s*:\s*([A-Z]{1,6})\b")
_TICKER_DASH_RE = re.compile(r"\b([A-Z]{1,6})\s*-\s*(?:NASDAQ|NYSE|AMEX)\b")

# IPO-ish phrase splitter (for company-name extraction)
_IPO_PHRASE_SPLIT_RE = re.compile(
    r"\b(prices|priced|sets terms|files|filed|launches|plans|seeks|targets)\b.*\b(ipo|s-1|f-1|nasdaq|nyse|sec)\b",
    re.IGNORECASE
)

# Cache SEC ticker map per run
_TICKER_MAP_CACHE: Optional[list[dict]] = None


# -----------------------------
# Low-level SEC HTTP
# -----------------------------
def _sec_get(url: str, user_agent: str, sleep_s: float = 0.25) -> requests.Response:
    """
    SEC requires a descriptive User-Agent with contact info.
    """
    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
    }
    r = requests.get(url, headers=headers, timeout=30)
    time.sleep(sleep_s)
    r.raise_for_status()
    return r


# -----------------------------
# Ticker / company name helpers
# -----------------------------
def _load_sec_ticker_map(user_agent: str) -> list[dict]:
    """
    Returns list of dicts with: ticker, name, cik10.
    Cached per run.
    """
    global _TICKER_MAP_CACHE
    if _TICKER_MAP_CACHE is not None:
        return _TICKER_MAP_CACHE

    data = _sec_get(SEC_TICKER_MAP_URL, user_agent).json()
    rows: list[dict] = []
    for _, row in data.items():
        name = (row.get("title") or "").strip()
        ticker = (row.get("ticker") or "").strip().upper()
        cik = row.get("cik_str")
        if name and ticker and cik:
            rows.append({"ticker": ticker, "name": name, "cik10": str(cik).zfill(10)})

    _TICKER_MAP_CACHE = rows
    return rows


def _normalize_name(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # remove common suffixes
    for suf in [
        "inc", "incorporated", "ltd", "limited", "plc", "corp", "corporation",
        "ag", "sa", "ab", "bv", "nv", "holdings", "group", "the"
    ]:
        s = re.sub(rf"\b{suf}\b", "", s).strip()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _strip_publisher_suffix(title: str) -> str:
    # "Company does X - Reuters" -> "Company does X"
    return re.split(r"\s+-\s+", (title or "").strip(), maxsplit=1)[0].strip()


def extract_company_name_from_headline(title: str) -> Optional[str]:
    """
    Best-effort company-name extraction from headline.
    """
    base = _strip_publisher_suffix(title)

    m = _IPO_PHRASE_SPLIT_RE.search(base)
    if m:
        left = base[:m.start()].strip()
        if 2 <= len(left) <= 90:
            return left

    if ":" in base:
        left = base.split(":", 1)[0].strip()
        if 2 <= len(left) <= 90:
            return left

    # fallback: first 6 words
    words = base.split()
    if len(words) >= 2:
        return " ".join(words[:6]).strip()

    return None


def _fuzzy_name_to_ticker(company_name: str, user_agent: str) -> Optional[str]:
    """
    Fuzzy match company name against SEC company names.
    Works only if ticker already exists (often not true pre-IPO).
    """
    rows = _load_sec_ticker_map(user_agent)
    target = _normalize_name(company_name)
    if not target or len(target) < 3:
        return None

    norm_names = [_normalize_name(r["name"]) for r in rows]
    matches = difflib.get_close_matches(target, norm_names, n=1, cutoff=0.88)
    if not matches:
        return None

    best_norm = matches[0]
    idx = norm_names.index(best_norm)
    return rows[idx]["ticker"]


def guess_ticker_from_text(title: str, user_agent: str) -> Optional[str]:
    """
    Best-effort ticker extraction from headline text.
    1) (TICKER)
    2) NASDAQ:TICKER / NYSE:TICKER
    3) TICKER - NASDAQ
    4) Fuzzy company name -> ticker (if already listed)
    """
    m = _TICKER_PARENS_RE.search(title or "")
    if m:
        return m.group(1).upper()

    m = _TICKER_EXCH_RE.search(title or "")
    if m:
        return m.group(1).upper()

    m = _TICKER_DASH_RE.search(title or "")
    if m:
        return m.group(1).upper()

    cname = extract_company_name_from_headline(title)
    if cname:
        return _fuzzy_name_to_ticker(cname, user_agent)

    return None


# -----------------------------
# Ticker -> CIK
# -----------------------------
def ticker_to_cik10(ticker: str, user_agent: str) -> str:
    t = ticker.strip().upper()
    data = _sec_get(SEC_TICKER_MAP_URL, user_agent).json()
    for _, row in data.items():
        if (row.get("ticker") or "").upper() == t:
            return str(row["cik_str"]).zfill(10)
    raise ValueError(f"CIK not found for ticker: {ticker}")


# -----------------------------
# Company name -> CIK (pre-IPO fallback)
# -----------------------------
def guess_cik_from_company_name(company_name: str, user_agent: str) -> Optional[str]:
    """
    Uses SEC full-text search to map company name -> likely CIK.
    Works even when no ticker exists yet (pre-IPO).
    """
    name = (company_name or "").strip()
    if len(name) < 3:
        return None

    url = "https://efts.sec.gov/LATEST/search-index"
    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/json",
    }
    payload = {
        "q": name,
        "forms": ["S-1", "S-1/A", "F-1", "F-1/A", "424B4"],
        "from": 0,
        "size": 5,
        "sort": "date-desc",
    }

    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
    r.raise_for_status()
    data = r.json()

    hits = data.get("hits", {}).get("hits", [])
    if not hits:
        return None

    # best hit
    src = hits[0].get("_source", {})
    cik = src.get("cik")
    if cik is None:
        return None

    return str(cik).zfill(10)


# -----------------------------
# Company submissions + filing selection
# -----------------------------
def get_company_submissions(cik10: str, user_agent: str) -> Dict[str, Any]:
    url = f"https://data.sec.gov/submissions/CIK{cik10}.json"
    return _sec_get(url, user_agent).json()


def pick_latest_ipo_filing(submissions_json: Dict[str, Any]) -> Optional[Dict[str, str]]:
    recent = submissions_json.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])

    filings = []
    for form, acc, dt, doc in zip(forms, accessions, dates, primary_docs):
        filings.append({"form": form, "accession": acc, "date": dt, "primary_doc": doc})

    filings.sort(key=lambda x: x["date"], reverse=True)

    for preferred_form in IPO_FORMS_PRIORITY:
        for f in filings:
            if f["form"] == preferred_form:
                return f

    return filings[0] if filings else None


def filing_primary_doc_url(cik10: str, accession: str, primary_doc: str) -> str:
    acc_nodashes = accession.replace("-", "")
    cik_int = int(cik10)
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodashes}/{primary_doc}"


def download_filing_html(cik10: str, accession: str, primary_doc: str, user_agent: str) -> str:
    url = filing_primary_doc_url(cik10, accession, primary_doc)
    return _sec_get(url, user_agent).text


# -----------------------------
# Extract last private price/share (best-effort)
# -----------------------------
def _parse_float_money(token: str) -> Optional[float]:
    try:
        return float(token.replace(",", ""))
    except Exception:
        return None


def extract_last_private_round_price(html: str) -> Dict[str, Any]:
    """
    Best-effort heuristic extraction:
    - scan text lines for: '$X' + 'per share'
    - prefer lines mentioning 'preferred'/'series' and recent sales language
    - choose the last candidate in document order (often latest disclosure)
    """
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n", strip=True)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    candidates: list[Tuple[float, str, float, str]] = []  # price, snippet, confidence, rationale

    for ln in lines:
        if not _MONEY_RE.search(ln):
            continue
        if not _PER_SHARE_RE.search(ln):
            continue

        has_pref = bool(_PREFERRED_RE.search(ln))
        has_common = bool(_COMMON_RE.search(ln))

        monies = []
        for m in _MONEY_RE.finditer(ln):
            val = _parse_float_money(m.group(1))
            if val is None:
                continue
            monies.append(val)

        # plausible per-share values
        monies = [v for v in monies if 0.01 < v < 500]
        if not monies:
            continue

        price = min(monies)

        confidence = 0.35
        rationale_bits = []

        if has_pref:
            confidence += 0.30
            rationale_bits.append("mentions preferred/series")
        if _RECENT_SALES_CLUES.search(ln):
            confidence += 0.20
            rationale_bits.append("recent sales/sold/issued clue")
        if "conversion" in ln.lower():
            confidence += 0.05
            rationale_bits.append("mentions conversion")
        if has_common and not has_pref:
            confidence -= 0.10
            rationale_bits.append("common mention without preferred")

        confidence = max(0.05, min(confidence, 0.95))
        rationale = ", ".join(rationale_bits) if rationale_bits else "per-share $ found"

        candidates.append((price, ln[:500], confidence, rationale))

    if not candidates:
        return {
            "price": None,
            "currency": "USD",
            "confidence": 0.0,
            "snippet": None,
            "rationale": "no preferred-per-share candidates found",
        }

    price, snippet, confidence, rationale = candidates[-1]
    return {
        "price": price,
        "currency": "USD",
        "confidence": confidence,
        "snippet": snippet,
        "rationale": rationale,
    }


def compute_step_change(
    private_price: float,
    ipo_low: Optional[float] = None,
    ipo_high: Optional[float] = None,
    ipo_final: Optional[float] = None,
) -> Dict[str, float]:
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


# -----------------------------
# Main entry points
# -----------------------------
def edgar_private_price_analysis(
    ticker: str,
    user_agent: str,
    ipo_low: Optional[float] = None,
    ipo_high: Optional[float] = None,
    ipo_final: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Start from ticker.
    """
    ticker_u = ticker.strip().upper()
    try:
        cik10 = ticker_to_cik10(ticker_u, user_agent)
        return edgar_private_price_analysis_from_cik(
            cik10,
            user_agent,
            ticker=ticker_u,
            ipo_low=ipo_low,
            ipo_high=ipo_high,
            ipo_final=ipo_final,
        )
    except Exception as e:
        return {
            "ticker": ticker_u,
            "error": str(e),
            "last_private_round_price_per_share": None,
        }


def edgar_private_price_analysis_from_company(
    company_name: str,
    user_agent: str,
    ipo_low: Optional[float] = None,
    ipo_high: Optional[float] = None,
    ipo_final: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Start from a company name (pre-IPO friendly).
    """
    cname = (company_name or "").strip()
    try:
        cik10 = guess_cik_from_company_name(cname, user_agent)
        if not cik10:
            return {
                "company_name": cname,
                "error": "Could not match company name to a CIK via SEC search",
                "last_private_round_price_per_share": None,
            }
        return edgar_private_price_analysis_from_cik(
            cik10,
            user_agent,
            company_name=cname,
            ipo_low=ipo_low,
            ipo_high=ipo_high,
            ipo_final=ipo_final,
        )
    except Exception as e:
        return {
            "company_name": cname,
            "error": str(e),
            "last_private_round_price_per_share": None,
        }


def edgar_private_price_analysis_from_cik(
    cik10: str,
    user_agent: str,
    ticker: Optional[str] = None,
    company_name: Optional[str] = None,
    ipo_low: Optional[float] = None,
    ipo_high: Optional[float] = None,
    ipo_final: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Start from a known CIK.
    """
    subs = get_company_submissions(cik10, user_agent)
    filing = pick_latest_ipo_filing(subs)

    if not filing:
        return {
            "ticker": ticker,
            "company_name": company_name,
            "cik": cik10,
            "error": "No IPO-related filings found for this CIK",
            "last_private_round_price_per_share": None,
        }

    form = filing["form"]
    accession = filing["accession"]
    filing_date = filing["date"]
    primary_doc = filing["primary_doc"]
    filing_url = filing_primary_doc_url(cik10, accession, primary_doc)

    html = download_filing_html(cik10, accession, primary_doc, user_agent)
    extracted = extract_last_private_round_price(html)

    step = {}
    if extracted["price"] is not None and any(v is not None for v in [ipo_low, ipo_high, ipo_final]):
        step = compute_step_change(extracted["price"], ipo_low=ipo_low, ipo_high=ipo_high, ipo_final=ipo_final)

    return {
        "ticker": ticker,
        "company_name": company_name,
        "cik": cik10,
        "filing_form_used": form,
        "filing_accession": accession,
        "filing_date": filing_date,
        "filing_url": filing_url,
        "last_private_round_price_per_share": extracted["price"],
        "currency": extracted["currency"],
        "extraction_confidence": extracted["confidence"],
        "extraction_rationale": extracted["rationale"],
        "supporting_snippet": extracted["snippet"],
        "step_up_down_pct": step,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }

def extract_relevant_ipo_sections(html: str) -> str:
    """
    Pull text with a bias toward sections that usually contain recent private round prices.
    Keeps size small for the LLM.
    """
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n", strip=True)

    # Try to capture around keywords that commonly include the relevant info
    keywords = [
        "Recent Sales of Unregistered Securities",
        "Dilution",
        "Capitalization",
        "Private Placement",
        "preferred stock",
        "price per share",
        "stockholders' equity",
    ]

    lines = text.split("\n")
    out = []
    for i, ln in enumerate(lines):
        for kw in keywords:
            if kw.lower() in ln.lower():
                start = max(0, i - 20)
                end = min(len(lines), i + 180)
                out.append("\n".join(lines[start:end]))
                break

    # Fallback: first chunk
    if not out:
        out = ["\n".join(lines[:600])]

    # De-dupe chunks
    seen = set()
    uniq = []
    for c in out:
        c2 = c.strip()
        if c2 and c2 not in seen:
            seen.add(c2)
            uniq.append(c2)

    return "\n\n---\n\n".join(uniq)[:12000]

from src.ai import ai_parse_edgar_last_private_round

def edgar_private_price_analysis_ai_from_cik(cik10: str, user_agent: str, company_name: str | None = None, ticker: str | None = None) -> dict:
    subs = get_company_submissions(cik10, user_agent)
    filing = pick_latest_ipo_filing(subs)
    if not filing:
        return {"error": "No IPO-related filings found", "last_private_round_price_per_share": None}

    html = download_filing_html(cik10, filing["accession"], filing["primary_doc"], user_agent)
    relevant_text = extract_relevant_ipo_sections(html)

    filing_url = filing_primary_doc_url(cik10, filing["accession"], filing["primary_doc"])
    context = {"company_name": company_name, "ticker": ticker, "filing_url": filing_url, "form": filing["form"], "filing_date": filing["date"]}

    ai = ai_parse_edgar_last_private_round(relevant_text, context)

    return {
        "ticker": ticker,
        "company_name": company_name,
        "cik": cik10,
        "filing_form_used": filing["form"],
        "filing_accession": filing["accession"],
        "filing_date": filing["date"],
        "filing_url": filing_url,
        "last_private_round_price_per_share": ai.get("last_private_round_price_per_share"),
        "currency": ai.get("currency", "USD"),
        "extraction_confidence": ai.get("confidence", 0.0),
        "supporting_snippet": ai.get("supporting_quote"),
        "extraction_rationale": ai.get("reasoning"),
        "ai_reasoning": ai.get("reasoning"),
    }
