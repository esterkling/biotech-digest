# src/ai.py
import os
import requests
import json

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
MODEL = "llama-3.1-8b-instant"  # fast + free tier friendly

def ai_summarize_takeaway(title: str, url: str, category: str, article_text: str) -> dict:
    api_key = os.environ["GROQ_API_KEY"]

    prompt = f"""
You are a biotech venture capitalist writing a morning digest.

Return STRICT JSON with:
- summary (1-2 sentences)
- vc_takeaway (1 sharp sentence)
- materiality (low/medium/high)

Category: {category}
Headline: {title}
URL: {url}

Article text (may be truncated):
\"\"\"{article_text[:5000]}\"\"\"
"""

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "Return valid JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 400,
    }

    r = requests.post(
        f"{GROQ_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    r.raise_for_status()

    content = r.json()["choices"][0]["message"]["content"]

    return json.loads(content)
