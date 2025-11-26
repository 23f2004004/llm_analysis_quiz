import os
import json
import re
import time
import threading
from urllib.parse import urlparse, urljoin

import requests
from flask import Flask, request, jsonify
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

print(">> Booting quiz solver server", flush=True)

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))


# ---------------------------------------------------
# BROWSER INITIALIZATION
# ---------------------------------------------------

def build_driver():
    """Prepare a headless Chrome instance."""
    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.binary_location = os.getenv("CHROME_BIN", "/usr/bin/chromium")

    srv = Service(os.getenv("CHROMEDRIVER_PATH", "/usr/bin/chromedriver"))
    return webdriver.Chrome(service=srv, options=opts)


def render_dynamic_page(url):
    """Open website through Selenium for JS-heavy pages."""
    print(f"[Browser] Loading: {url}", flush=True)
    driver = build_driver()

    try:
        driver.get(url)
        time.sleep(3)
        text = driver.find_element(By.TAG_NAME, "body").text
        driver.quit()
        print(f"[Browser] Content preview: {text[:180]}", flush=True)
        return text

    except Exception as exc:
        print(f"[Browser] Failure: {exc}", flush=True)
        try: driver.quit()
        except: pass
        return ""
    

# ---------------------------------------------------
# RESOURCE SCRAPING
# ---------------------------------------------------

def sniff_related_urls(base, html_text, visible_text):
    """Identify extra files (links, csv, codes) to fetch."""
    discovered = {}
    combined = html_text + visible_text

    extraction_patterns = [
        r'href=["\']([^"\']+)',
        r'\bCSV[^"\']+href=["\']([^"\']+)',
        r'\bdownload[^ ]+\s+([^\s"\'<>]+)',
        r'\bScrape[^ ]+\s+([^\s"\'<>]+)'
    ]

    raw_urls = []
    for p in extraction_patterns:
        raw_urls.extend(re.findall(p, combined, re.IGNORECASE))

    unique = set()
    for link in raw_urls:
        if link.startswith("#") or link.startswith("javascript"):
            continue
        if link in unique:
            continue
        unique.add(link)

        if link.startswith("/"):
            root = urlparse(base)
            final = f"{root.scheme}://{root.netloc}{link}"
        elif link.startswith("http"):
            final = link
        else:
            final = urljoin(base, link)

        if "submit" in final.lower():
            continue

        try:
            print(f"[Fetch] Pulling: {final}", flush=True)
            res = requests.get(final, timeout=15)
            body = res.text

            if "<script" in body and len(body) < 600:
                print("[Fetch] JS page detected → switching to browser", flush=True)
                body = render_dynamic_page(final)

            discovered[final] = body[:12000]
            print(f"[Fetch] OK: {body[:200]}", flush=True)

        except Exception as exc:
            print(f"[Fetch] Error on {final}: {exc}", flush=True)

    return discovered


# ---------------------------------------------------
# CSV NUMERIC EXTRACTION
# ---------------------------------------------------

def csv_sum(data, threshold=None):
    """Collect all numeric values in CSV and add them."""
    try:
        nums = []
        for row in data.strip().split("\n"):
            for cell in row.split(","):
                try:
                    nums.append(float(cell.strip()))
                except:
                    pass

        if threshold is not None:
            nums = [n for n in nums if n > threshold]

        return sum(nums)

    except Exception as exc:
        print(f"[Calc] CSV parse error: {exc}", flush=True)
        return None


# ---------------------------------------------------
# QUIZ SOLVER CORE
# ---------------------------------------------------

def handle_quiz(url):
    print(f"[Solver] Visiting: {url}", flush=True)
    driver = build_driver()

    try:
        driver.get(url)
        time.sleep(3)
        page_text = driver.find_element(By.TAG_NAME, "body").text
        page_html = driver.page_source
        driver.quit()

        print(f"[Solver] Body preview: {page_text[:400]}", flush=True)

        assets = sniff_related_urls(url, page_html, page_text)

        cutoff_val = None
        c_match = re.search(r'[Cc]utoff[: ]+(\d+)', page_text)
        if c_match:
            cutoff_val = int(c_match.group(1))

        precomputed = None
        for link, body in assets.items():
            if ".csv" in link or any(ch.isdigit() for ch in body[:80]):
                precomputed = csv_sum(body, cutoff_val)

        appendix = ""
        if assets:
            appendix += "\n\n### RESOURCE DUMP\n"
            for lnk, txt in assets.items():
                appendix += f"\n[{lnk}]\n{txt}\n"

        if precomputed is not None:
            appendix += f"\n\n### NUMERIC RESULT\nSUM > cutoff({cutoff_val}) = {precomputed}"

        prompt = f"""
Extract the exact quiz answer.

--- PAGE TEXT ---
{page_text}

--- HTML (snippet) ---
{page_html[:5000]}

{appendix}

RULESET:
1. If a short “secret code” or token is present in the fetched resources, output exactly that.
2. If a precomputed numeric sum is included, that sum IS the answer.
3. Only return a JSON object like:
   {{"submit_url": "/submit", "answer": VALUE}}
4. Nothing except the JSON.
"""

        print("[Solver] Querying Groq...", flush=True)
        llm = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
