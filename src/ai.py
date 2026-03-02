# src/ai.py
from __future__ import annotations

import os
import json
import time
from typing import Any, Dict, List, Optional

import requests

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")


# ----------------------------
# HTTP client (OpenAI-compatible)
# ----------------------------
def _groq_chat(
    messages: List[Dict[str, str]],
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 1400,
    temperature: float = 0.2,
    timeout_s: int = 60,
    retries: int = 2,
) -> str:
    api_key = os.environ["GROQ_API_KEY"]

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                f"{GROQ_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout_s,
            )
            # basic backoff on rate limit / transient
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                r.raise_for_status()

            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            last_err = e
            time.sleep(1.0 * (attempt + 1))

    raise RuntimeError(f"Groq request failed after retries: {last_err}")


# ----------------------------
# Robust JSON extraction + parsing
# ----------------------------
def _extract_first_json_block(text: str) -> str:
    """
    Extract the first JSON object/array by bracket balancing.
    Handles extra text before/after JSON and braces inside strings.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Empty response")

    start = None
    opener = None
    for i, ch in enumerate(text):
        if ch == "{":
            start = i
            opener = "{"
            break
        if ch == "[":
            start = i
            opener = "["
            break

    if start is None:
        raise ValueError(f"No JSON start found. First 200 chars:\n{text[:200]}")

    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    escape = False

    for j in range(start, len(text)):
        ch = text[j]

        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            continue

        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : j + 1]

    raise ValueError("No balanced JSON block found.")


def _repair_json_with_groq(bad_json: str) -> str:
    system = {"role": "system", "content": "You fix JSON. Return ONLY strictly valid JSON. No commentary."}
    user = {
        "role": "user",
        "content": (
            "Fix this so it becomes strictly valid JSON. "
            "Do not change the meaning. Return JSON only.\n\n"
            f"{bad_json}"
        ),
    }
    return _groq_chat([system, user], max_tokens=1600, temperature=0.0)


def _parse_json_strict(text: str, *, repair: bool = True) -> Any:
    """
    Attempts in order:
      1) json.loads(full text)
      2) json.loads(balanced extracted block)
      3) repair extracted block with Groq
      4) repair full text with Groq (last resort)
    """
    text = (text or "").strip()

    # 1) direct parse
    try:
        return json.loads(text)
    except Exception:
        pass

    # 2) balanced block parse
    try:
        block = _extract_first_json_block(text)
        return json.loads(block)
    except Exception:
        if not repair:
            raise

    # 3) repair extracted block
    try:
        block = _extract_first_json_block(text)
        repaired = _repair_json_with_groq(block)
        repaired_block = _extract_first_json_block(repaired)
        return json.loads(repaired_block)
    except Exception:
        pass

    # 4) repair full response (truncate to keep it cheap)
    repaired_full = _repair_json_with_groq(text[:12000])
    repaired_full_block = _extract_first_json_block(repaired_full)
    return json.loads(repaired_full_block)


def _safe_dict(obj: Any, default: Optional[dict] = None) -> dict:
    if isinstance(obj, dict):
        return obj
    return default or {}


# ----------------------------
# A) Dedup clustering
# ----------------------------
def ai_cluster_headlines(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Input items: [{id:int, title:str, url:str, source:str}, ...]
    Output:
      {"clusters":[{"cluster_id":1,"item_ids":[...],"representative_id":...,"label":"..."}]}
    Safe: returns {"clusters": []} on failure.
    """
    system = {"role": "system", "content": "Return valid JSON only. No markdown. No commentary."}
    user = {
        "role": "user",
        "content": (
            "Cluster these news items so that items about the SAME underlying event are grouped together.\n"
            "Use ONLY titles and sources. Be conservative: only merge if clearly the same event.\n"
            "Return JSON only with schema:\n"
            "{clusters:[{cluster_id:int,item_ids:[int],representative_id:int,label:string}]}\n\n"
            "Items:\n"
            + "\n".join([f"{it['id']}\t{it.get('source','')}\t{it.get('title','')}" for it in items])
        ),
    }

    try:
        txt = _groq_chat([system, user], max_tokens=1700, temperature=0.0)
        data = _parse_json_strict(txt, repair=True)
        data = _safe_dict(data, {"clusters": []})
        clusters = data.get("clusters", [])
        if not isinstance(clusters, list):
            return {"clusters": []}

        cleaned = []
        for idx, c in enumerate(clusters, start=1):
            if not isinstance(c, dict):
                continue
            rep_id = c.get("representative_id", None)
            item_ids = c.get("item_ids", [])
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
    except Exception as e:
        print("AI clustering parse failed:", repr(e))
        return {"clusters": []}


