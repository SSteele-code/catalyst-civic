#!/usr/bin/env python
"""
Executive Session Pull (Agenda Output Pass)

Stages executive-session material out of existing Agenda parser outputs into:
  _Sources/M1-Meetings/Executive_Session/_staging/<RUN_ID>/

Strict invariant:
  - Pull only (copy/stage relevant material)
  - No DB writes
  - No downstream parsing/enrichment
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


AGENDA_OUTPUT_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Agendas\_output")
EXECUTIVE_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Executive_Session")
SOURCE_ROOT = EXECUTIVE_ROOT / "_sources"
STAGING_ROOT = EXECUTIVE_ROOT / "_staging"

DISCOVERY_MANIFEST_FILE = EXECUTIVE_ROOT / "M1_EXECUTIVE_SESSION_DISCOVERY_MANIFEST.jsonl"
DISCOVERY_SUMMARY_FILE = EXECUTIVE_ROOT / "executive_session_discovery_summary.json"

STATE_FILE = EXECUTIVE_ROOT / "executive_session_output_pull_state.json"
MANIFEST_FILE = EXECUTIVE_ROOT / "M1_EXECUTIVE_SESSION_OUTPUT_PULL_MANIFEST.jsonl"

SOURCE_LANE = "agenda_output_executive_session_sections"
SCHEMA_VERSION = "m1.executive_session.pull.v1"


@dataclass(frozen=True)
class SessionExcerpt:
    session_key: str
    candidate_status: str
    heading_line_number: int
    heading_text: str
    start_line: int
    end_line: int
    text: str
    reason_line_count: int
    reason_categories: list[str]
    code_references: list[str]
    reason_lines: list[dict[str, Any]]
    heading_line_match: bool
    reason_line_matches: int
    reason_line_total: int


def clean_line(value: str) -> str:
    return " ".join((value or "").replace("\t", " ").split())


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"sources": {}}
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"sources": {}}
    if not isinstance(payload, dict):
        return {"sources": {}}
    payload.setdefault("sources", {})
    return payload


def save_state(state: dict[str, Any]) -> None:
    EXECUTIVE_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def append_manifest_rows(rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        return
    EXECUTIVE_ROOT.mkdir(parents=True, exist_ok=True)
    with MANIFEST_FILE.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")


def parse_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def resolve_discovery_run_id(explicit_run_id: str | None) -> str:
    if explicit_run_id:
        return explicit_run_id

    if DISCOVERY_SUMMARY_FILE.exists():
        try:
            summary = json.loads(DISCOVERY_SUMMARY_FILE.read_text(encoding="utf-8"))
            run_id = str(summary.get("run_id") or "").strip()
            if run_id:
                return run_id
        except Exception:
            pass

    runs_root = EXECUTIVE_ROOT / "_output" / "_runs"
    if runs_root.exists():
        run_dirs = sorted(
            [p.name for p in runs_root.iterdir() if p.is_dir() and p.name.startswith("RUN-ES-DISCOVERY-")]
        )
        if run_dirs:
            return run_dirs[-1]

    raise FileNotFoundError("Unable to resolve discovery run id; no summary or run folders found.")


def load_discovery_rows(run_id: str, include_adjacent: bool) -> list[dict[str, Any]]:
    allowed_status = {"candidate"}
    if include_adjacent:
        allowed_status.add("adjacent")

    run_manifest = (
        EXECUTIVE_ROOT / "_output" / "_runs" / run_id / "executive_session_discovery_manifest.jsonl"
    )
    rows: list[dict[str, Any]] = []
    if run_manifest.exists():
        rows = parse_jsonl(run_manifest)
    else:
        top_rows = parse_jsonl(DISCOVERY_MANIFEST_FILE)
        rows = [r for r in top_rows if str(r.get("run_id") or "").strip() == run_id]

    out: list[dict[str, Any]] = []
    for row in rows:
        status = str(row.get("candidate_status") or "").strip().lower()
        if status not in allowed_status:
            continue
        agenda_code = str(row.get("agenda_code") or "").strip()
        source_txt = str(row.get("source_txt") or "").strip()
        if not agenda_code or not source_txt:
            continue
        out.append(row)
    return out


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


def detect_source_txt_for_agenda(agenda_code: str) -> Path | None:
    candidate = AGENDA_OUTPUT_ROOT / agenda_code / f"{agenda_code}.txt"
    if candidate.exists():
        return candidate
    return None


def build_session_excerpt(lines: Sequence[str], row: dict[str, Any]) -> SessionExcerpt | None:
    heading_line_number = int(row.get("heading_line_number") or 0)
    if heading_line_number < 1 or heading_line_number > len(lines):
        return None

    heading_text = str(row.get("heading_text") or "").strip()
    source_heading = clean_line(lines[heading_line_number - 1])
    heading_line_match = clean_line(heading_text).lower() == source_heading.lower()

    raw_reason_lines = row.get("reason_lines")
    reason_lines: list[dict[str, Any]] = []
    if isinstance(raw_reason_lines, list):
        for item in raw_reason_lines:
            if isinstance(item, dict):
                reason_lines.append(item)

    reason_line_numbers: list[int] = []
    reason_line_total = 0
    reason_line_matches = 0
    for reason in reason_lines:
        line_no = reason.get("line_number")
        try:
            line_no_int = int(line_no)
        except Exception:
            continue
        if line_no_int < 1 or line_no_int > len(lines):
            continue
        reason_line_numbers.append(line_no_int)
        reason_line_total += 1
        expected_text = clean_line(str(reason.get("text") or ""))
        src_text = clean_line(lines[line_no_int - 1])
        if expected_text and (
            expected_text.lower() == src_text.lower()
            or expected_text.lower() in src_text.lower()
            or src_text.lower() in expected_text.lower()
        ):
            reason_line_matches += 1

    reason_line_count = len(reason_lines)
    core_start = min([heading_line_number] + reason_line_numbers) if reason_line_numbers else heading_line_number
    core_end = max([heading_line_number] + reason_line_numbers) if reason_line_numbers else heading_line_number

    start_line = max(1, core_start - 2)
    end_line = min(len(lines), core_end + 6)
    block = "\n".join(line.rstrip() for line in lines[start_line - 1 : end_line]).strip()
    if not block:
        return None

    candidate_status = str(row.get("candidate_status") or "").strip().lower()
    reason_categories = row.get("reason_categories")
    if not isinstance(reason_categories, list):
        reason_categories = []
    reason_categories = [str(x).strip() for x in reason_categories if str(x).strip()]

    code_references = row.get("code_references")
    if not isinstance(code_references, list):
        code_references = []
    code_references = [str(x).strip() for x in code_references if str(x).strip()]

    session_key = f"{heading_line_number}|{clean_line(heading_text).lower()}"

    return SessionExcerpt(
        session_key=session_key,
        candidate_status=candidate_status,
        heading_line_number=heading_line_number,
        heading_text=heading_text,
        start_line=start_line,
        end_line=end_line,
        text=block,
        reason_line_count=reason_line_count,
        reason_categories=reason_categories,
        code_references=code_references,
        reason_lines=reason_lines,
        heading_line_match=heading_line_match,
        reason_line_matches=reason_line_matches,
        reason_line_total=reason_line_total,
    )


def render_source_bundle_txt(source_txt: Path, excerpts: Sequence[SessionExcerpt]) -> str:
    header = [
        f"SOURCE: {source_txt}",
        f"SESSIONS: {len(excerpts)}",
        "",
    ]
    body: list[str] = []
    for i, ex in enumerate(excerpts, start=1):
        cats = ", ".join(ex.reason_categories) if ex.reason_categories else "none"
        codes = ", ".join(ex.code_references) if ex.code_references else "none"
        body.append(
            f"[{i:03d}] line {ex.heading_line_number} lines {ex.start_line}-{ex.end_line} "
            f"(status={ex.candidate_status}, reason_lines={ex.reason_line_count})"
        )
        body.append(f"HEADING: {ex.heading_text}")
        body.append(f"CATEGORIES: {cats}")
        body.append(f"CODE_REFERENCES: {codes}")
        body.append(ex.text)
        body.append("")
    return "\n".join(header + body).strip() + "\n"


def render_staging_summary(source_txt: Path, excerpts: Sequence[SessionExcerpt], integrity: dict[str, Any]) -> str:
    header = [
        f"SOURCE: {source_txt}",
        f"SESSIONS: {len(excerpts)}",
        f"INTEGRITY_SCORE: {integrity.get('integrity_score')}",
        "",
    ]
    body: list[str] = []
    for i, ex in enumerate(excerpts, start=1):
        body.append(
            f"[{i:03d}] {ex.candidate_status} heading_line={ex.heading_line_number} "
            f"range={ex.start_line}-{ex.end_line}"
        )
        body.append(f"HEADING: {ex.heading_text}")
        body.append(ex.text)
        body.append("")
    return "\n".join(header + body).strip() + "\n"


def write_source_bundle(
    agenda_code: str,
    source_txt: Path,
    excerpts: Sequence[SessionExcerpt],
    dry_run: bool,
) -> tuple[Path, Path | None, int]:
    packet_dir = SOURCE_ROOT / agenda_code
    out_txt = packet_dir / f"{agenda_code}.executive_session.txt"
    src_factsheet = source_txt.parent / f"{agenda_code}.factsheet.json"
    out_factsheet = packet_dir / f"{agenda_code}.factsheet.json"
    has_factsheet = src_factsheet.exists()

    planned_files = 2 if has_factsheet else 1
    if dry_run:
        return out_txt, (out_factsheet if has_factsheet else None), planned_files

    SOURCE_ROOT.mkdir(parents=True, exist_ok=True)
    if packet_dir.exists():
        shutil.rmtree(packet_dir)
    packet_dir.mkdir(parents=True, exist_ok=True)

    out_txt.write_text(render_source_bundle_txt(source_txt, excerpts), encoding="utf-8")
    written_files = 1

    factsheet_path: Path | None = None
    if has_factsheet:
        shutil.copy2(src_factsheet, out_factsheet)
        factsheet_path = out_factsheet
        written_files += 1

    return out_txt, factsheet_path, written_files


def compute_integrity(excerpts: Sequence[SessionExcerpt]) -> dict[str, Any]:
    anchor_total = len(excerpts)
    anchor_matches = sum(1 for ex in excerpts if ex.heading_line_match)
    reason_line_total = sum(ex.reason_line_total for ex in excerpts)
    reason_line_matches = sum(ex.reason_line_matches for ex in excerpts)
    nonempty_excerpt_count = sum(1 for ex in excerpts if ex.text.strip())

    anchor_ratio = (anchor_matches / anchor_total) if anchor_total else 0.0
    reason_ratio = (reason_line_matches / reason_line_total) if reason_line_total else 1.0
    coverage_ratio = min(1.0, nonempty_excerpt_count / anchor_total) if anchor_total else 0.0

    integrity_score = (0.70 * anchor_ratio) + (0.20 * reason_ratio) + (0.10 * coverage_ratio)
    return {
        "anchor_total": anchor_total,
        "anchor_matches": anchor_matches,
        "anchor_match_ratio": round(anchor_ratio, 4),
        "reason_line_total": reason_line_total,
        "reason_line_matches": reason_line_matches,
        "reason_line_match_ratio": round(reason_ratio, 4),
        "nonempty_excerpt_count": nonempty_excerpt_count,
        "excerpt_coverage_ratio": round(coverage_ratio, 4),
        "integrity_score": round(integrity_score, 4),
    }


def run_pull(
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    include_adjacent: bool = False,
    discovery_run_id: str | None = None,
    integrity_threshold: float = 0.95,
    enforce_integrity: bool = True,
) -> int:
    run_id = datetime.now().strftime("RUN_%Y%m%dT%H%M%S")
    run_started_at = datetime.now().isoformat(timespec="seconds")

    discovery_run = resolve_discovery_run_id(discovery_run_id)
    discovery_rows = load_discovery_rows(discovery_run, include_adjacent=include_adjacent)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in discovery_rows:
        agenda_code = str(row.get("agenda_code") or "").strip()
        if agenda_code:
            grouped[agenda_code].append(row)

    agenda_index = {p.stem: p for p in iter_agenda_output_texts(AGENDA_OUTPUT_ROOT)}

    state = load_state()
    source_state = state.setdefault("sources", {})

    run_rows: list[dict[str, Any]] = []
    scanned = 0
    staged = 0
    skipped_unchanged = 0
    missing_source = 0
    no_hits = 0
    integrity_pass_count = 0
    source_files_written = 0
    source_factsheets_copied = 0

    staging_dir = STAGING_ROOT / run_id
    if not dry_run:
        staging_dir.mkdir(parents=True, exist_ok=True)

    for agenda_code in sorted(grouped):
        scanned += 1
        if limit is not None and staged >= limit:
            break

        packet_rows = sorted(
            grouped[agenda_code],
            key=lambda r: (int(r.get("heading_line_number") or 0), str(r.get("heading_text") or "")),
        )

        source_txt = None
        for row in packet_rows:
            source_value = str(row.get("source_txt") or "").strip()
            if source_value and Path(source_value).exists():
                source_txt = Path(source_value)
                break
        if source_txt is None:
            source_txt = agenda_index.get(agenda_code) or detect_source_txt_for_agenda(agenda_code)
        if source_txt is None or not source_txt.exists():
            missing_source += 1
            continue

        source_key = str(source_txt)
        source_sha256 = sha256_file(source_txt)
        discovery_sha256 = sha256_text(
            json.dumps(packet_rows, ensure_ascii=True, sort_keys=True)
        )
        prev = source_state.get(source_key, {})
        if (
            not force
            and prev.get("source_sha256") == source_sha256
            and prev.get("discovery_sha256") == discovery_sha256
            and prev.get("last_status") == "staged"
        ):
            skipped_unchanged += 1
            continue

        lines = source_txt.read_text(encoding="utf-8", errors="replace").splitlines()
        seen_session_keys: set[str] = set()
        excerpts: list[SessionExcerpt] = []
        for row in packet_rows:
            ex = build_session_excerpt(lines, row)
            if ex is None:
                continue
            if ex.session_key in seen_session_keys:
                continue
            seen_session_keys.add(ex.session_key)
            excerpts.append(ex)

        if not excerpts:
            no_hits += 1
            source_state[source_key] = {
                "source_sha256": source_sha256,
                "discovery_sha256": discovery_sha256,
                "last_status": "no_hits",
                "last_run_id": run_id,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            if not dry_run:
                save_state(state)
            continue

        staged += 1
        integrity = compute_integrity(excerpts)
        integrity_score = float(integrity.get("integrity_score") or 0.0)
        integrity_pass = integrity_score >= integrity_threshold
        if integrity_pass:
            integrity_pass_count += 1

        source_bundle_txt, source_bundle_factsheet, source_bundle_count = write_source_bundle(
            agenda_code=agenda_code,
            source_txt=source_txt,
            excerpts=excerpts,
            dry_run=dry_run,
        )
        source_files_written += source_bundle_count
        if source_bundle_factsheet is not None:
            source_factsheets_copied += 1

        status_counts = Counter(ex.candidate_status for ex in excerpts)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "executive_session_pull_record",
            "run_id": run_id,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "source_lane": SOURCE_LANE,
            "discovery_run_id": discovery_run,
            "agenda_code": agenda_code,
            "machine_code": agenda_code,
            "source_txt": str(source_txt),
            "source_sha256": source_sha256,
            "source_bundle_txt": str(source_bundle_txt),
            "source_bundle_factsheet": str(source_bundle_factsheet) if source_bundle_factsheet else None,
            "source_bundle_file_count": source_bundle_count,
            "discovery_sha256": discovery_sha256,
            "session_count": len(excerpts),
            "status_counts": dict(status_counts),
            "integrity": {
                **integrity,
                "threshold": integrity_threshold,
                "pass": integrity_pass,
            },
            "sessions": [
                {
                    "session_key": ex.session_key,
                    "candidate_status": ex.candidate_status,
                    "heading_line_number": ex.heading_line_number,
                    "heading_text": ex.heading_text,
                    "start_line": ex.start_line,
                    "end_line": ex.end_line,
                    "reason_line_count": ex.reason_line_count,
                    "reason_categories": ex.reason_categories,
                    "code_references": ex.code_references,
                    "reason_lines": ex.reason_lines,
                    "heading_line_match": ex.heading_line_match,
                    "reason_line_matches": ex.reason_line_matches,
                    "reason_line_total": ex.reason_line_total,
                    "text": ex.text,
                }
                for ex in excerpts
            ],
        }
        payload_text = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
        extract_sha256 = sha256_text(payload_text)

        json_out = staging_dir / f"{agenda_code}.executive_session.json"
        txt_out = staging_dir / f"{agenda_code}.executive_session.txt"
        if not dry_run:
            json_out.write_text(payload_text, encoding="utf-8")
            txt_out.write_text(render_staging_summary(source_txt, excerpts, integrity), encoding="utf-8")

        manifest_row = {
            "run_id": run_id,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "schema_version": SCHEMA_VERSION,
            "machine_code": agenda_code,
            "source_txt": str(source_txt),
            "source_sha256": source_sha256,
            "extract_sha256": extract_sha256,
            "discovery_run_id": discovery_run,
            "session_count": len(excerpts),
            "status_counts": dict(status_counts),
            "integrity_score": integrity_score,
            "integrity_threshold": integrity_threshold,
            "integrity_pass": integrity_pass,
            "source_bundle_txt": str(source_bundle_txt),
            "source_bundle_factsheet": str(source_bundle_factsheet) if source_bundle_factsheet else None,
            "staged_json": str(json_out),
            "staged_txt": str(txt_out),
        }
        run_rows.append(manifest_row)

        source_state[source_key] = {
            "source_sha256": source_sha256,
            "discovery_sha256": discovery_sha256,
            "extract_sha256": extract_sha256,
            "last_status": "staged",
            "last_run_id": run_id,
            "session_count": len(excerpts),
            "integrity_score": integrity_score,
            "integrity_pass": integrity_pass,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "source_bundle_txt": str(source_bundle_txt),
            "source_bundle_factsheet": str(source_bundle_factsheet) if source_bundle_factsheet else None,
            "staged_json": str(json_out),
            "staged_txt": str(txt_out),
        }

        if not dry_run:
            append_manifest_rows([manifest_row])
            save_state(state)

    integrity_rate = (integrity_pass_count / staged) if staged else 0.0
    integrity_gate_pass = integrity_rate >= integrity_threshold

    if not dry_run:
        manifest_path = staging_dir / "executive_session_output_pull_manifest.jsonl"
        with manifest_path.open("w", encoding="utf-8") as f:
            for row in run_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

        summary = {
            "run_id": run_id,
            "started_at": run_started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "schema_version": SCHEMA_VERSION,
            "source_lane": SOURCE_LANE,
            "discovery_run_id": discovery_run,
            "scanned_packets": scanned,
            "staged_packets": staged,
            "skipped_unchanged": skipped_unchanged,
            "missing_source": missing_source,
            "no_hits": no_hits,
            "integrity_threshold": integrity_threshold,
            "records_passing_integrity": integrity_pass_count,
            "document_integrity_rate": round(integrity_rate, 4),
            "integrity_gate_pass": integrity_gate_pass,
            "source_files_written": source_files_written,
            "source_factsheets_copied": source_factsheets_copied,
            "agenda_output_root": str(AGENDA_OUTPUT_ROOT),
            "discovery_manifest": str(DISCOVERY_MANIFEST_FILE),
            "staging_dir": str(staging_dir),
        }
        (staging_dir / "run_summary.json").write_text(
            json.dumps(summary, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )

    print("=" * 64)
    print("EXECUTIVE SESSION OUTPUT PULL SUMMARY")
    print(f"  Run ID: {run_id}")
    print(f"  Discovery run ID: {discovery_run}")
    print(f"  Packets scanned: {scanned}")
    print(f"  Packets staged: {staged}")
    print(f"  Packets skipped (unchanged): {skipped_unchanged}")
    print(f"  Packets missing source txt: {missing_source}")
    print(f"  Packets with no extracted sessions: {no_hits}")
    print(f"  Integrity threshold: {integrity_threshold:.2f}")
    print(f"  Records passing integrity: {integrity_pass_count}/{staged}")
    print(f"  Document integrity rate: {integrity_rate:.4f}")
    print(f"  Integrity gate pass: {integrity_gate_pass}")
    if dry_run:
        print("  Dry run: yes (no files written)")
    else:
        print(f"  Executive source root: {SOURCE_ROOT}")
        print(f"  Source files written: {source_files_written}")
        print(f"  Source factsheets copied: {source_factsheets_copied}")
        print(f"  Staging dir: {staging_dir}")
        print(f"  Global manifest: {MANIFEST_FILE}")
        print(f"  State file: {STATE_FILE}")
    print("=" * 64)

    if enforce_integrity and staged > 0 and not integrity_gate_pass:
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pull executive-session material from agenda parser outputs into Executive_Session staging."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after staging N source packets with executive-session hits.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess source packets even if unchanged in state.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and score without writing staging/manifests/state.",
    )
    parser.add_argument(
        "--include-adjacent",
        action="store_true",
        help="Include discovery rows marked adjacent (default: candidate only).",
    )
    parser.add_argument(
        "--discovery-run-id",
        default=None,
        help="Discovery run id to pull from (default: latest from discovery summary).",
    )
    parser.add_argument(
        "--integrity-threshold",
        type=float,
        default=0.95,
        help="Minimum required run-level document integrity rate target (default: 0.95).",
    )
    parser.add_argument(
        "--no-enforce-integrity",
        action="store_true",
        help="Do not fail exit code when run-level integrity rate is below threshold.",
    )
    args = parser.parse_args()

    return run_pull(
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
        include_adjacent=args.include_adjacent,
        discovery_run_id=args.discovery_run_id,
        integrity_threshold=float(args.integrity_threshold),
        enforce_integrity=not bool(args.no_enforce_integrity),
    )


if __name__ == "__main__":
    raise SystemExit(main())

