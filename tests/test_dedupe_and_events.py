from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etl import dedupe_and_events


def run_dedupe(tmp_path, df: pd.DataFrame) -> pd.DataFrame:
    input_path = tmp_path / "normalized.parquet"
    output_path = tmp_path / "events.parquet"
    df.to_parquet(input_path, index=False)

    argv = [
        "dedupe_and_events.py",
        str(input_path),
        "--out",
        str(output_path),
    ]

    old_argv = dedupe_and_events.sys.argv
    dedupe_and_events.sys.argv = argv
    try:
        dedupe_and_events.main()
    finally:
        dedupe_and_events.sys.argv = old_argv

    return pd.read_parquet(output_path)


def test_mixed_timezone_application_period(monkeypatch, tmp_path):
    fixed_now = pd.Timestamp("2024-01-01", tz="UTC")

    monkeypatch.setattr(
        dedupe_and_events.pd.Timestamp,
        "now",
        classmethod(lambda cls, tz=None: fixed_now if tz else fixed_now.tz_localize(None)),
    )

    df = pd.DataFrame(
        [
            {
                "title": "A",
                "geography": "Tokyo",
                "subsidy_rate": "1/2",
                "application_period": "2024-06-30",
                "source_url": "https://example.com/a",
                "subsidy_cap_jpy": 1000000,
                "employee_limit": 10,
            },
            {
                "title": "A",
                "geography": "Tokyo",
                "subsidy_rate": "1/2",
                "application_period": "2024-06-30",
                "source_url": "https://example.com/a",
                "subsidy_cap_jpy": 1000000,
                "employee_limit": 20,
            },
            {
                "title": "B",
                "geography": "Osaka",
                "subsidy_rate": None,
                "application_period": "2023-01-01T00:00:00+00:00",
                "source_url": "https://example.com/b",
                "subsidy_cap_jpy": None,
                "employee_limit": None,
            },
        ]
    )

    result = run_dedupe(tmp_path, df)

    assert len(result) == 3
    assert result["dedupe_key"].nunique() == 3
    assert set(result["status"]) == {"open", "closed"}


def test_parse_period_end_returns_utc():
    timestamps = [
        "2025-09-16 00:00:00+00:00",
        "2025-09-16",
        "2025年9月16日",
        {"end": "2025-09-16"},
    ]

    parsed = [dedupe_and_events.parse_period_end(value) for value in timestamps]

    for ts in parsed:
        assert not pd.isna(ts)
        assert ts.tzinfo is not None and str(ts.tzinfo) == "UTC"
