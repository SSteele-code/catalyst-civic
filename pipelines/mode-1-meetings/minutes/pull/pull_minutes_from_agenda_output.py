#!/usr/bin/env python
"""
Minutes Pull (Outputs Pass)

Stage minute-related material out of existing Agenda parser outputs into:
  _Sources/M1-Meetings/Minutes/_staging/<RUN_ID>/

Strict invariant:
  - Pull only (copy/stage relevant material)
  - No DB writes
  - No downstream parsing/enrichment
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence


AGENDA_OUTPUT_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Agendas\_output")
MINUTES_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Minutes")
STAGING_ROOT = MINUTES_ROOT / "_staging"
STATE_FILE = MINUTES_ROOT / "minutes_output_pull_state.json"
MANIFEST_FILE = MINUTES_ROOT / "M1_MINUTES_OUTPUT_PULL_MANIFEST.jsonl"


SECTION_PATTERNS = [
    re.compile(r"^\s*[ivxlcdm]+\s*[\.\)]\s*[-_—]*\s*approval\s+of\s+minutes\b", re.IGNORECASE),
    re.compile(r"^\s*[ivxlcdm]+\s*[\.\)]\s*[-_—]*\s*approve\s+minutes\b", re.IGNORECASE),
    re.compile(r"^\s*[ivxlcdm]+\s*[\.\)]\s*[-_—]*\s*minutes\b", re.IGNORECASE),
    re.compile(r"^\s*[a-z]\s*[\.\),]\s*minutes\b", re.IGNORECASE),
    re.compile(r"^\s*approval\s+of\s+minutes\b", re.IGNORECASE),
    re.compile(r"^\s*in\s+re\s*:\s*minutes\b", re.IGNORECASE),
    re.compile(r"^\s*minutes\s+distributed\s+prior\s+to\s+meeting\b", re.IGNORECASE),
    re.compile(r"^\s*minutes\s*[-—:.]\s*$", re.IGNORECASE),
    re.compile(
        r"^\s*minutes\s*[-—:.]\s*.*(?:meeting|hearing|workshop|session|council|budget|planning)\b",
        re.IGNORECASE,
    ),
]

ACTION_PATTERNS = [
    re.compile(r"\bcouncil\s+voted\s+to\s+approve\b.*\bminutes\b", re.IGNORECASE),
    re.compile(r"\bmotion\b.*\b(?:approve|adopt)\b.*\bminutes\b", re.IGNORECASE),
    re.compile(r"\b(?:approve|approved|adopt)\b.*\bminutes\b", re.IGNORECASE),
    re.compile(r"\b(?:corrections?|deletions?)\b.*\bminutes\b", re.IGNORECASE),
    re.compile(r"\bminutes?\s+for\s+the\s+(?:month|following)\b", re.IGNORECASE),
]

MINUTES_TOKEN_RE = re.compile(r"\bminutes?\b", re.IGNORECASE)
NON_MINUTES_DURATION_RE = re.compile(
    r"\b(?:few|just|next)?\s*(?:\d+|one|two|three|four|five|ten|fifteen|thirty)\s*[- ]?minutes?\b",
    re.IGNORECASE,
)
NEXT_HEADING_RE = re.compile(
    r"^\s*(?:[ivxlcdm]+\s*[\.\)]\s+\S+|[A-Z][A-Za-z/&,\- ]{2,}:\s*$)",
    re.IGNORECASE,
)
PUBLIC_COMMENTS_RE = re.compile(r"\b(?:un)?scheduled\s+public\s+comments?\b", re.IGNORECASE)
PLAIN_TITLE_HEADING_RE = re.compile(r"^\s*[A-Z][A-Za-z0-9'&/\-]*(?:\s+[A-Z][A-Za-z0-9'&/\-]*){0,6}\s*$")
MINUTES_CONTEXT_RE = re.compile(
    r"\b(approval|approve|approved|adopt|meeting|hearings?|session|workshop|distributed|public|regular|special|budget)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Excerpt:
    kind: str
    start_line: int
    end_line: int
    text: str

    @property
    def key(self) -> str:
        payload = f"{self.kind}|{self.start_line}|{self.end_line}|{self.text}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"sources": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"sources": {}}


def save_state(state: dict) -> None:
    MINUTES_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def append_manifest_rows(rows: Sequence[dict]) -> None:
    if not rows:
        return
    MINUTES_ROOT.mkdir(parents=True, exist_ok=True)
    with MANIFEST_FILE.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")


def iter_agenda_output_texts(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    out: list[Path] = []
    for packet_dir in sorted(root.iterdir()):
        if not packet_dir.is_dir():
            continue
        canonical_txt = packet_dir / f"{packet_dir.name}.txt"
        if canonical_txt.exists():
            out.append(canonical_txt)
    return out


def is_noise_duration(line: str) -> bool:
    lower = line.lower()
    if not NON_MINUTES_DURATION_RE.search(lower):
        return False
    if MINUTES_CONTEXT_RE.search(lower):
        return False
    return True


def is_section_marker(line: str) -> bool:
    if not line.strip():
        return False
    for pattern in SECTION_PATTERNS:
        if pattern.search(line):
            return True
    return False


def is_action_line(line: str) -> bool:
    if not MINUTES_TOKEN_RE.search(line):
        return False
    if is_noise_duration(line):
        return False
    for pattern in ACTION_PATTERNS:
        if pattern.search(line):
            return True
    return False


def looks_like_new_heading(line: str) -> bool:
    if not line.strip():
        return False
    if PUBLIC_COMMENTS_RE.search(line):
        return True
    if PLAIN_TITLE_HEADING_RE.match(line) and not MINUTES_TOKEN_RE.search(line):
        return True
    if NEXT_HEADING_RE.match(line) and not MINUTES_TOKEN_RE.search(line):
        return True
    return False


def extract_section_block(lines: Sequence[str], start_idx: int, max_forward: int = 24) -> Excerpt:
    collected: list[str] = []
    end_idx = start_idx
    blank_streak = 0
    limit = min(len(lines), start_idx + max_forward + 1)

    for idx in range(start_idx, limit):
        line = lines[idx]
        if idx > start_idx and looks_like_new_heading(line):
            break

        collected.append(line.rstrip())
        end_idx = idx

        if not line.strip():
            blank_streak += 1
            if blank_streak >= 2 and idx > start_idx + 1:
                break
        else:
            blank_streak = 0

    while collected and not collected[-1].strip():
        collected.pop()
        end_idx -= 1

    return Excerpt(
        kind="minutes_section",
        start_line=start_idx + 1,
        end_line=end_idx + 1,
        text="\n".join(collected).strip(),
    )


def extract_action_block(lines: Sequence[str], idx: int, back: int = 1, forward: int = 3) -> Excerpt:
    start = max(0, idx - back)
    end = min(len(lines) - 1, idx + forward)
    snippet = "\n".join(line.rstrip() for line in lines[start : end + 1]).strip()
    return Excerpt(kind="minutes_action", start_line=start + 1, end_line=end + 1, text=snippet)


def extract_minutes_excerpts(text: str) -> list[Excerpt]:
    lines = text.splitlines()
    excerpts: list[Excerpt] = []
    seen: set[str] = set()
    covered_indices: set[int] = set()

    for idx, line in enumerate(lines):
        if idx in covered_indices:
            continue
        if not is_section_marker(line):
            continue
        ex = extract_section_block(lines, idx)
        if not ex.text:
            continue
        if ex.key in seen:
            continue
        seen.add(ex.key)
        excerpts.append(ex)
        for n in range(ex.start_line - 1, ex.end_line):
            covered_indices.add(n)

    for idx, line in enumerate(lines):
        if idx in covered_indices:
            continue
        if not is_action_line(line):
            continue
        ex = extract_action_block(lines, idx)
        if not ex.text:
            continue
        if ex.key in seen:
            continue
        seen.add(ex.key)
        excerpts.append(ex)

    excerpts.sort(key=lambda e: (e.start_line, e.end_line, e.kind))
    return excerpts


def render_txt_summary(source_txt: Path, excerpts: Sequence[Excerpt]) -> str:
    header = [
        f"SOURCE: {source_txt}",
        f"EXCERPTS: {len(excerpts)}",
        "",
    ]
    body: list[str] = []
    for i, ex in enumerate(excerpts, start=1):
        body.append(f"[{i:03d}] {ex.kind} lines {ex.start_line}-{ex.end_line}")
        body.append(ex.text)
        body.append("")
    return "\n".join(header + body).strip() + "\n"


def run_pull(limit: int | None = None, force: bool = False, dry_run: bool = False) -> None:
    run_id = datetime.now().strftime("RUN_%Y%m%dT%H%M%S")
    run_started_at = datetime.now().isoformat(timespec="seconds")

    state = load_state()
    source_state = state.setdefault("sources", {})

    run_rows: list[dict] = []
    staged = 0
    scanned = 0
    skipped_unchanged = 0
    no_hits = 0

    staging_dir = STAGING_ROOT / run_id
    if not dry_run:
        staging_dir.mkdir(parents=True, exist_ok=True)

    for txt_path in iter_agenda_output_texts(AGENDA_OUTPUT_ROOT):
        scanned += 1
        if limit is not None and staged >= limit:
            break

        source_key = str(txt_path)
        source_hash = sha256_file(txt_path)
        prev = source_state.get(source_key, {})

        if not force and prev.get("source_sha256") == source_hash:
            skipped_unchanged += 1
            continue

        text = txt_path.read_text(encoding="utf-8", errors="replace")
        excerpts = extract_minutes_excerpts(text)

        if not excerpts:
            no_hits += 1
            source_state[source_key] = {
                "source_sha256": source_hash,
                "last_status": "no_hits",
                "last_run_id": run_id,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            if not dry_run:
                save_state(state)
            continue

        staged += 1
        machine_code = txt_path.stem
        payload = {
            "run_id": run_id,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "source_txt": source_key,
            "machine_code": machine_code,
            "excerpt_count": len(excerpts),
            "excerpts": [
                {
                    "kind": ex.kind,
                    "start_line": ex.start_line,
                    "end_line": ex.end_line,
                    "text": ex.text,
                }
                for ex in excerpts
            ],
        }
        payload_text = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
        extract_hash = sha256_text(payload_text)

        json_out = staging_dir / f"{machine_code}.minutes.json"
        txt_out = staging_dir / f"{machine_code}.minutes.txt"

        if not dry_run:
            json_out.write_text(payload_text, encoding="utf-8")
            txt_out.write_text(render_txt_summary(txt_path, excerpts), encoding="utf-8")

        row = {
            "run_id": run_id,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "machine_code": machine_code,
            "source_txt": source_key,
            "source_sha256": source_hash,
            "extract_sha256": extract_hash,
            "excerpt_count": len(excerpts),
            "staged_json": str(json_out),
            "staged_txt": str(txt_out),
        }
        run_rows.append(row)

        source_state[source_key] = {
            "source_sha256": source_hash,
            "extract_sha256": extract_hash,
            "last_status": "staged",
            "last_run_id": run_id,
            "excerpt_count": len(excerpts),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "staged_json": str(json_out),
            "staged_txt": str(txt_out),
        }
        if not dry_run:
            append_manifest_rows([row])
            save_state(state)

    if not dry_run:
        manifest_path = staging_dir / "minutes_output_pull_manifest.jsonl"
        with manifest_path.open("w", encoding="utf-8") as f:
            for row in run_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

        summary = {
            "run_id": run_id,
            "started_at": run_started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "scanned_files": scanned,
            "staged_files": staged,
            "skipped_unchanged": skipped_unchanged,
            "no_hits": no_hits,
            "agenda_output_root": str(AGENDA_OUTPUT_ROOT),
            "staging_dir": str(staging_dir),
        }
        (staging_dir / "run_summary.json").write_text(
            json.dumps(summary, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )

    print("=" * 56)
    print("MINUTES OUTPUT PULL SUMMARY")
    print(f"  Run ID: {run_id}")
    print(f"  Agenda output root: {AGENDA_OUTPUT_ROOT}")
    print(f"  Files scanned: {scanned}")
    print(f"  Files staged: {staged}")
    print(f"  Files skipped (unchanged): {skipped_unchanged}")
    print(f"  Files with no minute hits: {no_hits}")
    if dry_run:
        print("  Dry run: yes (no files written)")
    else:
        print(f"  Staging dir: {staging_dir}")
        print(f"  Global manifest: {MANIFEST_FILE}")
        print(f"  State file: {STATE_FILE}")
    print("=" * 56)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pull minute-related material from agenda parser outputs into Minutes staging."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after staging N source files with minute hits.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess all source files even if unchanged in state.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and summarize only, without writing staging artifacts.",
    )
    args = parser.parse_args()

    run_pull(limit=args.limit, force=args.force, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
