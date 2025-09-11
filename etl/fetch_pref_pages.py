from datetime import datetime, timezone
import re
from requests.adapters import HTTPAdapter, Retry

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "eucalia-pref-scraper/1.0"})
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=0.5, status_forcelist=[429,500,502,503,504]
)))

def read_cfg(pref):
    with open(f"config/sources_{pref}.yml", "r", encoding="utf-8") as f:
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

def main():
    ...
    for page in meta.get("pages", []):
        try:
            updated_raw, updated_iso, links = scrape_page(page["url"], page["selectors"])
        except Exception as e:
            print(f"[WARN] failed: {page['url']} ({e})")
            continue
        rows.append({
            "id": hashlib.sha256(f"{meta.get('publisher')}-{page['url']}-{updated_iso}".encode("utf-8")).hexdigest(),
            "publisher": meta.get("publisher"),
            "geography": meta.get("geography_code"),
            "source_url": page["url"],
            "page_name": page["name"],
            "updated_hint_raw": updated_raw,
            "updated_at": updated_iso,
            "pdf_links": links,  # PDFのみに絞る
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })
