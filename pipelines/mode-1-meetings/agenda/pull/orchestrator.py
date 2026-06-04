import os
#!/usr/bin/env python
"""
Richlands Agenda Pull Orchestrator

Pipeline:
  1) Discover agenda links from yearly Richlands pages
  2) Fetch PDF
  3) Store into flat source root:
     _Sources/M1-Meetings/Agendas
  4) Rename using machine code:
     M1.AG.<document_number>.<created_yyyymmdd>.<pulled_yyyymmdd>.pdf
  5) Append manifest + update state
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from fetch_agenda import fetch_pdf
from parse_links import fetch_year_links


TARGET_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Agendas"
STATE_FILE = TARGET_ROOT / "agenda_state.json"
MANIFEST_FILE = TARGET_ROOT / "M1_AGENDAS_MANIFEST.jsonl"

MODE = "M1"
SUBGROUP = "AG"
AGENDA_YEARS = list(range(2013, datetime.now().year + 1))  # inclusive scan window

MACHINE_FILE_RE = re.compile(r"^M1\.AG\.(\d{6})\.(\d{8})\.(\d{8})\.pdf$", re.IGNORECASE)

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


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"processed_ids": {}}


def save_state(state: dict) -> None:
    TARGET_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def make_agenda_id(url: str) -> str:
    path = unquote(urlsplit(url).path)
    marker = "/agenda/"
    idx = path.find(marker)
    return path[idx + len(marker) :] if idx >= 0 else path


def load_manifest_inventory() -> tuple[int, set[str]]:
    max_doc_number = 0
    seen_urls: set[str] = set()

    if MANIFEST_FILE.exists():
        for line in MANIFEST_FILE.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                continue
            raw_doc = str(row.get("document_number") or "")
            if raw_doc.isdigit():
                max_doc_number = max(max_doc_number, int(raw_doc))
            raw_url = str(row.get("url") or "").strip()
            if raw_url:
                seen_urls.add(raw_url)

    if TARGET_ROOT.exists():
        for file_path in TARGET_ROOT.glob("*.pdf"):
            match = MACHINE_FILE_RE.match(file_path.name)
            if match:
                max_doc_number = max(max_doc_number, int(match.group(1)))

    return max_doc_number, seen_urls


def append_manifest_row(row: dict) -> None:
    TARGET_ROOT.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=True) + "\n")


def discover_agendas(years: list[int]) -> list[dict]:
    all_links: list[dict] = []
    print(">>> Stage 01: Discovering agenda PDFs (Reverse Chronological)...")
    # Reverse years to start with 2025
    for year in sorted(years, reverse=True):
        try:
            links = fetch_year_links(year)
            print(f"    {year}: {len(links)} PDF(s)")
            # Reverse links within the year to start with December
            all_links.extend(reversed(links))
        except Exception as exc:
            print(f"  ! Discovery failed for {year}: {exc}")
    return all_links


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


def infer_created_date(link: dict) -> date:
    year_hint = int(link.get("year") or datetime.now().year)
    source_bits = [
        str(link.get("date_str") or ""),
        str(link.get("title") or ""),
        unquote(Path(urlsplit(str(link.get("url") or "")).path).stem),
    ]

    month_names = "|".join(sorted(MONTH_MAP.keys(), key=len, reverse=True))
    for text in source_bits:
        lower = text.lower()
        for match in re.finditer(
            rf"\b({month_names})\b[\s._,-]*(\d{{1,2}})(?:st|nd|rd|th)?(?:[\s._,-]+(\d{{2,4}}))?",
            lower,
        ):
            month_token, day_token, year_token = match.group(1), match.group(2), match.group(3)
            month = MONTH_MAP[month_token]
            day = int(day_token)
            year = normalize_year(year_token, year_hint) if year_token else year_hint
            parsed = safe_date(year, month, day)
            if parsed:
                return parsed

        for match in re.finditer(r"(?<!\d)(\d{1,2})[.\-_/ ]+(\d{1,2})[.\-_/ ]+(\d{2,4})(?!\d)", lower):
            month = int(match.group(1))
            day = int(match.group(2))
            year = normalize_year(match.group(3), year_hint)
            parsed = safe_date(year, month, day)
            if parsed:
                return parsed

        for match in re.finditer(r"(?<!\d)(\d{1,2})[.\-_/ ]+(\d{1,2})[.\-_/ ]+(\d{1,2})[.\-_/ ]+(\d{4})(?!\d)", lower):
            month = int(match.group(1))
            day = int(match.group(2))
            year = int(match.group(4))
            parsed = safe_date(year, month, day)
            if parsed:
                return parsed

    return date(year_hint, 1, 1)


def run_orchestrator(
    dry_run: bool = False,
    limit: int | None = None,
    since: int | None = None,
    specific_year: int | None = None,
) -> None:
    TARGET_ROOT.mkdir(parents=True, exist_ok=True)

    state = load_state()
    next_doc_number, seen_urls = load_manifest_inventory()
    starting_count = len(list(TARGET_ROOT.glob("M1.AG.*.pdf")))

    years = AGENDA_YEARS
    if specific_year:
        years = [specific_year]
    elif since:
        years = [y for y in years if y >= since]

    raw_links = discover_agendas(years)
    print(f"\n>>> Found {len(raw_links)} total agenda PDF(s).")
    print(f">>> Destination: {TARGET_ROOT}")
    print(f">>> Existing machine-coded agenda files: {starting_count}")
    print(f">>> Next document number seed: {next_doc_number + 1:06d}")

    stats = {"new": 0, "skipped": 0, "failed": 0, "already_in_manifest": 0, "would_ingest": 0}
    pulled_today = datetime.now().strftime("%Y%m%d")
    run_stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    staging_root = TARGET_ROOT / "_staging" / run_stamp

    try:
        for idx, link in enumerate(raw_links, start=1):
            processed_for_limit = stats["would_ingest"] if dry_run else stats["new"]
            if limit is not None and processed_for_limit >= limit:
                print(f"\n>>> Limit reached ({limit}). Stopping.")
                break

            url = str(link.get("url") or "").strip()
            agenda_id = make_agenda_id(url)
            title = str(link.get("title") or agenda_id)

            if url in seen_urls:
                stats["already_in_manifest"] += 1
                continue

            existing = state.get("processed_ids", {}).get(agenda_id, {})
            if existing and existing.get("status") not in {"failed"}:
                stats["skipped"] += 1
                continue

            print(f"\n[{idx}] {title}")
            print(f"    URL: {url}")

            if dry_run:
                stats["would_ingest"] += 1
                continue

            item_stage = staging_root / f"item_{idx:04d}"
            fetched_path_raw = fetch_pdf(url, item_stage)
            if not fetched_path_raw:
                state["processed_ids"][agenda_id] = {
                    "status": "failed",
                    "date": datetime.now().isoformat(timespec="seconds"),
                    "title": title,
                    "url": url,
                }
                save_state(state)
                stats["failed"] += 1
                continue

            fetched_path = Path(fetched_path_raw)
            # Guard against upstream/network edge cases that return an empty file.
            if not fetched_path.exists() or fetched_path.stat().st_size <= 0:
                print(f"  ! Failed: empty download for {title}")
                try:
                    fetched_path.unlink(missing_ok=True)
                except Exception:
                    pass
                state["processed_ids"][agenda_id] = {
                    "status": "failed",
                    "date": datetime.now().isoformat(timespec="seconds"),
                    "title": title,
                    "url": url,
                    "error": "empty_download",
                }
                save_state(state)
                stats["failed"] += 1
                continue

            created_date = infer_created_date(link).strftime("%Y%m%d")
            next_doc_number += 1
            document_number = f"{next_doc_number:06d}"
            machine_code = f"{MODE}.{SUBGROUP}.{document_number}.{created_date}.{pulled_today}"
            target_path = TARGET_ROOT / f"{machine_code}.pdf"

            while target_path.exists():
                next_doc_number += 1
                document_number = f"{next_doc_number:06d}"
                machine_code = f"{MODE}.{SUBGROUP}.{document_number}.{created_date}.{pulled_today}"
                target_path = TARGET_ROOT / f"{machine_code}.pdf"

            shutil.move(str(fetched_path), str(target_path))

            manifest_row = {
                "machine_code": machine_code,
                "mode": MODE,
                "subgroup": SUBGROUP,
                "document_number": document_number,
                "created_date": created_date,
                "pulled_date": pulled_today,
                "file_name": target_path.name,
                "url": url,
                "agenda_id": agenda_id,
                "title": title,
                "discovery_year": int(link.get("year") or 0),
                "ingested_at": datetime.now().isoformat(timespec="seconds"),
            }
            append_manifest_row(manifest_row)

            state["processed_ids"][agenda_id] = {
                "status": "ok",
                "date": datetime.now().isoformat(timespec="seconds"),
                "title": title,
                "url": url,
                "machine_code": machine_code,
                "file_name": target_path.name,
                "created_date": created_date,
                "pulled_date": pulled_today,
            }
            save_state(state)
            seen_urls.add(url)
            stats["new"] += 1

            # Polite crawl pacing
            time.sleep(random.uniform(2.0, 5.0))
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)
        save_state(state)

    ending_count = len(list(TARGET_ROOT.glob("M1.AG.*.pdf")))
    print("\n" + "=" * 48)
    print("RUN SUMMARY")
    print(f"  Destination root: {TARGET_ROOT}")
    print(f"  Total discovered: {len(raw_links)}")
    print(f"  Existing count at start: {starting_count}")
    print(f"  Existing count at end: {ending_count}")
    if dry_run:
        print(f"  Would ingest (dry-run): {stats['would_ingest']}")
    else:
        print(f"  Newly ingested: {stats['new']}")
    print(f"  Already in manifest: {stats['already_in_manifest']}")
    print(f"  Skipped by state: {stats['skipped']}")
    print(f"  Failed: {stats['failed']}")
    print("=" * 48)


def main() -> int:
    parser = argparse.ArgumentParser(description="Richlands Agenda Pull Orchestrator")
    parser.add_argument("--dry-run", action="store_true", help="Discover only; do not download.")
    parser.add_argument("--limit", type=int, help="Stop after N new ingests.")
    parser.add_argument("--since", type=int, help="Only scan links from this year onward.")
    parser.add_argument("--year", type=int, help="Target a specific year exclusively.")
    args = parser.parse_args()
    run_orchestrator(dry_run=args.dry_run, limit=args.limit, since=args.since, specific_year=args.year)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
