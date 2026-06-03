#!/usr/bin/env python
"""
Ordinance/Resolution Pull (Agenda Output Pass)

Stages ordinance and resolution *metadata only* from Agenda output packets into:
  _Sources/M1-Meetings/Ordinance_Resolution/_staging/<RUN_ID>/

Strict invariant:
  - Pull only (no downstream parse/enrichment/db writes)
  - Metadata-only extraction (document name/id/title + meeting linkage)
  - No full ordinance/resolution body extraction
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence


AGENDA_OUTPUT_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Agendas\_output")
OR_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Ordinance_Resolution")
SOURCE_ROOT = OR_ROOT / "_sources"
STAGING_ROOT = OR_ROOT / "_staging"

STATE_FILE = OR_ROOT / "ordinance_resolution_output_pull_state.json"
MANIFEST_FILE = OR_ROOT / "M1_ORDINANCE_RESOLUTION_OUTPUT_PULL_MANIFEST.jsonl"

AG_CODE_RE = re.compile(r"^M1\.AG\.(\d{6})\.(\d{8})\.(\d{8})$", re.IGNORECASE)
PAGE_LINE_RE = re.compile(r"^\s*---\s*PAGE\b", re.IGNORECASE)
PACKET_CODE_RE = re.compile(r"^M1\.AG\.\d{8}\.[A-F0-9]{16}$", re.IGNORECASE)

HEADER_ORDINANCE_RE = re.compile(r'^\s*["“\(\[]*\s*ORDINANCE\s+NO\b', re.IGNORECASE)
HEADER_RESOLUTION_NO_RE = re.compile(r'^\s*["“\(\[]*\s*RESOLUTION\s+NO\b', re.IGNORECASE)
HEADER_RESOLUTION_STANDALONE_RE = re.compile(r'^\s*["“\(\[]*\s*RESOLUTION\s*$', re.IGNORECASE)
HEADER_A_RESOLUTION_RE = re.compile(r'^\s*["“\(\[]*\s*A\s+RESOLUTION\b', re.IGNORECASE)
HEADER_AN_ORDINANCE_RE = re.compile(r'^\s*["“\(\[]*\s*AN?\s+ORDINANCE\b', re.IGNORECASE)

DOC_NUMBER_RE = re.compile(
    r"\b([OR0Q9]-\d{4}-[0-9Xx]{2}(?:\.[0-9Xx]+)?(?:-[0-9Xx]{2})?|R-\d{4}-\d{2}(?:-\d{2})?)\b"
)
ORDINANCE_NO_CAPTURE_RE = re.compile(r"\bORDINANCE\s+NO\.?\s*[:;]?\s*([A-Z0-9.\-]+)?", re.IGNORECASE)
RESOLUTION_NO_CAPTURE_RE = re.compile(r"\bRESOLUTION\s+NO\.?\s*[:;]?\s*([A-Z0-9.\-]+)?", re.IGNORECASE)


@dataclass(frozen=True)
class ORMetadata:
    document_type: str
    document_number: str | None
    document_number_raw: str | None
    title: str | None
    header_line: str
    start_line: int
    end_line: int
    page_hint: str | None
    match_pattern: str
    confidence: float

    @property
    def key(self) -> str:
        parts = [
            self.document_type,
            self.document_number or "",
            (self.title or "").upper(),
            str(self.start_line),
            self.match_pattern,
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_packet_code_from_dir(packet_dir: Path) -> str | None:
    name = packet_dir.name.strip()
    if PACKET_CODE_RE.match(name):
        return name
    return None


def iter_agenda_packets(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    out: list[Path] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        packet_code = parse_packet_code_from_dir(d)
        if not packet_code:
            continue
        txt = d / f"{packet_code}.txt"
        factsheet = d / f"{packet_code}.factsheet.json"
        if txt.exists() and factsheet.exists():
            out.append(d)
    return out


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"sources": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"sources": {}}


def save_state(state: dict) -> None:
    OR_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def append_manifest_rows(rows: Sequence[dict]) -> None:
    if not rows:
        return
    OR_ROOT.mkdir(parents=True, exist_ok=True)
    with MANIFEST_FILE.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")


def ymd_to_iso(ymd: str) -> str | None:
    if not re.fullmatch(r"\d{8}", ymd or ""):
        return None
    try:
        return datetime.strptime(ymd, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def source_pdf_code_from_factsheet(facts: dict) -> str | None:
    name = str(facts.get("source_pdf_original_name") or "").strip()
    if not name.lower().endswith(".pdf"):
        return None
    return name[:-4]


def meeting_date_from_source_pdf_code(source_pdf_code: str | None) -> str | None:
    if not source_pdf_code:
        return None
    m = AG_CODE_RE.match(source_pdf_code)
    if not m:
        return None
    created_ymd = m.group(2)
    return ymd_to_iso(created_ymd)


def normalize_document_number(raw: str | None, document_type: str) -> tuple[str | None, str | None]:
    if raw is None:
        return None, None
    cleaned = normalize_ws(raw).strip("()[]{}:;,.")
    cleaned = cleaned.replace(" ", "")
    if not cleaned:
        return None, None
    cleaned = cleaned.upper()

    lead = cleaned[0]
    rest = cleaned[1:] if len(cleaned) > 1 else ""
    if document_type == "ORDINANCE" and lead in {"0", "Q", "9"}:
        cleaned = "O" + rest

    if document_type == "RESOLUTION" and lead == "0":
        cleaned = "O" + rest

    return cleaned, raw


def clean_line(line: str) -> str:
    return normalize_ws(line).strip(" -\t")


def looks_like_title(line: str, document_type: str) -> bool:
    s = clean_line(line)
    if not s:
        return False
    if PAGE_LINE_RE.match(s):
        return False
    up = s.upper()
    if HEADER_ORDINANCE_RE.match(s) or HEADER_RESOLUTION_NO_RE.match(s):
        return False
    if document_type == "RESOLUTION":
        if up.startswith("A RESOLUTION"):
            return True
    if document_type == "ORDINANCE":
        if up.startswith("AN ORDINANCE") or up.startswith("A ORDINANCE"):
            return True
    if "AMENDMENT" in up or "TITLE " in up:
        return True
    if len(s) >= 16 and sum(c.isalpha() for c in s) >= 8:
        return True
    return False


def extract_document_number(header_line: str, near_lines: list[str], document_type: str) -> tuple[str | None, str | None]:
    line = clean_line(header_line)
    m = DOC_NUMBER_RE.search(line)
    if m:
        return normalize_document_number(m.group(1), document_type)

    capture = None
    if document_type == "ORDINANCE":
        cm = ORDINANCE_NO_CAPTURE_RE.search(line)
        if cm:
            capture = cm.group(1)
    else:
        cm = RESOLUTION_NO_CAPTURE_RE.search(line)
        if cm:
            capture = cm.group(1)
    if capture:
        normalized, raw = normalize_document_number(capture, document_type)
        if normalized:
            return normalized, raw

    for near in near_lines:
        nm = DOC_NUMBER_RE.search(near)
        if nm:
            return normalize_document_number(nm.group(1), document_type)
    return None, None


def extract_title(lines: list[str], start_idx: int, document_type: str) -> str | None:
    limit = min(len(lines), start_idx + 8)
    for i in range(start_idx, limit):
        candidate = clean_line(lines[i])
        if not candidate:
            continue
        if looks_like_title(candidate, document_type):
            return candidate[:220]
    return None


def find_page_hint(lines: list[str], start_idx: int) -> str | None:
    for i in range(start_idx, max(-1, start_idx - 12), -1):
        if i < 0 or i >= len(lines):
            continue
        s = clean_line(lines[i])
        if PAGE_LINE_RE.match(s):
            return s
    return None


def detect_metadata_records(text: str) -> list[ORMetadata]:
    lines = text.splitlines()
    out: list[ORMetadata] = []
    seen: set[str] = set()

    for idx, raw in enumerate(lines):
        line = clean_line(raw)
        if not line:
            continue

        pattern = ""
        document_type = ""
        if HEADER_ORDINANCE_RE.match(line):
            pattern = "header_ordinance_no"
            document_type = "ORDINANCE"
        elif HEADER_RESOLUTION_NO_RE.match(line):
            pattern = "header_resolution_no"
            document_type = "RESOLUTION"
        elif HEADER_RESOLUTION_STANDALONE_RE.match(line):
            pattern = "header_resolution_standalone"
            document_type = "RESOLUTION"
        elif HEADER_A_RESOLUTION_RE.match(line):
            pattern = "header_a_resolution"
            document_type = "RESOLUTION"
        elif HEADER_AN_ORDINANCE_RE.match(line):
            pattern = "header_an_ordinance"
            document_type = "ORDINANCE"
        else:
            continue

        near_lines = [clean_line(lines[j]) for j in range(idx, min(len(lines), idx + 6))]
        doc_number, doc_number_raw = extract_document_number(line, near_lines, document_type)
        title = extract_title(lines, idx, document_type)
        page_hint = find_page_hint(lines, idx)

        confidence = 0.86
        if doc_number and title:
            confidence = 0.97
        elif doc_number:
            confidence = 0.94
        elif title:
            confidence = 0.90

        row = ORMetadata(
            document_type=document_type,
            document_number=doc_number,
            document_number_raw=doc_number_raw,
            title=title,
            header_line=line[:220],
            start_line=idx + 1,
            end_line=min(len(lines), idx + 6),
            page_hint=page_hint,
            match_pattern=pattern,
            confidence=confidence,
        )
        if row.key in seen:
            continue
        seen.add(row.key)
        out.append(row)

    out.sort(key=lambda r: (r.start_line, r.document_type, r.document_number or ""))
    return out


def render_source_summary(packet_code: str, source_pdf_code: str | None, meeting_date: str | None, records: list[ORMetadata]) -> str:
    lines: list[str] = []
    lines.append(f"PACKET_CODE: {packet_code}")
    lines.append(f"SOURCE_PDF_CODE: {source_pdf_code or ''}")
    lines.append(f"ANCHOR_MEETING_DATE: {meeting_date or ''}")
    lines.append(f"DOCUMENT_COUNT: {len(records)}")
    lines.append("")
    for i, r in enumerate(records, start=1):
        lines.append(
            f"[DOC{i:03d}] {r.document_type} id={r.document_number or ''} "
            f"title={r.title or ''} line={r.start_line} conf={r.confidence:.2f} pattern={r.match_pattern}"
        )
    return "\n".join(lines).strip() + "\n"


def write_source_bundle(
    packet_code: str,
    source_summary_text: str,
    source_factsheet: Path,
    dry_run: bool,
) -> tuple[Path, Path]:
    dest_dir = SOURCE_ROOT / packet_code
    summary_out = dest_dir / f"{packet_code}.ordinance_resolution_source.txt"
    facts_out = dest_dir / f"{packet_code}.factsheet.json"

    if dry_run:
        return summary_out, facts_out

    SOURCE_ROOT.mkdir(parents=True, exist_ok=True)
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    summary_out.write_text(source_summary_text, encoding="utf-8")
    shutil.copy2(source_factsheet, facts_out)
    return summary_out, facts_out


def run_pull(limit: int | None = None, force: bool = False, dry_run: bool = False) -> None:
    run_id = datetime.now().strftime("RUN_%Y%m%dT%H%M%S")
    started_at = datetime.now().isoformat(timespec="seconds")
    staging_dir = STAGING_ROOT / run_id

    state = load_state()
    state_sources = state.setdefault("sources", {})

    discovered = 0
    candidates = 0
    staged = 0
    skipped_unchanged = 0
    failed = 0
    source_files_written = 0
    manifest_rows: list[dict] = []
    failures: list[dict] = []

    packet_dirs = list(iter_agenda_packets(AGENDA_OUTPUT_ROOT))

    if not dry_run:
        STAGING_ROOT.mkdir(parents=True, exist_ok=True)
        staging_dir.mkdir(parents=True, exist_ok=True)

    for packet_dir in packet_dirs:
        if limit is not None and staged >= limit:
            break

        discovered += 1
        packet_code = packet_dir.name
        txt_path = packet_dir / f"{packet_code}.txt"
        facts_path = packet_dir / f"{packet_code}.factsheet.json"

        try:
            txt_sha = sha256_file(txt_path)
            text = txt_path.read_text(encoding="utf-8", errors="replace")
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
        except Exception as exc:
            failed += 1
            failures.append(
                {
                    "run_id": run_id,
                    "failed_at": datetime.now().isoformat(timespec="seconds"),
                    "packet_code": packet_code,
                    "packet_path": str(packet_dir),
                    "error": f"read_error: {exc}",
                }
            )
            continue

        source_pdf_code = source_pdf_code_from_factsheet(facts)
        if not source_pdf_code:
            failed += 1
            failures.append(
                {
                    "run_id": run_id,
                    "failed_at": datetime.now().isoformat(timespec="seconds"),
                    "packet_code": packet_code,
                    "packet_path": str(packet_dir),
                    "error": "missing_source_pdf_code",
                }
            )
            continue

        records = detect_metadata_records(text)
        if not records:
            continue

        candidates += 1
        source_hash = sha256_text(txt_sha + "|" + source_pdf_code)
        prev = state_sources.get(packet_code, {})
        if (
            not force
            and prev.get("source_hash") == source_hash
            and str(prev.get("last_status") or "") == "staged"
        ):
            skipped_unchanged += 1
            continue

        meeting_date = meeting_date_from_source_pdf_code(source_pdf_code)
        summary_text = render_source_summary(packet_code, source_pdf_code, meeting_date, records)
        source_summary_path, source_factsheet_path = write_source_bundle(
            packet_code=packet_code,
            source_summary_text=summary_text,
            source_factsheet=facts_path,
            dry_run=dry_run,
        )
        if not dry_run:
            source_files_written += 2

        payload = {
            "schema_version": "m1.ordinance_resolution.pull.v1",
            "record_type": "ordinance_resolution_pull_record",
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "run_id": run_id,
            "source_lane": "agenda_output_ordinance_resolution_metadata_only",
            "jurisdiction": "Richlands",
            "packet_code": packet_code,
            "source_pdf_code": source_pdf_code,
            "anchor_meeting_date": meeting_date,
            "source_txt": str(source_summary_path),
            "source_factsheet": str(source_factsheet_path),
            "source_type": "official_document_metadata_only",
            "source_packet_dir": str(packet_dir),
            "source_packet_txt": str(txt_path),
            "source_packet_txt_sha256": txt_sha,
            "document_count": len(records),
            "documents": [
                {
                    "document_type": r.document_type,
                    "document_number": r.document_number,
                    "document_number_raw": r.document_number_raw,
                    "title": r.title,
                    "header_line": r.header_line,
                    "start_line": r.start_line,
                    "end_line": r.end_line,
                    "page_hint": r.page_hint,
                    "match_pattern": r.match_pattern,
                    "confidence": r.confidence,
                    "meeting_ref": {
                        "anchor_meeting_date": meeting_date,
                        "source_pdf_code": source_pdf_code,
                        "packet_code": packet_code,
                    },
                }
                for r in records
            ],
        }
        payload_text = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
        extract_hash = sha256_text(payload_text)

        stage_json = staging_dir / f"{packet_code}.ordinance_resolution.json"
        stage_txt = staging_dir / f"{packet_code}.ordinance_resolution.txt"
        if not dry_run:
            stage_json.write_text(payload_text, encoding="utf-8")
            stage_txt.write_text(summary_text, encoding="utf-8")

        staged += 1
        row = {
            "run_id": run_id,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "packet_code": packet_code,
            "source_pdf_code": source_pdf_code,
            "anchor_meeting_date": meeting_date,
            "source_packet_txt": str(txt_path),
            "source_packet_txt_sha256": txt_sha,
            "source_txt": str(source_summary_path),
            "source_factsheet": str(source_factsheet_path),
            "source_type": "official_document_metadata_only",
            "document_count": len(records),
            "extract_hash": extract_hash,
            "staging_json": str(stage_json),
            "staging_txt": str(stage_txt),
        }
        manifest_rows.append(row)

        state_sources[packet_code] = {
            "last_run_id": run_id,
            "last_status": "staged",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "source_hash": source_hash,
            "source_pdf_code": source_pdf_code,
            "anchor_meeting_date": meeting_date,
            "document_count": len(records),
            "extract_hash": extract_hash,
            "source_packet_txt": str(txt_path),
            "source_txt": str(source_summary_path),
            "staging_json": str(stage_json),
        }
        if not dry_run:
            save_state(state)

    if not dry_run:
        manifest_path = staging_dir / "ordinance_resolution_output_pull_manifest.jsonl"
        with manifest_path.open("w", encoding="utf-8") as f:
            for row in manifest_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")
        append_manifest_rows(manifest_rows)

        if failures:
            failure_out = staging_dir / "ordinance_resolution_output_pull_failures.jsonl"
            with failure_out.open("w", encoding="utf-8") as f:
                for row in failures:
                    f.write(json.dumps(row, ensure_ascii=True) + "\n")

        summary = {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "source_lane": "agenda_output_ordinance_resolution_metadata_only",
            "agenda_output_root": str(AGENDA_OUTPUT_ROOT),
            "ordinance_resolution_source_root": str(SOURCE_ROOT),
            "staging_root": str(STAGING_ROOT),
            "packets_discovered": discovered,
            "packets_with_candidates": candidates,
            "records_staged": staged,
            "skipped_unchanged": skipped_unchanged,
            "failed": failed,
            "source_files_written": source_files_written,
            "force": force,
            "dry_run": dry_run,
            "limit": limit,
        }
        (staging_dir / "run_summary.json").write_text(
            json.dumps(summary, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )

    print("=" * 72)
    print("ORDINANCE/RESOLUTION PULL SUMMARY")
    print(f"  Run ID: {run_id}")
    print("  Source lane: agenda_output_ordinance_resolution_metadata_only")
    print(f"  Agenda output root: {AGENDA_OUTPUT_ROOT}")
    print(f"  Source root: {SOURCE_ROOT}")
    print(f"  Staging root: {STAGING_ROOT}")
    print(f"  Packets discovered: {discovered}")
    print(f"  Packets with candidates: {candidates}")
    print(f"  Records staged: {staged}")
    print(f"  Records skipped (unchanged): {skipped_unchanged}")
    print(f"  Records failed: {failed}")
    if dry_run:
        print("  Dry run: yes (no files written)")
    else:
        print(f"  Source files written: {source_files_written}")
        print(f"  Global manifest: {MANIFEST_FILE}")
        print(f"  State file: {STATE_FILE}")
        print(f"  Run artifacts: {staging_dir}")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pull ordinance/resolution metadata records from agenda packet outputs."
    )
    parser.add_argument("--limit", type=int, default=None, help="Process only first N packets with candidates.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-stage packets even if source hash is unchanged.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze only; do not write source/staging files.",
    )
    args = parser.parse_args()

    run_pull(limit=args.limit, force=args.force, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

