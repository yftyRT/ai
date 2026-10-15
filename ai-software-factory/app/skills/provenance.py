"""Machine-readable provenance manifests for imported Skills."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 1


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_version_record(
    *,
    repo_url: str,
    file_path: str,
    commit_sha: str,
    license_spdx: Optional[str],
    content: str,
    evaluation: dict[str, Any],
    attribution: str = "",
    status: str = "approved",
    imported_at: Optional[str] = None,
) -> dict[str, Any]:
    """One immutable record per imported version."""
    return {
        "repo_url": repo_url,
        "file_path": file_path,
        "commit_sha": commit_sha,
        "pinned": bool(commit_sha and len(commit_sha) >= 7),
        "license": license_spdx or "unknown",
        "imported_at": imported_at or utc_now_iso(),
        "content_hash": sha256_text(content),
        "evaluation": evaluation,
        "attribution": attribution,
        "status": status,
        "version_id": (commit_sha[:12] if commit_sha else sha256_text(content)[:12]),
    }


def build_manifest(name: str, roles: list[str], records: list[dict[str, Any]], active_version: str, enabled: bool = True) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "name": name,
        "roles": roles,
        "enabled": enabled,
        "active_version": active_version,
        "versions": records,
        "updated_at": utc_now_iso(),
    }


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Atomic write so a crash cannot leave a half-written manifest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_manifest(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def verify_content_hash(record: dict[str, Any], content: str) -> bool:
    return record.get("content_hash") == sha256_text(content)


def attribution_notice(repo_url: str, file_path: str, commit_sha: str, license_spdx: str, holder_hint: str = "") -> str:
    lines = [
        "# Attribution",
        "",
        "This Skill was imported from a third-party repository and is used under its license.",
        "",
        f"- Source repository: {repo_url}",
        f"- Original file: {file_path}",
        f"- Pinned commit: {commit_sha or 'unknown'}",
        f"- License (SPDX): {license_spdx or 'unknown'}",
    ]
    if holder_hint:
        lines.append(f"- Repository owner: {holder_hint}")
    lines += ["", "Keep this notice with the Skill. See the upstream repository for the full license text."]
    return "\n".join(lines) + "\n"
