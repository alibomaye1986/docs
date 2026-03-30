#!/usr/bin/env python3
"""
find_leads.py - Search DuckDuckGo for notary outreach leads.

Usage:
    python3 find_leads.py "Dallas" "TX"
"""

import sys
import os
import csv
import json
import shutil
import time
import re
import argparse
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

# ── Paths ─────────────────────────────────────────────────────────────────────

LEADS_CSV = Path("/root/.picoclaw/workspace/state/outreach/leads.csv")
CSV_HEADERS = ["created_at", "name", "firm", "email", "city", "state", "type", "notes", "status"]

# Ordered by outreach priority
TARGET_TYPES = [
    "probate attorney",
    "immigration lawyer",
    "solo practice attorney",
    "new law firm",
    "tech-friendly law firm",
    "estate planning attorney",
    "family law attorney",
    "general practice law firm",
]

DDGO_URL = "https://html.duckduckgo.com/html/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


# ── CSV helpers ───────────────────────────────────────────────────────────────

def ensure_csv() -> set:
    """Create CSV with headers if needed; return set of existing firm names.

    Also migrates existing CSVs that are missing the 'type' column by
    rewriting them with the new header and an empty value for that field.
    """
    LEADS_CSV.parent.mkdir(parents=True, exist_ok=True)
    existing_firms: set = set()

    if not LEADS_CSV.exists():
        with open(LEADS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()
        return existing_firms

    with open(LEADS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        existing_cols = reader.fieldnames or []

    # Migrate: rewrite if any expected column is missing
    if set(CSV_HEADERS) != set(existing_cols):
        tmp = LEADS_CSV.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in CSV_HEADERS})
        shutil.move(str(tmp), str(LEADS_CSV))
        print("  [info] Migrated leads.csv to include new 'type' column.")

    for row in rows:
        firm = (row.get("firm") or "").strip().lower()
        if firm:
            existing_firms.add(firm)
    return existing_firms


def append_leads(leads: list[dict]) -> None:
    with open(LEADS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        for lead in leads:
            writer.writerow(lead)


# ── DuckDuckGo search ─────────────────────────────────────────────────────────

def ddg_search(query: str, max_results: int = 10) -> list[dict]:
    """
    Submit a query to DuckDuckGo HTML endpoint and parse result snippets.
    Returns a list of dicts with keys: title, url, snippet.
    """
    results = []
    try:
        resp = requests.post(
            DDGO_URL,
            data={"q": query, "b": "", "kl": "us-en"},
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        html = resp.text

        # Extract result blocks between <div class="result"> tags (rough parse)
        # DDG HTML structure: result titles in <a class="result__a">, snippets in <a class="result__snippet">
        title_pat = re.compile(
            r'class="result__a"[^>]*>\s*(.*?)\s*</a>', re.DOTALL
        )
        url_pat = re.compile(
            r'class="result__url"[^>]*>\s*(.*?)\s*</[^>]+>', re.DOTALL
        )
        snippet_pat = re.compile(
            r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL
        )

        titles = [re.sub(r"<[^>]+>", "", t).strip() for t in title_pat.findall(html)]
        urls = [u.strip() for u in url_pat.findall(html)]
        snippets = [re.sub(r"<[^>]+>", "", s).strip() for s in snippet_pat.findall(html)]

        count = min(len(titles), max_results)
        for i in range(count):
            results.append(
                {
                    "title": titles[i] if i < len(titles) else "",
                    "url": urls[i] if i < len(urls) else "",
                    "snippet": snippets[i] if i < len(snippets) else "",
                }
            )
    except Exception as exc:
        print(f"  [warn] DDG search failed for '{query}': {exc}")
    return results


# ── Lead extraction ───────────────────────────────────────────────────────────

def extract_email(text: str) -> str:
    match = EMAIL_RE.search(text)
    return match.group(0) if match else ""


def build_lead(result: dict, city: str, state: str, target_type: str) -> dict:
    title = result["title"]
    snippet = result["snippet"]
    combined = f"{title} {snippet} {result['url']}"

    email = extract_email(combined)
    firm = title.strip() or "Unknown"
    # Strip trailing " - " separators often appended by search engines
    firm = re.sub(r"\s*[-|–]\s*.*$", "", firm).strip()

    notes = f"Found via DDG: {result['url']}"
    if len(notes) > 255:
        notes = notes[:252] + "..."

    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "name": "",
        "firm": firm,
        "email": email,
        "city": city,
        "state": state,
        "type": target_type,
        "notes": notes,
        "status": "new",
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search DuckDuckGo for notary outreach leads."
    )
    parser.add_argument("city", help='City name, e.g. "Dallas"')
    parser.add_argument("state", help='State abbreviation, e.g. "TX"')
    args = parser.parse_args()

    city = args.city.strip()
    state = args.state.strip().upper()

    print(f"Searching for leads in {city}, {state} …\n")

    existing_firms = ensure_csv()
    new_leads: list[dict] = []
    skipped = 0

    for target_type in TARGET_TYPES:
        query = f"{target_type} {city} {state}"
        print(f"  Querying: {query}")
        results = ddg_search(query, max_results=10)
        time.sleep(2)  # polite crawl delay

        for result in results:
            if not result["title"]:
                continue
            lead = build_lead(result, city, state, target_type)
            firm_key = lead["firm"].lower().strip()
            if not firm_key or firm_key == "unknown":
                continue
            if firm_key in existing_firms:
                skipped += 1
                continue
            existing_firms.add(firm_key)
            new_leads.append(lead)

    if new_leads:
        append_leads(new_leads)

    print(f"\nDone. {len(new_leads)} new leads added, {skipped} skipped as duplicates.")
    print(f"Leads file: {LEADS_CSV}")


if __name__ == "__main__":
    main()
