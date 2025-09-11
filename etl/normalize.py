#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
取得データを共通スキーマに正規化
"""
import argparse, glob, json, re, pandas as pd, os, yaml
from datetime import datetime

SCHEMA_COLS = [
  "program_id","source_url","publisher","title","fiscal_year","domain",
  "eligibility_json","geography","subsidy_rate","subsidy_cap_jpy","budget_total_jpy",
  "cost_items_allowed","deadline_type","deadline_at","application_method",
  "requires_gbizid","docs_urls_json","status","published_at","last_seen_at","content_hash"
]

def norm_jgrants(rec):
    pid = rec.get("id") or rec.get("subsidyId")
    title = rec.get("title") or rec.get("subsidyTitle")
    pub = rec.get("publisherName") or rec.get("ministryName") or "jGrants掲載"
    url = rec.get("publicUrl") or rec.get("detailUrl") or ""
    deadline = rec.get("applicationDeadline") or rec.get("deadline")
    status = rec.get("status") or rec.get("publicationStatus")
    fy = rec.get("fiscalYear") or ""
    # 簡易正規化（詳細は後段で改善）
    row = dict(
        program_id=f"jgrants:{pid}" if pid else None,
        source_url=url,
        publisher=pub,
        title=title,
        fiscal_year=str(fy),
        domain=None,
        eligibility_json=None,
        geography=None,
        subsidy_rate=None,
        subsidy_cap_jpy=None,
        budget_total_jpy=None,
        cost_items_allowed=None,
        deadline_type="hard" if deadline else None,
        deadline_at=deadline,
        application_method="jgrants",
        requires_gbizid=True,
        docs_urls_json=None,
        status=status,
        published_at=None,
        last_seen_at=datetime.utcnow().isoformat(),
        content_hash=None
    )
    return row

def norm_pref_page(rec):
    title = rec.get("page_name")
    url = rec.get("source_url")
    pub = rec.get("publisher")
    geo = rec.get("geography")
    row = dict(
        program_id=None,
        source_url=url,
        publisher=pub,
        title=title,
        fiscal_year=None,
        domain=None,
        eligibility_json=None,
        geography=geo,
        subsidy_rate=None,
        subsidy_cap_jpy=None,
        budget_total_jpy=None,
        cost_items_allowed=None,
        deadline_type=None,
        deadline_at=None,
        application_method="direct",
        requires_gbizid=False,
        docs_urls_json=json.dumps([l["url"] for l in rec.get("links",[])], ensure_ascii=False),
        status="unknown",
        published_at=None,
        last_seen_at=datetime.utcnow().isoformat(),
        content_hash=None
    )
    return row

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rows = []
    for path in sum([glob.glob(x) for x in args.inputs], []):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                if "links" in rec and "page_name" in rec:
                    rows.append(norm_pref_page(rec))
                elif "id" in rec or "subsidyId" in rec:
                    rows.append(norm_jgrants(rec))
                else:
                    # SII等はリンク収集のみ→docs_urls_jsonに入れておく
                    rows.append({
                        "program_id": None,
                        "source_url": rec.get("url"),
                        "publisher": "SII/関連",
                        "title": rec.get("text"),
                        "fiscal_year": None,
                        "domain": "energy",
                        "eligibility_json": None,
                        "geography": None,
                        "subsidy_rate": None,
                        "subsidy_cap_jpy": None,
                        "budget_total_jpy": None,
                        "cost_items_allowed": None,
                        "deadline_type": None,
                        "deadline_at": None,
                        "application_method": "direct",
                        "requires_gbizid": False,
                        "docs_urls_json": None,
                        "status": "unknown",
                        "published_at": None,
                        "last_seen_at": datetime.utcnow().isoformat(),
                        "content_hash": None
                    })
    df = pd.DataFrame(rows, columns=SCHEMA_COLS)
    df.to_parquet(args.out, index=False)
    print(f"Wrote {len(df)} rows to {args.out}")

if __name__ == "__main__":
    main()