# ----------------------------
# B) Structured extraction (category/summary/takeaway)
# ----------------------------
def ai_extract_structured(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Input: [{id,title,url,source,snippet},...]
    Output: {"items":[{id, category, one_line_summary, vc_takeaway, materiality, ...}, ...]}
    Safe: returns {"items": []} on failure.
    """
    system = {"role": "system", "content": "Return valid JSON only. No markdown. No commentary."}
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
            "Classification rules:\n"
            "- Analyst notes / price-target changes / stock-move commentary are NOT 'Financings' => use 'IPOs/Public markets'.\n"
            "- Put something in 'M&A/Licensing' ONLY if the transaction/partnership itself is the news (not merely mentioned).\n"
            "- Thought-leadership / marketing / event promotions => 'Other' with low materiality.\n\n"
            "Output rules:\n"
            "- materiality must be low/medium/high and reflect VC relevance.\n"
            "- one_line_summary MUST be exactly 1 sentence.\n"
            "- vc_takeaway MUST be exactly 1 sentence AND include one concrete angle specific to the story.\n"
            "  Examples: deal structure (upfront/milestones), label scope, safety signal type,\n"
            "  endpoint effect size, competitive landscape/comps, payer risk, manufacturing/CMC risk, next catalyst.\n"
            "  Avoid generic phrases like 'may impact' unless paired with a concrete catalyst.\n\n"
            "Items:\n"
            + "\n\n".join(
                [
                    f"ID: {it.get('id')}\nSOURCE: {it.get('source','')}\nTITLE: {it.get('title','')}\nSNIPPET: {it.get('snippet','')}"
                    for it in items
                ]
            )
        ),
    }

    try:
        txt = _groq_chat([system, user], max_tokens=2600, temperature=0.2)
        data = _parse_json_strict(txt, repair=True)
        data = _safe_dict(data, {"items": []})
        out_items = data.get("items", [])
        if not isinstance(out_items, list):
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
        for x in out_items:
            if not isinstance(x, dict) or "id" not in x:
                continue
            try:
                _id = int(x.get("id"))
            except Exception:
                continue

            cat = x.get("category") or "Other"
            if cat not in allowed_cats:
                cat = "Other"

            mat = (x.get("materiality") or "medium").strip().lower()
            if mat not in ("low", "medium", "high"):
                mat = "medium"

            cleaned.append(
                {
                    "id": _id,
                    "category": cat,
                    "materiality": mat,
                    "one_line_summary": (x.get("one_line_summary") or "").strip(),
                    "vc_takeaway": (x.get("vc_takeaway") or "").strip(),
                    # Optional fields (keep if present)
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
                }
            )

        return {"items": cleaned}
    except Exception as e:
        print("AI structured extraction failed:", repr(e))
        return {"items": []}


# ----------------------------
# Compatibility helper (single item)
# ----------------------------
def ai_summarize_takeaway(title: str, url: str, category: str, article_text: str) -> dict:
    snippet = (article_text or "").strip().replace("\n", " ")
    snippet = snippet[:1400]

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
# IPO / EDGAR AI parser (optional usage from src/edgar.py)
# ----------------------------
def ai_parse_edgar_last_private_round(filing_text: str, context: dict) -> dict:
    """
    Best-effort extraction of last private round price/share from S-1/F-1/424B4 excerpt.
    Returns dict with:
      last_private_round_price_per_share, currency, round_date, security, supporting_quote, confidence, reasoning
    Safe: returns null-ish fields on failure.
    """
    system = {"role": "system", "content": "Return valid JSON only. No markdown. No commentary."}
    user = {
        "role": "user",
        "content": f"""
You are reading an IPO registration statement excerpt (S-1/F-1/424B4).
Goal: identify the LAST private preferred financing round price per share.

Return STRICT JSON with keys:
- last_private_round_price_per_share (number or null)
- currency ("USD" if unknown)
- round_date (string or null)
- security (string or null)
- supporting_quote (string)  // <= 40 words
- confidence (number from 0 to 1)
- reasoning (string) // 1 short sentence

Context: {json.dumps(context)}

Filing text:
\"\"\"{(filing_text or '')[:9000]}\"\"\"
""".strip(),
    }

    try:
        txt = _groq_chat([system, user], max_tokens=800, temperature=0.1)
        data = _parse_json_strict(txt, repair=True)
        data = _safe_dict(
            data,
            {
                "last_private_round_price_per_share": None,
                "currency": "USD",
                "round_date": None,
                "security": None,
                "supporting_quote": "",
                "confidence": 0.0,
                "reasoning": "parse failed",
            },
        )

        # normalize
        lpr = data.get("last_private_round_price_per_share", None)
        try:
            if lpr is not None:
                lpr = float(lpr)
        except Exception:
            lpr = None

        conf = data.get("confidence", 0.0)
        try:
            conf = float(conf)
        except Exception:
            conf = 0.0
        conf = max(0.0, min(1.0, conf))

        return {
            "last_private_round_price_per_share": lpr,
            "currency": data.get("currency") or "USD",
            "round_date": data.get("round_date"),
            "security": data.get("security"),
            "supporting_quote": (data.get("supporting_quote") or "")[:240],
            "confidence": conf,
            "reasoning": (data.get("reasoning") or "")[:200],
        }
    except Exception as e:
        print("AI EDGAR parse failed:", repr(e))
        return {
            "last_private_round_price_per_share": None,
            "currency": "USD",
            "round_date": None,
            "security": None,
            "supporting_quote": "",
            "confidence": 0.0,
            "reasoning": "ai_parse_edgar_last_private_round failed",
        }
