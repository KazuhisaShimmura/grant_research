#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
都道府県公式ページ（医療/介護/省エネ系）から更新日とPDFリンクを抽出し JSONL 出力
"""
import argparse
import json
import hashlib
import time

from urllib.parse import urljoin
import yaml
import requests
from bs4 import BeautifulSoup


def read_cfg(pref):
    path = f"config/sources_{pref}.yml"
    return yaml.safe_load(open(path, "r", encoding="utf-8"))


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def scrape_page(base_url, selectors):
    r = requests.get(base_url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    updated = None
    if selectors.get("updated_at"):
        cand = soup.select_one(selectors["updated_at"])
        if cand:
            updated = cand.get_text(strip=True)
    links = []
    for a in soup.select(selectors.get("links", "a")):
        href = a.get("href")
        if not href:
            continue
        url = urljoin(base_url, href)
        text = a.get_text(" ", strip=True)
        is_pdf = (
            ".pdf" in href.lower()) or (
            "pdf" in (
                selectors.get("pdf_hint") or ""))
        links.append({"url": url, "text": text, "is_pdf": is_pdf})
    return updated, links


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pref", required=True, choices=["tokyo", "kanagawa"])
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    meta = read_cfg(args.pref)
    rows = []
    for page in meta.get("pages", []):
        updated, links = scrape_page(page["url"], page["selectors"])
        rows.append({
            "publisher": meta.get("publisher"),
            "geography": meta.get("geography_code"),
            "source_url": page["url"],
            "page_name": page["name"],
            "updated_hint": updated,
            "links": links,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S")
        })
    with open(args.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Wrote {len(rows)} rows to {args.out}")


if __name__ == "__main__":
    main()
