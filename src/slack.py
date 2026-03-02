import requests

def post(webhook_url: str, text: str):
    r = requests.post(webhook_url, json={"text": text}, timeout=30)
    r.raise_for_status()
