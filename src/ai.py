# src/ai.py
import os
import json
import time
import requests
from typing import List, Dict, Any

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
# Free-tier friendly and fast; you can switch to a larger model if you have headroom.
MODEL = "llama-3.1-8b-instant"

def _groq_chat(messages: list[dict], max_tokens: int = 1200, temperature: float = 0.1) -> str:
    api_key = os.environ["GROQ_API_KEY"]
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    r = requests.post(
        f"{GROQ_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )

    # If you hit rate limits on free tier, Groq returns 429 and includes headers.
    # Docs: rate limits and OpenAI-compat endpoint.   [oai_citation:1‡GroqCloud](https://console.groq.com/docs/rate-limits?utm_source=chatgpt.com)
    if r.status_code == 429:
        # Back off briefly and try once more
        time.sleep(3)
        r = requests.post(
            f"{GROQ_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )

    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def _parse_json_strict(text: str) -> Any:
    """
    Models sometimes prepend text; this pulls the first JSON object/array.
    """
    text = text.strip()
    # Try direct
    try:
        return json.loads(text)
    except Exception:
        pass

    # Find first {...} or [...]
    start = None
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start is None:
        raise ValueError("No JSON found in model response")

    # Find last matching end
    end = None
    for j in range(len(text) - 1, -1, -1):
        if text[j] in "}]":
            end = j + 1
            break
    if end is None:
        raise ValueError("No JSON end found in model response")

    return json.loads(text[start:end])

# ----------------------------
# A) AI CLUSTERING (DEDUPE)
# ----------------------------
def ai_cluster_headlines(items: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Input: [{id, title, url, source}, ...]
    Output JSON:
      {
        "clusters":[
           {"cluster_id":1, "item_ids":[0,3,7], "representative_id":0, "label":"Gilead deal ..."},
           ...
        ]
      }
    """
    user = {
        "role": "user",
        "content": (
            "Cluster these news items so that items about the SAME underlying event are grouped together.\n"
            "Use ONLY titles and sources. Be conservative (don't over-merge).\n"
            "Return JSON only with schema:\n"
            "{clusters:[{cluster_id:int,item_ids:[int],representative_id:int,label:string}]}\n\n"
            "Items:\n" +
            "\n".join([f"{it['id']}\t{it.get('source','')}\t{it['title']}" for it in items])
        ),
    }
    system = {"role": "system", "content": "Return valid JSON only. No markdown."}
    txt = _groq_chat([system, user], max_tokens=1600, temperature=0.0)
    return _parse_json_strict(txt)

# ----------------------------
# B) AI EXTRACTION + CATEGORY
# ----------------------------
def ai_extract_structured(items: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Input: [{id, title, url, source, snippet}, ...]
    Output JSON:
    {
      "items":[
        {
          "id": int,
          "category": one of [
            "Financings","IPOs/Public markets","M&A/Licensing","Clinical readouts/Safety",
            "FDA/EMA Regulatory","Pharma/Big biotech","Nordic/European biotech","Other"
          ],
          "companies":[...],
          "counterparties":[...],
          "event_type": "...",
          "amounts": {"upfront": "...", "total": "...", "raise": "..."} ,
          "stage": "...",
          "indication": "...",
          "modality_or_target": "...",
          "phase": "...",
          "regulator": "...",
          "geography": "...",
          "materiality": "low|medium|high",
          "one_line_summary": "...",
          "vc_takeaway": "..."
        },...
      ]
    }
    """
    user = {
        "role": "user",
        "content": (
            "You are preparing a biotech VC daily digest.\n"
            "For each item, extract structured fields from the title + snippet.\n"
            "If unknown, use null or empty.\n"
            "Return JSON only with schema {items:[...]}.\n"
            "Categories must be one of:\n"
            "Financings; IPOs/Public markets; M&A/Licensing; Clinical readouts/Safety; "
            "FDA/EMA Regulatory; Pharma/Big biotech; Nordic/European biotech; Other\n\n"
            "Items:\n" +
            "\n\n".join([
                f"ID: {it['id']}\nSOURCE: {it.get('source','')}\nTITLE: {it['title']}\nSNIPPET: {it.get('snippet','')}"
                for it in items
            ])
        ),
    }
    system = {"role": "system", "content": "Return valid JSON only. No markdown."}
    txt = _groq_chat([system, user], max_tokens=2200, temperature=0.2)
    return _parse_json_strict(txt)
