#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[6]
LEGACY_ROOT = REPO_ROOT / "_Sources" / "Mode 1-Meetings"
TARGET_ROOT = REPO_ROOT / "_Sources" / "M1-Meetings" / "Transcripts"
MANIFEST_PATH = TARGET_ROOT / "M1_TS_MANIFEST.jsonl"

MODE = "M1"
SUBGROUP = "TS"
VALID_EXTENSIONS = {".txt", ".vtt"}

YOUTUBE_ID_RE = re.compile(r"([A-Za-z0-9_-]{11})(?:\.en)?$", re.IGNORECASE)
MACHINE_FILE_RE = re.compile(
    r"^M1-TS-(\d{6})-(\d{8})-(\d{8})-([A-Za-z0-9_-]{3,32})(?:-R(\d{2}))?\.(txt|vtt)$",
    re.IGNORECASE,
)

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
class TranscriptFile:
    source_path: Path
    extension: str
    youtube_id: str | None
    created_date: date
    pulled_date: date


def safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
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


def find_year_hint(path: Path) -> int | None:
    for part in path.parts:
        if re.fullmatch(r"20\d{2}", part):
            return int(part)
    return None


def extract_youtube_id(path: Path) -> str | None:
    match = YOUTUBE_ID_RE.search(path.stem)
    if not match:
        return None
    return match.group(1)


def parse_created_date(path: Path) -> date:
    stem = path.stem
    year_hint = find_year_hint(path)
    lower = stem.lower()

    if year_hint:
        parsed = safe_date(year_hint, 1, 1)
        if parsed is not None:
            fallback_by_year = parsed
        else:
            fallback_by_year = datetime.fromtimestamp(path.stat().st_mtime).date()
    else:
        fallback_by_year = datetime.fromtimestamp(path.stat().st_mtime).date()

    month_names = "|".join(sorted(MONTH_MAP.keys(), key=len, reverse=True))

    # "March 10, 2026" and "Dec 9 2025"
    for match in re.finditer(
        rf"\b({month_names})\b[\s._,-]*(\d{{1,2}})(?:st|nd|rd|th)?(?:[\s._,-]+(\d{{2,4}}))?",
        lower,
    ):
        month_token, day_token, year_token = match.group(1), match.group(2), match.group(3)
        month = MONTH_MAP[month_token]
        day = int(day_token)
        year = normalize_year(year_token, year_hint) if year_token else (year_hint or fallback_by_year.year)
        parsed = safe_date(year, month, day)
        if parsed:
            return parsed

    # YYYY-MM-DD
    for match in re.finditer(r"(?<!\d)(20\d{2})[.\-_/ ]+(\d{1,2})[.\-_/ ]+(\d{1,2})(?!\d)", lower):
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3))
        parsed = safe_date(year, month, day)
        if parsed:
            return parsed

    # MM-DD-YYYY
    for match in re.finditer(r"(?<!\d)(\d{1,2})[.\-_/ ]+(\d{1,2})[.\-_/ ]+(\d{2,4})(?!\d)", lower):
        month = int(match.group(1))
        day = int(match.group(2))
        year = normalize_year(match.group(3), year_hint)
        parsed = safe_date(year, month, day)
        if parsed:
            return parsed

    return fallback_by_year


