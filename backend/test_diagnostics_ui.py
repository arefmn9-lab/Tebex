from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.routes import automation as automation_routes


def _use_diagnostics_root(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "diagnostics"
    root.mkdir()
    monkeypatch.setattr(automation_routes, "DIAGNOSTICS_ROOT", root)
    return root


def test_diagnostics_runs_are_reachable_and_sorted_newest_first(monkeypatch, tmp_path: Path) -> None:
    root = _use_diagnostics_root(monkeypatch, tmp_path)
    old = root / "old_run"
    new = root / "new_run"
    old.mkdir()
    new.mkdir()
    (old / "result.json").write_text('{"status":"old"}', encoding="utf-8")
    (new / "batch_summary.json").write_text('{"status":"new"}', encoding="utf-8")
    (new / "screen.png").write_bytes(b"png")

    response = TestClient(app).get("/automation/diagnostics/runs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["read_only"] is True
    assert [run["name"] for run in payload["runs"]][:2] == ["new_run", "old_run"]
    assert payload["runs"][0]["status_file"] == "batch_summary.json"
    assert payload["runs"][0]["screenshot_count"] == 1


def test_diagnostics_file_listing_stays_under_allowed_root(monkeypatch, tmp_path: Path) -> None:
    root = _use_diagnostics_root(monkeypatch, tmp_path)
    run = root / "run_a"
    run.mkdir()
    (run / "result.json").write_text('{"ok":true}', encoding="utf-8")
    (run / "events.jsonl").write_text('{"event":"one"}\n', encoding="utf-8")
    (run / "source_chat.png").write_bytes(b"image")

    response = TestClient(app).get("/automation/diagnostics/runs/run_a/files")

    assert response.status_code == 200
    paths = {item["relative_path"] for item in response.json()["files"]}
    assert paths == {"run_a/result.json", "run_a/events.jsonl", "run_a/source_chat.png"}


def test_diagnostics_json_jsonl_text_and_image_previews(monkeypatch, tmp_path: Path) -> None:
    root = _use_diagnostics_root(monkeypatch, tmp_path)
    run = root / "run_a"
    run.mkdir()
    (run / "result.json").write_text('{"status":"ok"}', encoding="utf-8")
    (run / "events.jsonl").write_text('{"event":"one"}\n', encoding="utf-8")
    (run / "notes.txt").write_text("plain text", encoding="utf-8")
    (run / "screen.png").write_bytes(b"png")

    client = TestClient(app)
    assert client.get("/automation/diagnostics/preview", params={"path": "run_a/result.json"}).json()["parsed_json"] == {"status": "ok"}
    assert "event" in client.get("/automation/diagnostics/preview", params={"path": "run_a/events.jsonl"}).json()["preview"]
    assert client.get("/automation/diagnostics/preview", params={"path": "run_a/notes.txt"}).json()["preview"] == "plain text"
    image_payload = client.get("/automation/diagnostics/preview", params={"path": "run_a/screen.png"}).json()
    assert image_payload["file"]["kind"] == "image"
    assert image_payload["image_url"].endswith("run_a/screen.png")


def test_diagnostics_invalid_json_large_preview_and_unsupported_file_are_safe(monkeypatch, tmp_path: Path) -> None:
    root = _use_diagnostics_root(monkeypatch, tmp_path)
    run = root / "run_a"
    run.mkdir()
    (run / "bad.json").write_text("{bad", encoding="utf-8")
    (run / "large.log").write_text("x" * (automation_routes._DIAGNOSTIC_MAX_PREVIEW_BYTES + 10), encoding="utf-8")
    (run / "artifact.bin").write_bytes(b"\0\1")

    client = TestClient(app)
    assert client.get("/automation/diagnostics/preview", params={"path": "run_a/bad.json"}).json()["parse_error"].startswith("Invalid JSON")
    large = client.get("/automation/diagnostics/preview", params={"path": "run_a/large.log"}).json()
    assert large["truncated"] is True
    assert len(large["preview"]) == automation_routes._DIAGNOSTIC_MAX_PREVIEW_BYTES
    unsupported = client.get("/automation/diagnostics/preview", params={"path": "run_a/artifact.bin"}).json()
    assert unsupported["file"]["kind"] == "unsupported"
    assert unsupported["preview"] is None


def test_diagnostics_reject_traversal_absolute_profiles_and_secret_files(monkeypatch, tmp_path: Path) -> None:
    root = _use_diagnostics_root(monkeypatch, tmp_path)
    run = root / "run_a"
    run.mkdir()
    (run / "result.json").write_text("{}", encoding="utf-8")
    (run / ".env").write_text("SECRET=1", encoding="utf-8")
    (run / "cookies.json").write_text("{}", encoding="utf-8")
    (run / "data.db").write_bytes(b"db")

    client = TestClient(app)
    blocked_paths = [
        "../outside.txt",
        "..%2Foutside.txt",
        str(tmp_path / "outside.txt"),
        "browser_profiles/profile.json",
        "run_a/.env",
        "run_a/cookies.json",
        "run_a/data.db",
    ]
    for path in blocked_paths:
        response = client.get("/automation/diagnostics/preview", params={"path": path})
        assert response.status_code in {403, 404}


def test_diagnostics_download_is_safe_and_ui_is_read_only(monkeypatch, tmp_path: Path) -> None:
    root = _use_diagnostics_root(monkeypatch, tmp_path)
    run = root / "run_a"
    run.mkdir()
    (run / "result.json").write_text('{"ok":true}', encoding="utf-8")
    (run / "artifact.bin").write_bytes(b"\0\1")

    client = TestClient(app)
    assert client.get("/automation/diagnostics/download", params={"path": "run_a/result.json"}).status_code == 200
    assert client.get("/automation/diagnostics/download", params={"path": "run_a/artifact.bin"}).status_code == 415
    assert client.post("/automation/diagnostics/runs").status_code == 405


def test_diagnostics_frontend_route_and_navigation_are_registered() -> None:
    app_source = Path("frontend/src/App.jsx").read_text(encoding="utf-8")
    sidebar_source = Path("frontend/src/components/Sidebar.jsx").read_text(encoding="utf-8")
    page_source = Path("frontend/src/pages/Diagnostics.jsx").read_text(encoding="utf-8")

    assert "diagnostics: Diagnostics" in app_source
    assert 'id: "diagnostics"' in sidebar_source
    assert "تشخیص فنی" in sidebar_source
    assert "listDiagnosticRuns" in page_source
    assert "previewDiagnosticFile" in page_source
    assert "Download" in page_source
    assert "Copy" in page_source
    assert "delete" not in page_source.lower()
    assert "upload" not in page_source.lower()
