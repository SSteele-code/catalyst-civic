import os
#!/usr/bin/env python
"""
Minutes PRE_PARSE (Agenda-Staging Lane)

Transforms staged agenda-mined minutes excerpts into a normalized Minutes schema
and writes pusher-ready artifacts into:
  _Sources/M1-Meetings/Minutes/_output/<minutes_code>/

Strict invariant:
  - PRE_PARSE only (schema normalization + lineage packaging)
  - No DB writes
  - No glossary writes

Linkage contract:
  - source_pdf_code: M1.AG.<docnum>.<created_yyyymmdd>.<pulled_yyyymmdd>
  - minutes_code:    M1.AG.MN.<docnum>.<created_yyyymmdd>.<pulled_yyyymmdd>
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


MINUTES_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Minutes"
STAGING_ROOT = MINUTES_ROOT / "_staging"
OUTPUT_ROOT = MINUTES_ROOT / "_output"
RUNS_ROOT = OUTPUT_ROOT / "_runs"

STATE_FILE = MINUTES_ROOT / "minutes_preparse_state.json"
MANIFEST_FILE = MINUTES_ROOT / "M1_MINUTES_PREPARSE_MANIFEST.jsonl"

SCHEMA_VERSION = "m1.minutes.preparse.v1"
SOURCE_LANE = "agenda_output_minutes_excerpt"
JURISDICTION = "Richlands"

AG_CODE_RE = re.compile(r"^M1\.AG\.(\d{6})\.(\d{8})\.(\d{8})$", re.IGNORECASE)
MINUTES_APPROVAL_RE = re.compile(r"\b(approve|approval|approved|adopt)\b.*\bminutes?\b", re.IGNORECASE)
MINUTES_TOKEN_RE = re.compile(r"\bminutes?\b", re.IGNORECASE)

MONTH_MAP = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


@dataclass
class StageCandidate:
    stage_json_path: Path
    stage_json_sha256: str
    source_stage_run_id: str
    source_stage_captured_at: str
    source_stage_machine_code: str
    source_txt_path: Path
    source_txt_sha256: str
    source_pdf_code: str
    minutes_code: str
    factsheet_path: Path
    source_pdf_original_name: str
    source_pdf_internal_name: str
    source_pdf_hash: str
    page_count: int | None
    excerpts: list[dict]


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
        return {"records": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"records": {}}


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


def load_manifest_codes() -> set[str]:
    codes: set[str] = set()
    if not MANIFEST_FILE.exists():
        return codes
    for line in MANIFEST_FILE.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            continue
        code = str(row.get("minutes_code") or "").strip()
        if code:
            codes.add(code)
    return codes


def to_int(value: object, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def iter_stage_json_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("*.minutes.json"))


def build_minutes_code(source_pdf_code: str) -> str | None:
    match = AG_CODE_RE.match(source_pdf_code.strip())
    if not match:
        return None
    docnum, created_ymd, pulled_ymd = match.group(1), match.group(2), match.group(3)
    return f"M1.AG.MN.{docnum}.{created_ymd}.{pulled_ymd}"


def ymd_to_iso(ymd: str) -> str | None:
    if not re.fullmatch(r"\d{8}", ymd):
        return None
    try:
        return datetime.strptime(ymd, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def normalize_year(raw_year: str, year_hint: int | None) -> int:
    value = int(raw_year)
    if value < 100:
        if year_hint:
            century = year_hint // 100
            candidate = century * 100 + value
            if abs(candidate - year_hint) <= 5:
                return candidate
        return 2000 + value if value <= 69 else 1900 + value
    return value


def extract_date_mentions(text: str, year_hint: int | None = None) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    lower = text.lower()

    month_names = "|".join(sorted(MONTH_MAP.keys(), key=len, reverse=True))
    for match in re.finditer(
        rf"\b({month_names})\b[\s._,-]*(\d{{1,2}})(?:st|nd|rd|th)?(?:[\s._,-]+(\d{{2,4}}))?",
        lower,
    ):
        month_token, day_token, year_token = match.group(1), match.group(2), match.group(3)
        raw = text[match.start() : match.end()]
        month = MONTH_MAP[month_token]
        day = int(day_token)
        year = normalize_year(year_token, year_hint) if year_token else year_hint
        if year is None:
            continue
        iso_date = ymd_to_iso(f"{year:04d}{month:02d}{day:02d}")
        if not iso_date:
            continue
        key = f"{iso_date}|{raw.strip().lower()}"
        if key in seen:
            continue
        seen.add(key)
        found.append({"iso_date": iso_date, "raw": raw.strip()})

    for match in re.finditer(r"(?<!\d)(\d{1,2})[.\-_/ ]+(\d{1,2})[.\-_/ ]+(\d{2,4})(?!\d)", lower):
        raw = text[match.start() : match.end()]
        month = int(match.group(1))
        day = int(match.group(2))
        year = normalize_year(match.group(3), year_hint)
        iso_date = ymd_to_iso(f"{year:04d}{month:02d}{day:02d}")
        if not iso_date:
            continue
        key = f"{iso_date}|{raw.strip().lower()}"
        if key in seen:
            continue
        seen.add(key)
        found.append({"iso_date": iso_date, "raw": raw.strip()})

    found.sort(key=lambda d: (d["iso_date"], d["raw"]))
    return found


def read_factsheet(source_txt_path: Path, source_stage_machine_code: str) -> tuple[Path | None, dict | None]:
    output_dir = source_txt_path.parent
    candidate = output_dir / f"{source_stage_machine_code}.factsheet.json"
    if candidate.exists():
        try:
            return candidate, json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            return candidate, None
    for fs in sorted(output_dir.glob("*.factsheet.json")):
        try:
            return fs, json.loads(fs.read_text(encoding="utf-8"))
        except Exception:
            continue
    return None, None


def parse_stage_candidate(stage_json_path: Path) -> tuple[StageCandidate | None, str | None]:
    try:
        stage_text = stage_json_path.read_text(encoding="utf-8")
        payload = json.loads(stage_text)
    except Exception as exc:
        return None, f"invalid_stage_json: {exc}"

    source_stage_machine_code = str(payload.get("machine_code") or "").strip()
    source_txt_raw = str(payload.get("source_txt") or "").strip()
    if not source_stage_machine_code:
        return None, "missing_machine_code"
    if not source_txt_raw:
        return None, "missing_source_txt"

    source_txt_path = Path(source_txt_raw)
    if not source_txt_path.exists():
        return None, "missing_source_txt_file"

    factsheet_path, facts = read_factsheet(source_txt_path, source_stage_machine_code)
    if not facts:
        return None, "missing_or_invalid_factsheet"

    source_pdf_original_name = str(facts.get("source_pdf_original_name") or "").strip()
    if not source_pdf_original_name.lower().endswith(".pdf"):
        return None, "missing_source_pdf_original_name"
    source_pdf_code = source_pdf_original_name[:-4]

    minutes_code = build_minutes_code(source_pdf_code)
    if not minutes_code:
        return None, f"unmappable_source_pdf_code: {source_pdf_code}"

    excerpts = payload.get("excerpts")
    if not isinstance(excerpts, list):
        return None, "missing_excerpts"

    source_txt_sha256 = sha256_file(source_txt_path)

    candidate = StageCandidate(
        stage_json_path=stage_json_path,
        stage_json_sha256=sha256_text(stage_text),
        source_stage_run_id=str(payload.get("run_id") or "").strip(),
        source_stage_captured_at=str(payload.get("captured_at") or "").strip(),
        source_stage_machine_code=source_stage_machine_code,
        source_txt_path=source_txt_path,
        source_txt_sha256=source_txt_sha256,
        source_pdf_code=source_pdf_code,
        minutes_code=minutes_code,
        factsheet_path=factsheet_path if factsheet_path else Path(""),
        source_pdf_original_name=source_pdf_original_name,
        source_pdf_internal_name=str(facts.get("source_pdf_internal_name") or "").strip(),
        source_pdf_hash=str(facts.get("source_pdf_hash") or "").strip(),
        page_count=facts.get("page_count") if isinstance(facts.get("page_count"), int) else None,
        excerpts=excerpts,
    )
    return candidate, None


def choose_best_candidate(candidates: Sequence[StageCandidate]) -> StageCandidate:
    def rank_key(c: StageCandidate) -> tuple[str, str]:
        return (c.source_stage_captured_at, str(c.stage_json_path))

    return sorted(candidates, key=rank_key, reverse=True)[0]


def render_summary_text(payload: dict) -> str:
    header = [
        f"MINUTES_CODE: {payload['minutes_code']}",
        f"SOURCE_PDF_CODE: {payload['linked_source_pdf_code']}",
        f"SOURCE_LANE: {payload['source_lane']}",
        f"EXCERPT_COUNT: {payload['minutes_excerpt_summary']['excerpt_count']}",
        "",
    ]
    body: list[str] = []
    for ex in payload.get("minutes_excerpts", []):
        body.append(
            f"[{ex['excerpt_id']}] {ex['kind']} lines {ex['start_line']}-{ex['end_line']} "
            f"(approval={ex['signals']['contains_approval_terms']})"
        )
        body.append(ex["text"])
        body.append("")
    return "\n".join(header + body).strip() + "\n"


def build_payload(candidate: StageCandidate, run_id: str) -> dict:
    source_match = AG_CODE_RE.match(candidate.source_pdf_code)
    assert source_match is not None
    created_ymd = source_match.group(2)
    anchor_meeting_date = ymd_to_iso(created_ymd)
    anchor_year = int(created_ymd[:4]) if created_ymd else None

    excerpt_rows: list[dict] = []
    kind_counts: dict[str, int] = {}
    global_date_mentions: list[dict] = []
    global_date_keys: set[str] = set()
    has_approval_language = False

    for idx, raw in enumerate(candidate.excerpts, start=1):
        kind = str(raw.get("kind") or "unknown").strip()
        start_line = to_int(raw.get("start_line"), default=0)
        end_line = to_int(raw.get("end_line"), default=0)
        text = str(raw.get("text") or "").strip()

        kind_counts[kind] = kind_counts.get(kind, 0) + 1

        mentions = extract_date_mentions(text, year_hint=anchor_year)
        for dm in mentions:
            key = f"{dm['iso_date']}|{dm['raw'].lower()}"
            if key not in global_date_keys:
                global_date_keys.add(key)
                global_date_mentions.append(dm)

        approval_here = bool(MINUTES_APPROVAL_RE.search(text)) or (
            bool(MINUTES_TOKEN_RE.search(text)) and "approval of minutes" in text.lower()
        )
        if approval_here:
            has_approval_language = True

        excerpt_rows.append(
            {
                "excerpt_id": f"EX{idx:03d}",
                "kind": kind,
                "start_line": start_line,
                "end_line": end_line,
                "text": text,
                "text_sha256": sha256_text(text),
                "signals": {
                    "contains_approval_terms": approval_here,
                    "date_mentions": mentions,
                },
            }
        )

    global_date_mentions.sort(key=lambda d: (d["iso_date"], d["raw"]))

    payload = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "minutes_preparse_record",
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "preparse_run_id": run_id,
        "source_lane": SOURCE_LANE,
        "jurisdiction": JURISDICTION,
        "minutes_code": candidate.minutes_code,
        "artifact_machine_code": candidate.minutes_code,
        "linked_source_pdf_code": candidate.source_pdf_code,
        "meeting_context": {
            "anchor_meeting_date": anchor_meeting_date,
            "anchor_meeting_type": None,
        },
        "lineage": {
            "source_stage_run_id": candidate.source_stage_run_id,
            "source_stage_captured_at": candidate.source_stage_captured_at,
            "source_stage_machine_code": candidate.source_stage_machine_code,
            "source_stage_json_path": str(candidate.stage_json_path),
            "source_stage_json_sha256": candidate.stage_json_sha256,
            "source_txt_path": str(candidate.source_txt_path),
            "source_txt_sha256": candidate.source_txt_sha256,
            "agenda_output_dir": str(candidate.source_txt_path.parent),
            "factsheet_path": str(candidate.factsheet_path),
            "source_pdf_original_name": candidate.source_pdf_original_name,
            "source_pdf_internal_name": candidate.source_pdf_internal_name,
            "source_pdf_hash": candidate.source_pdf_hash,
            "source_pdf_page_count": candidate.page_count,
        },
        "minutes_excerpt_summary": {
            "excerpt_count": len(excerpt_rows),
            "excerpt_kind_counts": kind_counts,
            "contains_approval_language": has_approval_language,
            "date_mentions": global_date_mentions,
        },
        "minutes_excerpts": excerpt_rows,
        "pusher_ready": {
            "meeting_id": candidate.minutes_code,
            "source_id": candidate.source_pdf_code,
            "content_mode": "excerpt_pack",
            "is_complete_minutes_document": False,
            "glossary_scope_text_hint": "minutes_excerpts[].text",
        },
    }
    return payload


def run_preparse(limit: int | None = None, force: bool = False, dry_run: bool = False) -> None:
    run_id = datetime.now().strftime("RUN_%Y%m%dT%H%M%S")
    started_at = datetime.now().isoformat(timespec="seconds")

    state = load_state()
    state_records = state.setdefault("records", {})

    discovered = 0
    mapped = 0
    prepared = 0
    skipped_unchanged = 0
    failed = 0

    failure_rows: list[dict] = []
    prepared_rows: list[dict] = []
    manifest_codes = load_manifest_codes()

    groups: dict[str, list[StageCandidate]] = {}

    for stage_json in iter_stage_json_files(STAGING_ROOT):
        discovered += 1
        candidate, error = parse_stage_candidate(stage_json)
        if error:
            failed += 1
            failure_rows.append(
                {
                    "run_id": run_id,
                    "failed_at": datetime.now().isoformat(timespec="seconds"),
                    "source_stage_json_path": str(stage_json),
                    "error": error,
                }
            )
            continue
        groups.setdefault(candidate.minutes_code, []).append(candidate)

    mapped = len(groups)
    chosen: list[StageCandidate] = [choose_best_candidate(v) for v in groups.values()]
    chosen.sort(key=lambda c: c.minutes_code)

    if limit is not None:
        chosen = chosen[:limit]

    if not dry_run:
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        run_dir = RUNS_ROOT / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        run_dir = RUNS_ROOT / run_id

    for candidate in chosen:
        prev = state_records.get(candidate.minutes_code, {})
        output_json = OUTPUT_ROOT / candidate.minutes_code / f"{candidate.minutes_code}.preparse.json"
        output_txt = OUTPUT_ROOT / candidate.minutes_code / f"{candidate.minutes_code}.preparse.txt"

        if (
            not force
            and prev.get("source_stage_json_sha256") == candidate.stage_json_sha256
            and prev.get("source_txt_sha256") == candidate.source_txt_sha256
            and output_json.exists()
        ):
            skipped_unchanged += 1
            continue

        try:
            payload = build_payload(candidate, run_id=run_id)
            payload_text = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
            payload_sha256 = sha256_text(payload_text)

            if not dry_run:
                out_dir = OUTPUT_ROOT / candidate.minutes_code
                out_dir.mkdir(parents=True, exist_ok=True)
                output_json.write_text(payload_text, encoding="utf-8")
                output_txt.write_text(render_summary_text(payload), encoding="utf-8")

            prepared += 1
            row = {
                "run_id": run_id,
                "prepared_at": datetime.now().isoformat(timespec="seconds"),
                "schema_version": SCHEMA_VERSION,
                "minutes_code": candidate.minutes_code,
                "linked_source_pdf_code": candidate.source_pdf_code,
                "source_stage_json_path": str(candidate.stage_json_path),
                "source_stage_json_sha256": candidate.stage_json_sha256,
                "source_txt_path": str(candidate.source_txt_path),
                "source_txt_sha256": candidate.source_txt_sha256,
                "payload_sha256": payload_sha256,
                "output_json": str(output_json),
                "output_txt": str(output_txt),
                "excerpt_count": len(candidate.excerpts),
            }
            prepared_rows.append(row)

            state_records[candidate.minutes_code] = {
                "last_run_id": run_id,
                "last_status": "prepared",
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "source_stage_json_path": str(candidate.stage_json_path),
                "source_stage_json_sha256": candidate.stage_json_sha256,
                "source_txt_path": str(candidate.source_txt_path),
                "source_txt_sha256": candidate.source_txt_sha256,
                "linked_source_pdf_code": candidate.source_pdf_code,
                "output_json": str(output_json),
                "output_txt": str(output_txt),
                "payload_sha256": payload_sha256,
            }

            if not dry_run:
                if candidate.minutes_code not in manifest_codes:
                    append_manifest_rows([row])
                    manifest_codes.add(candidate.minutes_code)
                save_state(state)
        except Exception as exc:
            failed += 1
            failure_rows.append(
                {
                    "run_id": run_id,
                    "failed_at": datetime.now().isoformat(timespec="seconds"),
                    "minutes_code": candidate.minutes_code,
                    "source_stage_json_path": str(candidate.stage_json_path),
                    "error": str(exc),
                }
            )
            continue

    if not dry_run:
        run_manifest = run_dir / "minutes_preparse_manifest.jsonl"
        with run_manifest.open("w", encoding="utf-8") as f:
            for row in prepared_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

        if failure_rows:
            run_failures = run_dir / "minutes_preparse_failures.jsonl"
            with run_failures.open("w", encoding="utf-8") as f:
                for row in failure_rows:
                    f.write(json.dumps(row, ensure_ascii=True) + "\n")

        run_summary = {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "source_lane": SOURCE_LANE,
            "schema_version": SCHEMA_VERSION,
            "staging_root": str(STAGING_ROOT),
            "output_root": str(OUTPUT_ROOT),
            "discovered_stage_files": discovered,
            "mapped_minutes_codes": mapped,
            "prepared_records": prepared,
            "skipped_unchanged": skipped_unchanged,
            "failed": failed,
            "limit": limit,
            "force": force,
        }
        (run_dir / "run_summary.json").write_text(json.dumps(run_summary, ensure_ascii=True, indent=2) + "\n")

        save_state(state)

    print("=" * 60)
    print("MINUTES PRE_PARSE SUMMARY")
    print(f"  Run ID: {run_id}")
    print(f"  Source lane: {SOURCE_LANE}")
    print(f"  Staging root: {STAGING_ROOT}")
    print(f"  Output root: {OUTPUT_ROOT}")
    print(f"  Stage files discovered: {discovered}")
    print(f"  Mapped minutes codes: {mapped}")
    print(f"  Prepared records: {prepared}")
    print(f"  Skipped (unchanged): {skipped_unchanged}")
    print(f"  Failed: {failed}")
    if dry_run:
        print("  Dry run: yes (no files written)")
    else:
        print(f"  Global manifest: {MANIFEST_FILE}")
        print(f"  State file: {STATE_FILE}")
        print(f"  Run artifacts: {run_dir}")
    print("=" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize agenda-mined minutes staging artifacts into pusher-ready Minutes schema."
    )
    parser.add_argument("--limit", type=int, default=None, help="Process only first N minutes codes.")
    parser.add_argument("--force", action="store_true", help="Rebuild outputs even when unchanged by state.")
    parser.add_argument("--dry-run", action="store_true", help="Scan/map only; do not write outputs.")
    args = parser.parse_args()

    run_preparse(limit=args.limit, force=args.force, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