def discover_legacy_transcript_files(legacy_root: Path) -> list[TranscriptFile]:
    records: list[TranscriptFile] = []
    transcript_dirs = [p for p in legacy_root.rglob("*") if p.is_dir() and p.name.lower() == "transcripts"]
    for transcript_dir in transcript_dirs:
        for path in sorted(transcript_dir.rglob("*")):
            if not path.is_file():
                continue
            ext = path.suffix.lower()
            if ext not in VALID_EXTENSIONS:
                continue
            created = parse_created_date(path)
            pulled = datetime.fromtimestamp(path.stat().st_mtime).date()
            records.append(
                TranscriptFile(
                    source_path=path,
                    extension=ext,
                    youtube_id=extract_youtube_id(path),
                    created_date=created,
                    pulled_date=pulled,
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
        raw_doc = str(payload.get("document_number") or "")
        if raw_doc.isdigit():
            max_doc_number = max(max_doc_number, int(raw_doc))
        source = str(payload.get("legacy_source_path") or "")
        if source:
            source_paths.add(source)
    return max_doc_number, source_paths


def load_existing_doc_from_target(target_root: Path) -> int:
    max_doc_number = 0
    if not target_root.exists():
        return max_doc_number
    for file_path in target_root.glob("M1-TS-*.txt"):
        match = MACHINE_FILE_RE.match(file_path.name)
        if match:
            max_doc_number = max(max_doc_number, int(match.group(1)))
    for file_path in target_root.glob("M1-TS-*.vtt"):
        match = MACHINE_FILE_RE.match(file_path.name)
        if match:
            max_doc_number = max(max_doc_number, int(match.group(1)))
    return max_doc_number


def remove_empty_transcript_dirs(legacy_root: Path) -> int:
    removed = 0
    candidates = sorted(
        (p for p in legacy_root.rglob("*") if p.is_dir() and p.name.lower() == "transcripts"),
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for transcript_dir in candidates:
        for inner in sorted((d for d in transcript_dir.rglob("*") if d.is_dir()), key=lambda p: len(p.parts), reverse=True):
            if any(inner.iterdir()):
                continue
            try:
                inner.rmdir()
                removed += 1
            except OSError:
                continue
        if any(transcript_dir.iterdir()):
            continue
        try:
            transcript_dir.rmdir()
            removed += 1
        except OSError:
            continue
    return removed


def migrate(legacy_root: Path, target_root: Path, prune_legacy: bool = True, dry_run: bool = False) -> dict:
    if not legacy_root.exists():
        raise FileNotFoundError(f"Legacy root not found: {legacy_root}")

    target_root.mkdir(parents=True, exist_ok=True)
    max_doc_manifest, seen_sources = load_existing_manifest(MANIFEST_PATH)
    max_doc_target = load_existing_doc_from_target(target_root)
    next_doc_number = max(max_doc_manifest, max_doc_target)

    discovered = discover_legacy_transcript_files(legacy_root)
    groups: dict[str, list[TranscriptFile]] = defaultdict(list)
    for record in discovered:
        if record.youtube_id:
            key = f"YT::{record.youtube_id}"
        else:
            key = f"PATH::{record.source_path.as_posix()}"
        groups[key].append(record)

    # stable group ordering
    sorted_group_keys = sorted(
        groups.keys(),
        key=lambda k: (
            min(item.created_date for item in groups[k]),
            min(item.pulled_date for item in groups[k]),
            k,
        ),
    )

    moved = 0
    skipped_existing_manifest = 0
    manifest_lines: list[str] = []

    for group_key in sorted_group_keys:
        files = sorted(groups[group_key], key=lambda x: (x.extension, str(x.source_path).lower()))
        remaining = [f for f in files if str(f.source_path.resolve()) not in seen_sources]
        if not remaining:
            skipped_existing_manifest += len(files)
            continue

        next_doc_number += 1
        document_number = f"{next_doc_number:06d}"

        created_date = min(item.created_date for item in files).strftime("%Y%m%d")
        pulled_date = max(item.pulled_date for item in files).strftime("%Y%m%d")
        youtube_id = next((item.youtube_id for item in files if item.youtube_id), None)
        stream_key = youtube_id if youtube_id else f"UNK{document_number}"
        base_name = f"{MODE}-{SUBGROUP}-{document_number}-{created_date}-{pulled_date}-{stream_key}"

        ext_revision_counts: dict[str, int] = defaultdict(int)
        for item in files:
            source_abs = str(item.source_path.resolve())
            if source_abs in seen_sources:
                skipped_existing_manifest += 1
                continue

            ext = item.extension.lower()
            ext_revision_counts[ext] += 1
            revision = ext_revision_counts[ext]
            revision_suffix = "" if revision == 1 else f"-R{revision:02d}"
            target_name = f"{base_name}{revision_suffix}{ext}"
            target_path = target_root / target_name

            while target_path.exists():
                revision += 1
                ext_revision_counts[ext] = revision
                revision_suffix = f"-R{revision:02d}"
                target_name = f"{base_name}{revision_suffix}{ext}"
                target_path = target_root / target_name

            if not dry_run:
                shutil.move(str(item.source_path), str(target_path))

            row = {
                "machine_code": f"{MODE}-{SUBGROUP}-{document_number}-{created_date}-{pulled_date}-{stream_key}",
                "mode": MODE,
                "subgroup": SUBGROUP,
                "document_number": document_number,
                "created_date": created_date,
                "pulled_date": pulled_date,
                "youtube_id": youtube_id,
                "stream_key": stream_key,
                "asset_type": ext.lstrip("."),
                "revision": revision,
                "file_name": target_name,
                "legacy_source_path": source_abs,
                "legacy_relative_path": str(item.source_path.relative_to(legacy_root)),
                "migrated_at": datetime.now().isoformat(timespec="seconds"),
            }
            manifest_lines.append(json.dumps(row, ensure_ascii=True))
            moved += 1

    if manifest_lines and not dry_run:
        with open(MANIFEST_PATH, "a", encoding="utf-8") as f:
            for line in manifest_lines:
                f.write(line + "\n")

    removed_dirs = 0
    if prune_legacy and not dry_run:
        removed_dirs = remove_empty_transcript_dirs(legacy_root)

    return {
        "discovered_files": len(discovered),
        "groups": len(groups),
        "moved_files": moved,
        "skipped_existing_manifest": skipped_existing_manifest,
        "removed_empty_dirs": removed_dirs,
        "target_root": str(target_root),
        "manifest": str(MANIFEST_PATH),
        "dry_run": dry_run,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rehome legacy Mode 1 transcripts to _Sources/M1-Meetings/Transcripts.")
    parser.add_argument("--legacy-root", type=Path, default=LEGACY_ROOT)
    parser.add_argument("--target-root", type=Path, default=TARGET_ROOT)
    parser.add_argument("--no-prune-legacy", action="store_true", help="Do not remove empty legacy transcript folders.")
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
