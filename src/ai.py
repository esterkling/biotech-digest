# src/ai.py
from __future__ import annotations

import os
import json
import time
from typing import Any, Dict, List

import requests

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
MODEL = "llama-3.1-8b-instant"  # free-tier friendly


# ----------------------------
# Core Groq client (OpenAI-compatible)
# ----------------------------
def _groq_chat(
    messages: List[Dict[str, str]],
    *,
    max_tokens: int = 1200,
    temperature: float = 0.1,
    timeout_s: int = 60,
) -> str:
    api_key = os.environ["GROQ_API_KEY"]

    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    def _post() -> requests.Response:
        return requests.post(
            f"{GROQ_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout_s,
        )

    r = _post()
    if r.status_code == 429:
        time.sleep(3)
        r = _post()

    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def _parse_json_strict(text: str) -> Any:
    """
    Models sometimes return extra text before/after JSON.
    This extracts the first JSON object/array.
    """
    text = (text or "").strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    start = None
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start is None:
        raise ValueError(f"No JSON start found in model response: {text[:200]}")

    end = None
    for j in range(len(text) - 1, -1, -1):
        if text[j] in "}]":
            end = j + 1
            break
    if end is None or end <= start:
        raise ValueError(f"No JSON end found in model response: {text[:200]}")

    return json.loads(text[start:end])


# ----------------------------
# A) AI CLUSTERING (DEDUPE) - batch
# ----------------------------
def ai_cluster_headlines(items: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Input items: [{id:int, title:str, url:str, source:str}, ...]
    Output:
      {"clusters":[{"cluster_id":1,"item_ids":[...],"representative_id":...,"label":"..."}]}
    """
    system = {"role": "system", "content": "Return valid JSON only. No markdown."}
    user = {
        "role": "user",
        "content": (
            "Cluster these news items so that items about the SAME underlying event are grouped together.\n"
            "Use ONLY titles and sources. Be conservative (don't over-merge).\n"
            "Return JSON only with schema:\n"
            "{clusters:[{cluster_id:int,item_ids:[int],representative_id:int,label:string}]}\n\n"
            "Items:\n"
            + "\n".join([f"{it['id']}\t{it.get('source','')}\t{it['title']}" for it in items])
        ),
    }

    txt = _groq_chat([system, user], max_tokens=1600, temperature=0.0)
    data = _parse_json_strict(txt)

    clusters = data.get("clusters") if isinstance(data, dict) else None
    if not isinstance(clusters, list):
        return {"clusters": []}

    cleaned = []
    for idx, c in enumerate(clusters, start=1):
        if not isinstance(c, dict):
            continue
        item_ids = c.get("item_ids", [])
        rep_id = c.get("representative_id", None)
        if rep_id is None or not isinstance(item_ids, list) or not item_ids:
            continue
        cleaned.append(
            {
                "cluster_id": int(c.get("cluster_id") or idx),
                "item_ids": [int(x) for x in item_ids if isinstance(x, int) or str(x).isdigit()],
                "representative_id": int(rep_id),
                "label": str(c.get("label") or "")[:120],
            }
        )

    return {"clusters": cleaned}


# ----------------------------
# B) AI EXTRACTION + CATEGORY - batch
# ----------------------------
def ai_extract_structured(items: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Input: [{id,title,url,source,snippet},...]

    Output:
      {"items":[{id, category, one_line_summary, vc_takeaway, materiality, ...}, ...]}
    """
    system = {"role": "system", "content": "Return valid JSON only. No markdown."}
    user = {
        "role": "user",
        "content": (
            "You are preparing a biotech VC morning digest.\n"
            "For each item, extract structured fields from TITLE + SNIPPET (snippet may be empty).\n"
            "Do NOT invent facts not supported by snippet/title. If unknown, use null/empty.\n\n"
            "Return JSON only with schema {items:[...]}.\n"
            "Categories must be one of:\n"
            "Financings; IPOs/Public markets; M&A/Licensing; Clinical readouts/Safety; "
            "FDA/EMA Regulatory; Pharma/Big biotech; Nordic/European biotech; Other\n\n"
            "Rules:\n"
            "- materiality: low/medium/high (VC relevance)\n"
            "- one_line_summary: exactly 1 sentence\n"
            "- vc_takeaway: exactly 1 sharp sentence\n\n"
            "Items:\n"
            + "\n\n".join(
                [
                    f"ID: {it['id']}\nSOURCE: {it.get('source','')}\nTITLE: {it['title']}\nSNIPPET: {it.get('snippet','')}"
                    for it in items
                ]
            )
        ),
    }

    txt = _groq_chat([system, user], max_tokens=2200, temperature=0.2)
    data = _parse_json_strict(txt)

    items_out = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items_out, list):
        return {"items": []}

    allowed_cats = {
        "Financings",
        "IPOs/Public markets",
        "M&A/Licensing",
        "Clinical readouts/Safety",
        "FDA/EMA Regulatory",
        "Pharma/Big biotech",
        "Nordic/European biotech",
        "Other",
    }

    cleaned = []
    for x in items_out:
        if not isinstance(x, dict):
            continue
        try:
            _id = int(x.get("id"))
        except Exception:
            continue

        cat = x.get("category") or "Other"
        if cat not in allowed_cats:
            cat = "Other"

        mat = (x.get("materiality") or "medium").lower()
        if mat not in ("low", "medium", "high"):
            mat = "medium"

        cleaned.append(
            {
                "id": _id,
                "category": cat,
                "companies": x.get("companies") or [],
                "counterparties": x.get("counterparties") or [],
                "event_type": x.get("event_type"),
                "amounts": x.get("amounts") or {},
                "stage": x.get("stage"),
                "indication": x.get("indication"),
                "modality_or_target": x.get("modality_or_target"),
                "phase": x.get("phase"),
                "regulator": x.get("regulator"),
                "geography": x.get("geography"),
                "materiality": mat,
                "one_line_summary": x.get("one_line_summary") or "",
                "vc_takeaway": x.get("vc_takeaway") or "",
            }
        )

    return {"items": cleaned}


