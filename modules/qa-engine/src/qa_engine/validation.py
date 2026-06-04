from __future__ import annotations

import re
from pathlib import Path


RUN_ID_PATTERN = re.compile(r"^QA_\d{4}_\d{2}_\d{2}_[A-Z0-9]{4}$")


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_PATTERN.match(str(run_id)):
        raise ValueError(f"Invalid QA run id: {run_id}")
    return str(run_id)


def ensure_json_file(path: Path, name: str) -> None:
    if not path.exists() or not path.is_file():
        raise ValueError(f"Missing required file: {name} ({path})")
    if path.suffix.lower() != ".json":
        raise ValueError(f"Invalid {name} file extension: {path}")


def ensure_within(base_dir: Path, candidate: Path) -> Path:
    resolved_base = base_dir.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_base)
    except ValueError as exc:
        raise ValueError(f"Unsafe path outside expected root: {resolved_candidate}") from exc
    return resolved_candidate
