"""
The file-handoff seam between the native scheduler and the in-app Chrome tasks.

The native side never drives Chrome (a launchd process can't reach the
Claude-in-Chrome integration). Instead it drops a request JSON here; a thin
in-app Cowork task polls pending(), does the browser work, and calls done().
This is the proven, reliable form of "native dispatcher triggers Cowork".

Request kinds:
  - publish_linkedin : post an approved Company comment/post to the company page
  - publish_newsletter   : publish an approved Newsletter Hiker draft to Substack/X
  - surface_linkedin : read LinkedIn for on-thesis posts, write results back
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from . import config

VALID_KINDS = {"publish_linkedin", "publish_newsletter", "surface_linkedin"}


def _slug() -> str:
    return uuid.uuid4().hex[:12]


def enqueue(kind: str, payload: dict, *, queue_dir: Path | None = None) -> Path:
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown chrome_queue kind: {kind}")
    qd = queue_dir or config.QUEUE_DIR
    qd.mkdir(parents=True, exist_ok=True)
    req = {"kind": kind, "payload": payload, "id": _slug(), "status": "pending",
           "queued_at": datetime.now().astimezone().isoformat()}
    path = qd / f"{kind}-{req['id']}.json"
    path.write_text(json.dumps(req, indent=2), encoding="utf-8")
    return path


def pending(*, queue_dir: Path | None = None) -> list[dict]:
    qd = queue_dir or config.QUEUE_DIR
    out: list[dict] = []
    for p in sorted(qd.glob("*.json")):
        try:
            req = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if req.get("status") == "pending":
            req["_path"] = str(p)
            out.append(req)
    return out


def done(req_path: str | Path, result: dict | None = None) -> None:
    p = Path(req_path)
    req = json.loads(p.read_text(encoding="utf-8"))
    req["status"] = "done"
    req["done_at"] = datetime.now().astimezone().isoformat()
    if result is not None:
        req["result"] = result
    p.write_text(json.dumps(req, indent=2), encoding="utf-8")
