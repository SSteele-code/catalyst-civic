#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[6]
LEGACY_ROOT = REPO_ROOT / "_Sources" / "Mode 1-Meetings"
TARGET_ROOT = REPO_ROOT / "_Sources" / "M1-Meetings" / "Agendas"
MANIFEST_PATH = TARGET_ROOT / "M1_AGENDAS_MANIFEST.jsonl"

MODE = "M1"
SUBGROUP = "AG"

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

PDF_EXTENSIONS = {".pdf"}


@dataclass
class AgendaRecord:
    source_path: Path
    created_date: date
    pulled_date: date
    year_hint: int | None


def find_year_hint(path: Path) -> int | None:
    for part in path.parts:
        if re.fullmatch(r"20\d{2}", part):
            return int(part)
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


def safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_created_date(path: Path) -> date:
    stem = path.stem
    year_hint = find_year_hint(path)
    lower = stem.lower()

    # Month-name forms: "March 10, 2026", "Dec 9 2025"
    month_names = "|".join(sorted(MONTH_MAP.keys(), key=len, reverse=True))
    for match in re.finditer(rf"\b({month_names})\b[\s._,-]*(\d{{1,2}})(?:st|nd|rd|th)?(?:[\s._,-]+(\d{{2,4}}))?", lower):
        month_token, day_token, year_token = match.group(1), match.group(2), match.group(3)
        month = MONTH_MAP[month_token]
        day = int(day_token)
        if year_token:
            year = normalize_year(year_token, year_hint)
        elif year_hint:
            year = year_hint
        else:
            continue
        parsed = safe_date(year, month, day)
        if parsed:
            return parsed

    # Numeric forms: mm-dd-yyyy, mm.dd.yyyy, mm dd yyyy
    for match in re.finditer(r"(?<!\d)(\d{1,2})[.\-_/ ]+(\d{1,2})[.\-_/ ]+(\d{2,4})(?!\d)", lower):
        month = int(match.group(1))
        day = int(match.group(2))
        year = normalize_year(match.group(3), year_hint)
        parsed = safe_date(year, month, day)
        if parsed:
            return parsed

    # Four-number noisy forms: 07-09-13-2013 -> month/day/year=07/09/2013
    for match in re.finditer(r"(?<!\d)(\d{1,2})[.\-_/ ]+(\d{1,2})[.\-_/ ]+(\d{1,2})[.\-_/ ]+(\d{4})(?!\d)", lower):
        month = int(match.group(1))
        day = int(match.group(2))
        year = int(match.group(4))
        parsed = safe_date(year, month, day)
        if parsed:
            return parsed

    if year_hint is not None:
        return date(year_hint, 1, 1)

    # Last-resort fallback
    ts = path.stat().st_mtime
    return datetime.fromtimestamp(ts).date()


