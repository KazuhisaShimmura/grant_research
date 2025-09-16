import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etl import fetch_pref_pages


class DummyResponse:
    def __init__(self, text: str):
        self.text = text
        self.status_code = 200
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    def raise_for_status(self) -> None:  # pragma: no cover - simple stub
        return


class DummySession:
    def __init__(self, html_by_url):
        self.html_by_url = html_by_url
        self.requested_urls = []

    def get(self, url, timeout=30):
        self.requested_urls.append((url, timeout))
        return DummyResponse(self.html_by_url[url])


def write_config(tmp_path: Path, name: str, data: dict) -> Path:
    config_path = tmp_path / f"sources_{name}.yml"
    config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return config_path


def test_run_creates_output_and_preview(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    write_config(
        config_dir,
        "demo",
        {
            "publisher": "Demo Pref",
            "geography": "JP-00",
            "pages": [
                {
                    "name": "Support",
                    "url": "https://example.com/support/",
                    "selectors": {
                        "links": "a.doc",
                        "updated_at": ".updated",
                    },
                }
            ],
        },
    )

    html = """
    <html>
      <body>
        <div class="updated">2024/04/01</div>
        <a class="doc" href="files/info.pdf">Guideline PDF</a>
        <a class="doc" href="files/ignore.txt">Ignored text</a>
      </body>
    </html>
    """
    session = DummySession({"https://example.com/support/": html})

    monkeypatch.setattr(fetch_pref_pages, "CONFIG_DIR", config_dir)

    out_path = tmp_path / "out" / "result.jsonl"

    fetch_pref_pages.run(
        out_path,
        prefs=["demo"],
        sleep=0,
        preview=2,
        session=session,
    )

    captured = capsys.readouterr()
    assert "[DONE] wrote 1 rows" in captured.out
    assert "[PREVIEW] showing up to 2 rows" in captured.out

    assert out_path.exists()
    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])

    assert record["publisher"] == "Demo Pref"
    assert record["pdf_links"] == [
        {
            "url": "https://example.com/support/files/info.pdf",
            "text": "Guideline PDF",
        }
    ]
    assert record["updated_at"] == "2024-04-01T00:00:00+00:00"
    assert "fetched_at" in record

    assert session.requested_urls == [("https://example.com/support/", 30)]


def test_run_with_no_rows_creates_empty_file_and_preview(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    write_config(
        config_dir,
        "empty",
        {
            "publisher": "Empty Pref",
            "geography": "JP-99",
            "pages": [],
        },
    )

    monkeypatch.setattr(fetch_pref_pages, "CONFIG_DIR", config_dir)

    out_path = tmp_path / "out" / "empty.jsonl"

    fetch_pref_pages.run(
        out_path,
        prefs=["empty"],
        sleep=0,
        preview=3,
    )

    captured = capsys.readouterr()
    assert "[INFO] wrote 0 rows" in captured.out
    assert "[PREVIEW] <empty>" in captured.out

    assert out_path.exists()
    assert out_path.read_text(encoding="utf-8") == ""
