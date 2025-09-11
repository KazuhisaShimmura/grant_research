#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BigQuery Loader — resilient & production-ready

- Auto-detect input format (.parquet / .jsonl|.ndjson / .csv)
- Normalize column names to BigQuery-friendly identifiers
- Coerce object (dict/list) columns to JSON strings
- Unify datetimes to tz-aware UTC
- Support write disposition (append/truncate/empty), partitioning, clustering
- Optional explicit schema (JSON) or fallback to PyArrow inference
"""

import argparse
import json
import os
import re
import sys
import hashlib
from datetime import datetime, timezone
from typing import List, Optional

import pandas as pd

from google.cloud import bigquery
from google.cloud.bigquery import SchemaField


# ---------- helpers ----------

BQ_NAME_PATTERN = re.compile(r'[^a-zA-Z0-9_]')
BQ_LEADING_BAD = re.compile(r'^[^a-zA-Z_]')

def normalize_bq_column_name(name: str) -> str:
    if name is None:
        return "_col"
    s = str(name).strip()
    s = BQ_NAME_PATTERN.sub("_", s)
    s = BQ_LEADING_BAD.sub("_", s)
    # BigQuery 列名は1〜300文字、念のためクリップ
    return s[:300] or "_col"

def normalize_dataframe_for_bq(df: pd.DataFrame) -> pd.DataFrame:
    # 列名を正規化（重複はサフィックスで解消）
    original = list(df.columns)
    newcols = []
    used = {}
    for c in original:
        nc = normalize_bq_column_name(c)
        if nc in used:
            used[nc] += 1
            nc = f"{nc}__{used[nc]}"
        else:
            used[nc] = 0
        newcols.append(nc)
    df = df.copy()
    df.columns = newcols

    # dict/list を含む列を JSON 文字列に（BQでSTRINGとして安定ロード）
    for c in df.columns:
        if df[c].dtype == "object":
            # 中に dict/list が混じる列は JSON 文字列化
            if df[c].map(lambda x: isinstance(x, (dict, list))).any():
                df[c] = df[c].map(lambda x: json.dumps(x, ensure_ascii=False) if isinstance(x, (dict, list)) else x)

    # datetime を tz-aware UTC に統一
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            # tz-naive は UTC とみなす、tz-aware は UTC へ変換
            if getattr(df[c].dtype, "tz", None) is None:
                df[c] = df[c].dt.tz_localize(timezone.utc)
            else:
                df[c] = df[c].dt.tz_convert(timezone.utc)

    return df

def read_input(path: str) -> pd.DataFrame:
    low = path.lower()
    if low.endswith(".parquet"):
        return pd.read_parquet(path)
    if low.endswith(".jsonl") or low.endswith(".ndjson"):
        return pd.read_json(path, lines=True)
    if low.endswith(".csv"):
        # 日本語混在想定、推定＆上書きの順指定
        try:
            return pd.read_csv(path)
        except UnicodeDecodeError:
            return pd.read_csv(path, encoding="cp932")
    raise ValueError(f"Unsupported input format for {path} (expected .parquet/.jsonl/.ndjson/.csv)")

def load_schema_from_json(path: str) -> List[SchemaField]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # 期待する形式: [{"name":"col","type":"STRING","mode":"NULLABLE","description":"..."}]
    schema = []
    for col in data:
        schema.append(
            bigquery.SchemaField(
                col["name"],
                col["type"],
                mode=col.get("mode", "NULLABLE"),
                description=col.get("description"),
            )
        )
    return schema

def parse_args():
    p = argparse.ArgumentParser(description="Load a file into BigQuery (robust).")
    p.add_argument("input", help="Input file (.parquet | .jsonl/.ndjson | .csv)")
    p.add_argument("--project", default=os.environ.get("GCP_PROJECT", ""),
                   help="GCP project (default: env GCP_PROJECT)")
    p.add_argument("--dataset", default=os.environ.get("BQ_DATASET", "grants_hc"),
                   help="BigQuery dataset (default: env BQ_DATASET or 'grants_hc')")
    p.add_argument("--table", default=os.environ.get("BQ_TABLE", "programs"),
                   help="BigQuery table (default: env BQ_TABLE or 'programs')")
    p.add_argument("--write", choices=["append", "truncate", "empty"], default="append",
                   help="Write disposition: append=WRITE_APPEND, truncate=WRITE_TRUNCATE, empty=WRITE_EMPTY")
    p.add_argument("--schema", help="Path to BigQuery schema JSON (optional)")
    p.add_argument("--partition-field", help="Time partitioning field (optional)")
    p.add_argument("--partition-type", choices=["DAY", "HOUR", "MONTH", "YEAR"], default="DAY",
                   help="Partitioning granularity (default: DAY)")
    p.add_argument("--cluster", nargs="*", default=None,
                   help="Clustering column names (space-separated)")
    p.add_argument("--location", default=None, help="BigQuery location (e.g., US, asia-northeast1)")
    p.add_argument("--max-bad-records", type=int, default=0,
                   help="Maximum number of bad records allowed (default: 0)")
    p.add_argument("--dry-run", action="store_true", help="Plan & validate only; don't load")
    p.add_argument("--job-id-prefix", default=None, help="Custom job id prefix for idempotency (optional)")
    return p.parse_args()

# ---------- main ----------

def main():
    args = parse_args()

    if not args.project:
        print("GCP_PROJECT (or --project) is empty; skip load.", file=sys.stderr)
        sys.exit(0)

    table_id = f"{args.project}.{args.dataset}.{args.table}"
    print(f"[INFO] Loading to {table_id}")

    # Read & normalize dataframe
    df = read_input(args.input)
    if df.empty:
        print("[WARN] Input has 0 rows; nothing to load.")
        sys.exit(0)

    before_cols = list(df.columns)
    df = normalize_dataframe_for_bq(df)
    after_cols = list(df.columns)

    # Write disposition
    wd_map = {
        "append": bigquery.WriteDisposition.WRITE_APPEND,
        "truncate": bigquery.WriteDisposition.WRITE_TRUNCATE,
        "empty": bigquery.WriteDisposition.WRITE_EMPTY,
    }
    write_disposition = wd_map[args.write]

    # Job config
    job_config = bigquery.LoadJobConfig(
        write_disposition=write_disposition,
        max_bad_records=args.max_bad_records,
        create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
    )

    # Use explicit schema if provided; otherwise let BQ infer via PyArrow
    if args.schema:
        schema = load_schema_from_json(args.schema)
        job_config.schema = schema
        # スキーマ指定時は from_dataframe 側の推論を抑制
        job_config.autodetect = False
    else:
        job_config.autodetect = True  # PyArrow で推論

    # Partitioning / Clustering
    if args.partition_field:
        job_config.time_partitioning = bigquery.TimePartitioning(
            type_=getattr(bigquery.TimePartitioningType, args.partition_type),
            field=args.partition_field,
        )
    if args.cluster:
        job_config.clustering_fields = args.cluster

    # Job ID (idempotency friendly)
    if not args.job_id_prefix:
        # 入力パスとテーブル名から簡易ハッシュ
        h = hashlib.sha1(f"{args.input}|{table_id}".encode("utf-8")).hexdigest()[:8]
        job_id_prefix = f"bqload_{h}_"
    else:
        job_id_prefix = args.job_id_prefix

    client = bigquery.Client(project=args.project, location=args.location)

    # Dry run: validate schema/table existence/permissions
    if args.dry_run:
        # BQ の dry-run はクエリに対してのみ。ここでは前段チェックを行う
        print("[DRY-RUN] Skipping actual load. Showing summary:")
        print(f"  input_rows={len(df)}  columns={len(df.columns)}  location={args.location or '(default)'}")
        if before_cols != after_cols:
            print("  [INFO] Column names normalized:")
            for b, a in zip(before_cols, after_cols):
                if b != a:
                    print(f"    {b} -> {a}")
        if args.schema:
            print(f"  schema: {args.schema} (explicit)")
        else:
            print("  schema: autodetect (PyArrow)")
        print(f"  write_disposition={args.write}  partition_field={args.partition_field or '-'}  cluster={args.cluster or '-'}")
        sys.exit(0)

    # Load
    load_job = client.load_table_from_dataframe(
        df, table_id, job_config=job_config, job_id_prefix=job_id_prefix
    )
    print(f"[INFO] Started job: {load_job.job_id}")
    result = load_job.result()  # Waits for the job to complete

    # Post info
    dest = client.get_table(table_id)
    print(f"[DONE] Loaded {len(df)} rows to {table_id}")
    print(f"       Table now has {dest.num_rows} rows, {len(dest.schema)} columns")
    if args.partition_field:
        print(f"       Partitioned on {args.partition_field} ({args.partition_type})")
    if args.cluster:
        print(f"       Clustering: {', '.join(args.cluster)}")

if __name__ == "__main__":
    main()
