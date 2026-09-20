#!/usr/bin/env python3
"""Trusted workflow client. Candidate code runs only in the offline Docker job."""
# ruff: noqa: S603, S607, S108, S310
# Commands use argv arrays, the API requires HTTPS, and /tmp is a private container tmpfs.

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

MAX_ARTIFACT = 64 * 1024 * 1024


def api(path: str, body: Any = None, binary: bool = False) -> Any:
    base = os.environ["KAUPO_API_URL"].rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("the API requires HTTPS")
    request = urllib.request.Request(
        base + "/api/v1/research/experiments" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + os.environ["KAUPO_RESEARCH_TOKEN"],
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        raw = response.read(MAX_ARTIFACT + 1)
    if len(raw) > MAX_ARTIFACT:
        raise ValueError("response exceeds artifact limit")
    return raw if binary else json.loads(raw)


def output(key: str, value: str) -> None:
    with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
        handle.write(f"{key}={value}\n")


def prepare() -> None:
    spec = json.loads(os.environ["EXPERIMENT"])
    contract = api("", spec)
    Path("prepared").mkdir(exist_ok=True)
    Path("prepared/contract.json").write_text(json.dumps(contract))
    if contract["status"] == "reported":
        print("This reference already has immutable results; choose a new reference for a new attempt.")
        return
    # Write the id first so the final job can record a preparation failure.
    output("experiment_id", contract["id"])
    output("strategy_ref", contract["manifest"]["strategy_ref"])
    output("variants", json.dumps([v["id"] for v in contract["manifest"]["variants"]]))
    image = contract["economics"]["image"]
    if not re.fullmatch(r"ghcr.io/nemecec/kaupo:[0-9a-f]{40}", image):
        raise ValueError("the experiment requires a fixed platform image")
    output("image", image)
    output("platform_ref", image.rsplit(":", 1)[1])
    data = api("/" + contract["id"] + "/dataset", binary=True)
    Path("prepared/dataset.json.gz").write_bytes(data)
    refreshed = api("/" + contract["id"])
    if hashlib.sha256(data).hexdigest() != refreshed["dataset_sha256"]:
        raise ValueError("download digest mismatch")
    Path("prepared/contract.json").write_text(json.dumps(refreshed))
    output("ready", "true")


def docker(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(["docker", *args], check=True, **kwargs)


def sandbox() -> None:
    contract = json.loads(Path("prepared/contract.json").read_text())
    image = contract["economics"]["image"]
    variant = os.environ["VARIANT"]
    if variant not in [v["id"] for v in contract["manifest"]["variants"]]:
        raise ValueError("undeclared variant")
    actual = subprocess.check_output(["git", "-C", "candidate", "rev-parse", "HEAD"], text=True).strip()
    if actual != contract["manifest"]["strategy_ref"]:
        raise ValueError("candidate revision mismatch")
    if not re.fullmatch(r"ghcr.io/nemecec/kaupo:[0-9a-f]{40}", image):
        raise ValueError("unexpected image")
    strategy_dir = Path("candidate/strategies")
    if strategy_dir.is_symlink() or not strategy_dir.resolve().is_relative_to(Path("candidate").resolve()):
        raise ValueError("strategies must be inside the candidate checkout")
    out = Path("results") / variant
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o777)
    docker("pull", image)
    image_digest = subprocess.check_output(
        ["docker", "image", "inspect", image, "--format", "{{index .RepoDigests 0}}"], text=True
    ).strip()
    docker("pull", "postgres:16-alpine")
    try:
        docker(
            "run",
            "-d",
            "--name",
            "research-db",
            "--network",
            "none",
            "--memory",
            "1g",
            "--pids-limit",
            "128",
            "--cpus",
            "1",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,size=768m",
            "-e",
            "POSTGRES_PASSWORD=disposable",
            "-e",
            "POSTGRES_DB=research",
            "postgres:16-alpine",
        )
        for _ in range(60):
            ready = subprocess.run(
                ["docker", "exec", "research-db", "pg_isready", "-U", "postgres"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("disposable database did not start")
        common = [
            "run",
            "--rm",
            "--network",
            "container:research-db",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--read-only",
            "--memory=3g",
            "--cpus=2",
            "--pids-limit=128",
            "--ulimit",
            "fsize=67108864",
            "--tmpfs",
            "/tmp:rw,size=128m",
            "-e",
            "KAUPO_DATABASE_URL=postgresql+asyncpg://postgres:disposable@localhost:5432/research",
            "-v",
            str(Path("prepared").resolve()) + ":/input:ro",
        ]
        docker(
            *common,
            "--name",
            "research-initialize",
            image,
            "python",
            "-m",
            "kaupo.research.sandbox",
            "initialize",
            timeout=300,
        )
        docker(
            *common,
            "--name",
            "research-candidate",
            "-v",
            str(Path("candidate/strategies").resolve()) + ":/strategies:ro",
            "-v",
            str(out.resolve()) + ":/output:rw",
            image,
            "python",
            "-m",
            "kaupo.research.sandbox",
            "run",
            "--variant",
            variant,
            timeout=1800,
        )
        result_path = out / "result.json"
        if result_path.is_symlink() or not result_path.is_file() or result_path.stat().st_size > MAX_ARTIFACT:
            raise ValueError("missing or invalid result artifact")
        value = json.loads(result_path.read_text())
        value["image_digest"] = image_digest
        value["platform_ref"] = image.rsplit(":", 1)[1]
        result_path.unlink()  # The container uid owns the original file; the runner owns its directory.
        result_path.write_text(json.dumps(value, allow_nan=False))
    except Exception as exc:
        result = out / "result.json"
        if result.is_symlink() or result.is_file():
            result.unlink()
        result.write_text(json.dumps({"id": variant, "status": "failed", "error": str(exc)[:2000]}))
        raise
    finally:
        subprocess.run(
            ["docker", "rm", "-f", "research-candidate", "research-initialize", "research-db"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def report() -> None:
    experiment_id = os.environ["EXPERIMENT_ID"]
    contract = api("/" + experiment_id)
    outcomes = []
    for variant in contract["manifest"]["variants"]:
        path = Path("results") / variant["id"] / "result.json"
        outcome = {"id": variant["id"], "status": "not_run", "error": "No valid cloud artifact"}
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_ARTIFACT:
            try:
                value = json.loads(path.read_text())
                if not isinstance(value, dict) or value.get("status") not in (
                    "completed",
                    "failed",
                    "not_run",
                ):
                    raise ValueError("invalid result structure or status")
                if value["id"] != variant["id"]:
                    raise ValueError("artifact id mismatch")
                if (
                    value["status"] == "completed"
                    and value.get("dataset_sha256") != contract["dataset_sha256"]
                ):
                    raise ValueError("artifact dataset mismatch")
                # Full equity, orders, fills, and events stay in the artifact.
                evidence = value.pop("evidence", None)
                value["evidence_sha256"] = hashlib.sha256(
                    json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
                ).hexdigest()
                value["artifact_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                if len(json.dumps(value, allow_nan=False)) > 15000:
                    raise ValueError("oversized result summary")
                outcome = value
            except (KeyError, TypeError, ValueError, OSError) as exc:
                outcome["status"] = "failed"
                outcome["error"] = "Invalid cloud artifact: " + str(exc)[:1000]
        outcomes.append(outcome)
    api(
        "/" + experiment_id + "/result",
        {
            "dataset_sha256": contract["dataset_sha256"] or "0" * 64,
            "workflow_run_id": int(os.environ["GITHUB_RUN_ID"]),
            "outcomes": outcomes,
        },
    )
    print(json.dumps({"experiment_id": experiment_id, "outcomes": outcomes}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "sandbox", "report"))
    globals()[parser.parse_args().action]()