# ----------------------------
# Compatibility single-item helper
# ----------------------------
def ai_summarize_takeaway(title: str, url: str, category: str, article_text: str) -> dict:
    """
    Backwards compatible wrapper: returns summary + vc_takeaway + materiality.
    """
    snippet = (article_text or "").strip().replace("\n", " ")
    snippet = snippet[:1200]

    out = ai_extract_structured(
        [{"id": 0, "title": title, "url": url, "source": "", "snippet": snippet}]
    )
    items = out.get("items", [])
    if not items:
        return {"summary": "", "vc_takeaway": "", "materiality": "medium"}

    x = items[0]
    return {
        "summary": (x.get("one_line_summary") or "").strip(),
        "vc_takeaway": (x.get("vc_takeaway") or "").strip(),
        "materiality": (x.get("materiality") or "medium").strip(),
    }


# ----------------------------
# IPO / EDGAR AI parser (needed by src/edgar.py)
# ----------------------------
def ai_parse_edgar_last_private_round(filing_text: str, context: dict) -> dict:
    """
    Parse an S-1/F-1/424B4 excerpt and identify the last private preferred financing round price/share.
    Returns:
      {
        last_private_round_price_per_share: number|null,
        currency: "USD",
        round_date: string|null,
        security: string|null,
        supporting_quote: string,
        confidence: 0..1,
        reasoning: string
      }
    """
    system = {"role": "system", "content": "Return valid JSON only. No markdown."}
    user = {
        "role": "user",
        "content": f"""
You are reading an IPO registration statement excerpt (S-1/F-1/424B4).
Goal: identify the LAST private preferred financing round price per share (not option exercises, not conversions unless clearly the priced round).

Return STRICT JSON with keys:
- last_private_round_price_per_share (number or null)
- currency ("USD" if unknown)
- round_date (string or null)
- security (string or null)  // e.g. "Series C Preferred"
- supporting_quote (string)  // exact quote used (<= 40 words)
- confidence (number from 0 to 1)
- reasoning (string) // 1 short sentence

If not enough info, set price to null and lower confidence.

Context (may be partial): {json.dumps(context)}

Filing text (may be truncated):
\"\"\"{(filing_text or '')[:9000]}\"\"\"
""".strip(),
    }

    txt = _groq_chat([system, user], max_tokens=700, temperature=0.1)
    return _parse_json_strict(txt)
