"""Small file-backed job store.  One directory per translation job."""
from __future__ import annotations

import json
import hashlib
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from . import config

_JOB_ID_RE = re.compile(r"^[a-f0-9]{8,32}$")
_WRITE_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def job_dir(job_id: str) -> Path:
    if not _JOB_ID_RE.match(job_id):
        raise ValueError("invalid job id")
    return config.JOBS_DIR / job_id


def _job_path(job_id: str) -> Path:
    return job_dir(job_id) / "job.json"


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def create_job(filename: str, content: bytes) -> dict[str, Any]:
    job_id = uuid.uuid4().hex[:12]
    directory = config.JOBS_DIR / job_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "source.pdf").write_bytes(content)
    job = {
        "id": job_id,
        "filename": filename,
        "sha256": hashlib.sha256(content).hexdigest(),
        "status": "queued",
        "progress": 0,
        "message": "已上传，等待处理",
        "error": "",
        "pages": 0,
        "title": "",
        "total_blocks": 0,
        "translated_blocks": 0,
        "created_at": _now(),
        "updated_at": _now(),
    }
    _write_json(directory / "job.json", job)
    return job


def get_job(job_id: str) -> dict[str, Any] | None:
    try:
        path = _job_path(job_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def update_job(job_id: str, **fields: Any) -> dict[str, Any] | None:
    with _WRITE_LOCK:
        job = get_job(job_id)
        if job is None:
            return None
        job.update(fields)
        job["updated_at"] = _now()
        _write_json(_job_path(job_id), job)
        return job


def save_document(job_id: str, document: dict) -> None:
    _write_json(job_dir(job_id) / "document.json", document)


def load_document(job_id: str) -> dict | None:
    try:
        path = job_dir(job_id) / "document.json"
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_translation_html(job_id: str, html: str) -> None:
    path = job_dir(job_id) / "translation.html"
    tmp = path.with_suffix(".html.tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(path)


def translation_html_path(job_id: str) -> Path:
    return job_dir(job_id) / "translation.html"


def _job_pdf_sha(job: dict[str, Any]) -> str:
    stored = str(job.get("sha256") or "").strip()
    if stored:
        return stored
    try:
        path = job_dir(str(job.get("id") or "")) / "source.pdf"
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except (OSError, ValueError):
        return str(job.get("filename") or job.get("id") or "")


def list_jobs(limit: int = 12) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    if not config.JOBS_DIR.exists():
        return jobs
    for directory in config.JOBS_DIR.iterdir():
        if not directory.is_dir():
            continue
        path = directory / "job.json"
        if not path.exists():
            continue
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("status") in ("done", "error", "translating", "parsing"):
            jobs.append(job)
    jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)

    # The same PDF may have several translation attempts. Keep only the newest
    # job for each file so the "recent translations" list does not repeat it.
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for job in jobs:
        key = _job_pdf_sha(job)
        if key in seen:
            continue
        seen.add(key)
        unique.append(job)
        if len(unique) >= limit:
            break
    return unique


def recover_interrupted_jobs() -> None:
    """Mark jobs that were running when the server stopped as failed."""
    for job in list_jobs(limit=100):
        if job.get("status") in ("parsing", "translating", "queued"):
            update_job(
                job["id"],
                status="error",
                message="服务重启，任务已中断，请重新上传",
                error="server restarted",
            )