def discover_legacy_agendas(legacy_root: Path) -> list[AgendaRecord]:
    records: list[AgendaRecord] = []
    agenda_dirs = [p for p in legacy_root.rglob("*") if p.is_dir() and p.name.lower() == "agenda_council packet"]
    for agenda_dir in agenda_dirs:
        for path in sorted(agenda_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in PDF_EXTENSIONS:
                continue
            created = parse_created_date(path)
            pulled = datetime.fromtimestamp(path.stat().st_mtime).date()
            records.append(
                AgendaRecord(
                    source_path=path,
                    created_date=created,
                    pulled_date=pulled,
                    year_hint=find_year_hint(path),
                )
            )
    records.sort(key=lambda r: (r.created_date, r.pulled_date, str(r.source_path).lower()))
    return records


def load_existing_manifest(path: Path) -> tuple[int, set[str]]:
    max_doc_number = 0
    source_paths: set[str] = set()
    if not path.exists():
        return max_doc_number, source_paths
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        doc = str(payload.get("document_number") or "")
        if doc.isdigit():
            max_doc_number = max(max_doc_number, int(doc))
        source = str(payload.get("legacy_source_path") or "")
        if source:
            source_paths.add(source)
    return max_doc_number, source_paths


def remove_empty_legacy_agenda_dirs(legacy_root: Path) -> int:
    removed = 0
    agenda_dirs = sorted(
        (p for p in legacy_root.rglob("*") if p.is_dir() and p.name.lower() == "agenda_council packet"),
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for agenda_dir in agenda_dirs:
        if any(agenda_dir.iterdir()):
            continue
        agenda_dir.rmdir()
        removed += 1
    # Clean now-empty parent year folders under former agenda locations
    for path in sorted((p for p in legacy_root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if path == legacy_root:
            continue
        if any(path.iterdir()):
            continue
        try:
            path.rmdir()
            removed += 1
        except OSError:
            continue
    return removed


def migrate(legacy_root: Path, target_root: Path, prune_legacy: bool = True, dry_run: bool = False) -> dict:
    if not legacy_root.exists():
        raise FileNotFoundError(f"Legacy root not found: {legacy_root}")

    target_root.mkdir(parents=True, exist_ok=True)
    max_doc_number, seen_sources = load_existing_manifest(MANIFEST_PATH)
    records = discover_legacy_agendas(legacy_root)

    moved = 0
    skipped_existing_manifest = 0
    manifest_rows: list[str] = []

    for record in records:
        source_abs = str(record.source_path.resolve())
        if source_abs in seen_sources:
            skipped_existing_manifest += 1
            continue

        max_doc_number += 1
        document_number = f"{max_doc_number:06d}"
        created_tag = record.created_date.strftime("%Y%m%d")
        pulled_tag = record.pulled_date.strftime("%Y%m%d")
        machine_code = f"{MODE}.{SUBGROUP}.{document_number}.{created_tag}.{pulled_tag}"
        target_path = target_root / f"{machine_code}.pdf"

        while target_path.exists():
            max_doc_number += 1
            document_number = f"{max_doc_number:06d}"
            machine_code = f"{MODE}.{SUBGROUP}.{document_number}.{created_tag}.{pulled_tag}"
            target_path = target_root / f"{machine_code}.pdf"

        row = {
            "machine_code": machine_code,
            "mode": MODE,
            "subgroup": SUBGROUP,
            "document_number": document_number,
            "created_date": created_tag,
            "pulled_date": pulled_tag,
            "file_name": target_path.name,
            "legacy_source_path": source_abs,
            "legacy_relative_path": str(record.source_path.relative_to(legacy_root)),
            "legacy_year_hint": record.year_hint,
            "migrated_at": datetime.now().isoformat(timespec="seconds"),
        }

        if not dry_run:
            shutil.move(str(record.source_path), str(target_path))
        manifest_rows.append(json.dumps(row, ensure_ascii=True))
        moved += 1

    if manifest_rows and not dry_run:
        with open(MANIFEST_PATH, "a", encoding="utf-8") as f:
            for line in manifest_rows:
                f.write(line + "\n")

    removed_dirs = 0
    if prune_legacy and not dry_run:
        removed_dirs = remove_empty_legacy_agenda_dirs(legacy_root)

    return {
        "discovered": len(records),
        "moved": moved,
        "skipped_existing_manifest": skipped_existing_manifest,
        "removed_empty_dirs": removed_dirs,
        "target_root": str(target_root),
        "manifest": str(MANIFEST_PATH),
        "dry_run": dry_run,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rehome legacy Mode 1 agenda PDFs to _Sources/M1-Meetings/Agendas.")
    parser.add_argument("--legacy-root", type=Path, default=LEGACY_ROOT)
    parser.add_argument("--target-root", type=Path, default=TARGET_ROOT)
    parser.add_argument("--no-prune-legacy", action="store_true", help="Do not remove empty legacy agenda folders.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = migrate(
        legacy_root=args.legacy_root.resolve(),
        target_root=args.target_root.resolve(),
        prune_legacy=not args.no_prune_legacy,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
