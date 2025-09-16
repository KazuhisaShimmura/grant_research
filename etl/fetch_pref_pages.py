import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def available_prefs() -> List[str]:
    prefs: List[str] = []
    for path in CONFIG_DIR.glob("sources_*.yml"):
        stem = path.stem
        if stem == "sources_common":
            continue
        pref = stem.replace("sources_", "", 1).strip()
        if pref:
            prefs.append(pref)
    return sorted(set(prefs))


SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "eucalia-pref-scraper/1.0"})
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=0.5, status_forcelist=[429,500,502,503,504]
)))

def read_cfg(pref):
    path = CONFIG_DIR / f"sources_{pref}.yml"
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def is_pdf_link(a_tag, href, selectors):
    href = (href or "").lower()
    if href.endswith(".pdf"):
        return True
    # テキスト/クラスにヒントがある場合
    hint = selectors.get("pdf_hint")
    if hint:
        pat = re.compile(hint, re.I)
        txt = a_tag.get_text(" ", strip=True)
        classes = " ".join(a_tag.get("class") or [])
        if pat.search(txt) or pat.search(classes):
            return True
    # 最後の砦：HEADでContent-Type確認（コスト注意）
    # try:
    #     h = SESSION.head(urljoin(base_url, href), timeout=15, allow_redirects=True)
    #     if h.headers.get("Content-Type","").lower().startswith("application/pdf"):
    #         return True
    # except Exception:
    #     pass
    return False

def parse_updated(text):
    if not text:
        return None, None
    raw = text.strip()
    # 例：2025年9月1日 / 2025/09/01 / 令和7年9月1日 など簡易正規化
    raw2 = re.sub(r"[年月]", "/", raw).replace("日","").strip()
    try:
        dt = datetime.fromisoformat(raw2)
    except Exception:
        try:
            dt = datetime.strptime(raw2, "%Y/%m/%d")
        except Exception:
            dt = None
    iso = dt.replace(tzinfo=timezone.utc).isoformat() if dt else None
    return raw, iso

def scrape_page(base_url, selectors):
    r = SESSION.get(base_url, timeout=30)
    r.encoding = r.apparent_encoding or r.encoding
    r.raise_for_status()
    try:
        soup = BeautifulSoup(r.text, "lxml")
    except Exception:
        soup = BeautifulSoup(r.text, "html.parser")

    updated_raw = None
    updated_iso = None
    if selectors.get("updated_at"):
        cand = soup.select_one(selectors["updated_at"])
        if cand:
            updated_raw, updated_iso = parse_updated(cand.get_text(" ", strip=True))

    link_sel = selectors.get("links", "a")
    out_links = []
    for a in soup.select(link_sel):
        href = a.get("href")
        if not href:
            continue
        url = urljoin(base_url, href)
        text = a.get_text(" ", strip=True)
        is_pdf = is_pdf_link(a, href, selectors)
        if is_pdf:
            out_links.append({"url": url, "text": text})

    return updated_raw, updated_iso, out_links


def parse_args():
    parser = argparse.ArgumentParser(description="Scrape prefectural grant pages defined in config/sources_*.yml")
    parser.add_argument("--out", required=True, help="出力JSONLファイルパス")
    parser.add_argument("--prefs", nargs="*", default=None,
                        help="取得対象の都道府県コード（例: tokyo kanagawa）。未指定時は全件")
    parser.add_argument("--sleep", type=float, default=0.2,
                        help="ページ間スリープ秒（デフォルト0.2s）")
    return parser.parse_args()


def ensure_output_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def main():
    args = parse_args()
    prefs = args.prefs or available_prefs()

    if not prefs:
        print("[ERROR] No prefecture configs found.", file=sys.stderr)
        return

    rows: List[Dict] = []
    for pref in prefs:
        try:
            meta = read_cfg(pref)
        except FileNotFoundError:
            print(f"[WARN] config not found for pref='{pref}'", file=sys.stderr)
            continue
        except yaml.YAMLError as e:
            print(f"[WARN] failed to parse config for pref='{pref}': {e}", file=sys.stderr)
            continue

        publisher = meta.get("publisher")
        geography = meta.get("geography_code") or meta.get("geography")
        for page in meta.get("pages", []):
            url = page.get("url")
            if not url:
                continue
            selectors = page.get("selectors") or {}
            try:
                updated_raw, updated_iso, links = scrape_page(url, selectors)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] failed: {url} ({e})", file=sys.stderr)
                continue

            row = {
                "id": hashlib.sha256(f"{publisher}-{url}-{updated_iso}".encode("utf-8")).hexdigest(),
                "publisher": publisher,
                "geography": geography,
                "source_url": url,
                "page_name": page.get("name"),
                "updated_hint_raw": updated_raw,
                "updated_at": updated_iso,
                "pdf_links": links,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
            rows.append(row)

            if args.sleep and args.sleep > 0:
                time.sleep(args.sleep)

    if not rows:
        out_path = Path(args.out)
        ensure_output_dir(out_path)
        out_path.write_text("", encoding="utf-8")
        print(f"[INFO] wrote 0 rows to {out_path}")
        return

    unique = {}
    for row in rows:
        unique.setdefault(row["id"], row)

    ordered_rows = sorted(unique.values(), key=lambda r: (
        r.get("publisher") or "",
        r.get("page_name") or "",
        r.get("source_url") or "",
    ))

    out_path = Path(args.out)
    ensure_output_dir(out_path)
    with out_path.open("w", encoding="utf-8") as f:
        for row in ordered_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"[DONE] wrote {len(ordered_rows)} rows to {out_path}")


if __name__ == "__main__":
    main()
