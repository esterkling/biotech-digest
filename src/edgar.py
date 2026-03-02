import requests
from bs4 import BeautifulSoup
from datetime import datetime
import time

SEC_USER_AGENT = "BiotechDigest/1.0 (your@email.com)"

def get_cik_from_ticker(ticker):
    url = "https://www.sec.gov/files/company_tickers.json"
    headers = {"User-Agent": SEC_USER_AGENT}
    r = requests.get(url, headers=headers)
    data = r.json()
    for item in data.values():
        if item["ticker"].upper() == ticker.upper():
            return str(item["cik_str"]).zfill(10)
    return None

def get_latest_s1(cik):
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    headers = {"User-Agent": SEC_USER_AGENT}
    r = requests.get(url, headers=headers)
    filings = r.json()["filings"]["recent"]
    for form, acc, doc in zip(filings["form"], filings["accessionNumber"], filings["primaryDocument"]):
        if form in ["S-1", "S-1/A", "F-1", "F-1/A", "424B4"]:
            return acc, doc
    return None, None

def download_filing(cik, accession, doc):
    accession_no_dashes = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_no_dashes}/{doc}"
    headers = {"User-Agent": SEC_USER_AGENT}
    r = requests.get(url, headers=headers)
    return r.text

def main():
    ticker = "GENB"  # change when testing IPOs
    cik = get_cik_from_ticker(ticker)
    if not cik:
        print("Ticker not found")
        return
    
    accession, doc = get_latest_s1(cik)
    if not accession:
        print("No S-1 found")
        return

    html = download_filing(cik, accession, doc)
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text()

    print("---- DIGEST OUTPUT ----")
    print(f"Ticker: {ticker}")
    print(f"Filing: {accession}")
    print("Length of filing text:", len(text))
    print("Generated:", datetime.utcnow())

if __name__ == "__main__":
    main()
  def edgar_private_price_analysis(ticker: str, user_agent: str):
    try:
        cik = get_cik_from_ticker(ticker)
        accession, doc = get_latest_s1(cik)
        html = download_filing(cik, accession, doc)
        
        # your existing extraction logic here
        price = extract_last_private_price(html)

        return {
            "last_private_round_price_per_share": price,
            "extraction_confidence": 0.8
        }

    except Exception as e:
        return {
            "last_private_round_price_per_share": None,
            "extraction_confidence": 0.0,
            "error": str(e)
        }
