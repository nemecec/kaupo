"""Cloud orchestration keeps credentials out of candidate containers and records missing attempts."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def cloud():
    path = Path(__file__).resolve().parents[2] / "scripts/research_cloud.py"
    spec = importlib.util.spec_from_file_location("research_cloud", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def contract():
    return {
        "id": "attempt",
        "dataset_sha256": "d" * 64,
        "manifest": {"strategy_ref": "a" * 40, "variants": [{"id": "control"}, {"id": "candidate"}]},
        "economics": {"image": "ghcr.io/nemecec/kaupo:" + "b" * 40},
    }


def test_each_candidate_uses_offline_fresh_database_without_credentials(cloud, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VARIANT", "control")
    monkeypatch.setenv("KAUPO_RESEARCH_TOKEN", "must-not-enter-container")
    (tmp_path / "candidate/strategies").mkdir(parents=True)
    (tmp_path / "prepared").mkdir()
    (tmp_path / "prepared/contract.json").write_text(json.dumps(contract()))
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        if "research-candidate" in args:
            (tmp_path / "results/control/result.json").write_text(
                json.dumps({"id": "control", "status": "completed"})
            )

    monkeypatch.setattr(cloud, "docker", docker)
    monkeypatch.setattr(
        cloud.subprocess,
        "check_output",
        lambda args, **kwargs: "a" * 40 if args[0] == "git" else "ghcr.io/nemecec/kaupo@sha256:" + "f" * 64,
    )
    monkeypatch.setattr(cloud.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    cloud.sandbox()
    candidate = next(call for call in calls if "research-candidate" in call)
    assert candidate[candidate.index("--network") + 1] == "container:research-db"
    assert "--cap-drop=ALL" in candidate
    assert "--read-only" in candidate
    assert "must-not-enter-container" not in repr(calls)
    database = next(call for call in calls if "postgres:16-alpine" in call and call[0] == "run")
    assert database[database.index("--network") + 1] == "none"
    initialize = next(call for call in calls if call[-1] == "initialize")
    assert not any("/strategies" in argument for argument in initialize)
    assert "image_digest" in json.loads((tmp_path / "results/control/result.json").read_text())


def test_report_preserves_missing_and_invalid_variants(cloud, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EXPERIMENT_ID", "attempt")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    (tmp_path / "results/control").mkdir(parents=True)
    (tmp_path / "results/control/result.json").write_text(
        json.dumps({"id": "wrong-id", "status": "completed"})
    )
    posted = []

    def api(path, body=None):
        if body is not None:
            posted.append(body)
        return contract()

    monkeypatch.setattr(cloud, "api", api)
    cloud.report()
    assert posted[0]["outcomes"] == [
        {"id": "control", "status": "failed", "error": "Invalid cloud artifact: artifact id mismatch"},
        {"id": "candidate", "status": "not_run", "error": "No valid cloud artifact"},
    ]


def test_symlink_cannot_become_evidence(cloud, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EXPERIMENT_ID", "attempt")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    (tmp_path / "results/control").mkdir(parents=True)
    target = tmp_path / "untrusted.json"
    target.write_text('{"id":"control","status":"completed"}')
    (tmp_path / "results/control/result.json").symlink_to(target)
    posted = []
    monkeypatch.setattr(cloud, "api", lambda path, body=None: posted.append(body) if body else contract())
    cloud.report()
    assert posted[0]["outcomes"][0]["status"] == "not_run"
