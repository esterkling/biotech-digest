# src/extract.py
import requests
from bs4 import BeautifulSoup

def extract_article_text(url: str) -> str:
    try:
        r = requests.get(url, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")

        # Remove script/style
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        paragraphs = soup.find_all("p")
        text = "\n".join(p.get_text() for p in paragraphs)

        return text[:8000]  # limit size
    except Exception:
        return ""
