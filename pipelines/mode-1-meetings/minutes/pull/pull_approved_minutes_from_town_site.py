#!/usr/bin/env python
"""
Minutes Pull (Approved Website Source Pass)

Pull approved meeting-minutes PDFs from the Richlands town website into:
  _Sources/M1-Meetings/Minutes/_vaulted/

Strict invariant:
  - Pull only (discover + fetch + stage into source store)
  - No DB writes
  - No parsing/enrichment logic
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import time
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import Request, urlopen


MINUTES_INDEX_URL = "https://www.town.richlands.va.us/minutes/minutes.html"

MINUTES_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Minutes")
APPROVED_ROOT = MINUTES_ROOT / "_vaulted"
STAGING_ROOT = MINUTES_ROOT / "_staging"
RUNS_ROOT = APPROVED_ROOT / "_runs"

STATE_FILE = MINUTES_ROOT / "minutes_approved_pull_state.json"
MANIFEST_FILE = MINUTES_ROOT / "M1_MINUTES_APPROVED_PULL_MANIFEST.jsonl"

MODE = "M1"
SUBGROUP = "MN"
USER_AGENT = "CatalystCivic/1.0 (minutes-approved-puller)"

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 90
PACE_RANGE_SECONDS = (0.2, 0.6)
MIN_PDF_BYTES = 256

MACHINE_FILE_RE = re.compile(r"^M1\.MN\.(\d{6})\.(\d{8})\.(\d{8})\.pdf$", re.IGNORECASE)
YEAR_PAGE_RE = re.compile(r"(?i)(?:^|/)(20\d{2}|201\d)MINUTES\.html$")

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


@dataclass(frozen=True)
class YearPage:
    year: int
    url: str


@dataclass(frozen=True)
class MinutesCandidate:
    source_url: str
    title: str
    year: int
    year_page_url: str


@dataclass(frozen=True)
class DownloadedPdf:
    staged_path: Path
    content_type: str
    source_pdf_bytes: int
    source_pdf_sha256: str


class AnchorParser(HTMLParser):
    """Minimal <a> extractor for href + visible link text."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._in_anchor = False
        self._href = ""
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        self._in_anchor = True
        attr_map = dict(attrs)
        self._href = attr_map.get("href") or ""
        self._text_parts = []

    def handle_data(self, data: str) -> None:
        if not self._in_anchor:
            return
        text = data.strip()
        if text:
            self._text_parts.append(text)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._in_anchor:
            return
        text = " ".join(self._text_parts).strip()
        self.links.append((self._href.strip(), text))
        self._in_anchor = False
        self._href = ""
        self._text_parts = []


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"processed_ids": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"processed_ids": {}}


def save_state(state: dict) -> None:
    MINUTES_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def append_manifest_row(row: dict) -> None:
    MINUTES_ROOT.mkdir(parents=True, exist_ok=True)
    with MANIFEST_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=True) + "\n")


def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def load_manifest_inventory() -> tuple[int, set[str], set[int]]:
    max_doc_number = 0
    seen_urls: set[str] = set()
    used_doc_numbers: set[int] = set()

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
                doc_number = int(raw_doc)
                used_doc_numbers.add(doc_number)
                max_doc_number = max(max_doc_number, doc_number)
            raw_url = str(row.get("source_url") or row.get("url") or "").strip()
            if raw_url:
                seen_urls.add(raw_url)

    if APPROVED_ROOT.exists():
        for file_path in APPROVED_ROOT.glob("*.pdf"):
            match = MACHINE_FILE_RE.match(file_path.name)
            if match:
                doc_number = int(match.group(1))
                used_doc_numbers.add(doc_number)
                max_doc_number = max(max_doc_number, doc_number)

    return max_doc_number, seen_urls, used_doc_numbers


def request_bytes(url: str, timeout: int = REQUEST_TIMEOUT_SECONDS) -> tuple[bytes, str] | None:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.read(), str(resp.headers.get("Content-Type") or "").strip()
        except HTTPError as exc:
            if exc.code == 404:
                print(f"  ! HTTP 404: {url}")
                return None
            print(f"  ! HTTP {exc.code} on {url} (attempt {attempt}/{MAX_RETRIES})")
        except (URLError, TimeoutError, OSError) as exc:
            print(f"  ! Network error on {url} (attempt {attempt}/{MAX_RETRIES}): {exc}")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS)
    return None


def request_text(url: str, timeout: int = REQUEST_TIMEOUT_SECONDS) -> str | None:
    fetched = request_bytes(url, timeout=timeout)
    if fetched is None:
        return None
    blob, _content_type = fetched
    return blob.decode("utf-8", errors="replace")


def discover_year_pages(index_url: str) -> list[YearPage]:
    html = request_text(index_url)
    if not html:
        return []

    parser = AnchorParser()
    parser.feed(html)

    found: dict[int, str] = {}
    for href, _text in parser.links:
        if not href:
            continue
        match = YEAR_PAGE_RE.search(href)
        if not match:
            continue
        year = int(match.group(1))
        absolute = urljoin(index_url, href)
        found[year] = absolute

    return [YearPage(year=y, url=found[y]) for y in sorted(found.keys(), reverse=True)]


def is_minutes_pdf_link(url: str) -> bool:
    parts = urlsplit(url)
    path = parts.path.lower()
    if not path.endswith(".pdf"):
        return False
    return path.startswith("/minutes/")


def make_source_id(url: str) -> str:
    path = unquote(urlsplit(url).path)
    marker = "/minutes/"
    lowered = path.lower()
    idx = lowered.find(marker)
    tail = path[idx + len(marker) :] if idx >= 0 else path
    return tail.strip("/").lower()


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


def infer_created_date(title: str, source_url: str, year_hint: int) -> date:
    path_stem = unquote(Path(urlsplit(source_url).path).stem)
    source_bits = [title or "", path_stem]

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

        for match in re.finditer(
            r"(?<!\d)(\d{1,2})[.\-_/ ]+(\d{1,2})[.\-_/ ]+(\d{1,2})[.\-_/ ]+(\d{4})(?!\d)",
            lower,
        ):
            month = int(match.group(1))
            day = int(match.group(2))
            year = int(match.group(4))
            parsed = safe_date(year, month, day)
            if parsed:
                return parsed

    return date(year_hint, 1, 1)


def discover_minutes_candidates(year_pages: list[YearPage]) -> tuple[list[MinutesCandidate], int]:
    candidates: list[MinutesCandidate] = []
    seen: set[str] = set()
    duplicate_links = 0

    print(">>> Stage 01: Discovering approved minutes PDF links...")
    for yp in year_pages:
        html = request_text(yp.url)
        if not html:
            print(f"  ! Failed to read year page: {yp.url}")
            continue

        parser = AnchorParser()
        parser.feed(html)
        year_count = 0

        for href, text in parser.links:
            if not href:
                continue
            absolute = urljoin(yp.url, href)
            if not is_minutes_pdf_link(absolute):
                continue
            if absolute in seen:
                duplicate_links += 1
                continue
            seen.add(absolute)
            candidates.append(
                MinutesCandidate(
                    source_url=absolute,
                    title=text.strip(),
                    year=yp.year,
                    year_page_url=yp.url,
                )
            )
            year_count += 1

        print(f"    {yp.year}: {year_count} approved-minute PDF link(s)")

    return candidates, duplicate_links


def is_pdf_blob(blob: bytes) -> bool:
    if not blob:
        return False
    pos = blob[:1024].find(b"%PDF-")
    return 0 <= pos <= 32


def fetch_pdf(url: str, output_dir: Path) -> tuple[DownloadedPdf | None, str | None]:
    output_dir.mkdir(parents=True, exist_ok=True)
    fetched = request_bytes(url)
    if fetched is None:
        return None, "download_failed"
    blob, content_type = fetched
    if len(blob) <= 0:
        return None, "empty_download"
    if len(blob) < MIN_PDF_BYTES:
        return None, f"too_small_download:{len(blob)}"
    if not is_pdf_blob(blob):
        return None, f"non_pdf_payload:{content_type or 'unknown'}"

    out_path = output_dir / "source.pdf"
    out_path.write_bytes(blob)
    return (
        DownloadedPdf(
            staged_path=out_path,
            content_type=content_type,
            source_pdf_bytes=len(blob),
            source_pdf_sha256=sha256_bytes(blob),
        ),
        None,
    )


def run_pull(
    dry_run: bool = False,
    limit: int | None = None,
    force: bool = False,
    since: int | None = None,
    specific_year: int | None = None,
) -> None:
    APPROVED_ROOT.mkdir(parents=True, exist_ok=True)

    state = load_state()
    processed_ids = state.setdefault("processed_ids", {})

    next_doc_number, seen_urls, used_doc_numbers = load_manifest_inventory()
    starting_count = len(list(APPROVED_ROOT.glob("M1.MN.*.pdf")))

    year_pages = discover_year_pages(MINUTES_INDEX_URL)
    if specific_year is not None:
        year_pages = [yp for yp in year_pages if yp.year == specific_year]
    elif since is not None:
        year_pages = [yp for yp in year_pages if yp.year >= since]

    if not year_pages:
        print("No year pages discovered. Nothing to do.")
        return

    candidates, duplicate_links = discover_minutes_candidates(year_pages)
    print(f"\n>>> Total approved-minute links discovered: {len(candidates)}")
    print(f">>> Duplicate discovered links dropped: {duplicate_links}")
    print(f">>> Destination root: {APPROVED_ROOT}")
    print(f">>> Existing machine-coded minutes files: {starting_count}")
    print(f">>> Next document number seed: {next_doc_number + 1:06d}")

    stats = {"new": 0, "skipped": 0, "failed": 0, "already_in_manifest": 0, "would_pull": 0}
    pulled_today = datetime.now().strftime("%Y%m%d")
    run_stamp = datetime.now().strftime("RUN_%Y%m%dT%H%M%S")
    run_started_at = datetime.now().isoformat(timespec="seconds")
    staging_root = STAGING_ROOT / run_stamp
    run_rows: list[dict] = []
    failure_rows: list[dict] = []

    if not dry_run:
        RUNS_ROOT.mkdir(parents=True, exist_ok=True)
        run_dir = RUNS_ROOT / run_stamp
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        run_dir = RUNS_ROOT / run_stamp

    try:
        if not dry_run:
            staging_root.mkdir(parents=True, exist_ok=True)

        for idx, item in enumerate(candidates, start=1):
            processed_for_limit = stats["would_pull"] if dry_run else stats["new"]
            if limit is not None and processed_for_limit >= limit:
                print(f"\n>>> Limit reached ({limit}). Stopping.")
                break

            source_url = item.source_url
            source_id = make_source_id(source_url)
            title = item.title or source_id

            if not force and source_url in seen_urls:
                stats["already_in_manifest"] += 1
                continue

            existing = processed_ids.get(source_id, {})
            if not force and existing and existing.get("status") not in {"failed"}:
                stats["skipped"] += 1
                continue

            print(f"\n[{idx}] {title}")
            print(f"    URL: {source_url}")

            if dry_run:
                stats["would_pull"] += 1
                continue

            item_stage = staging_root / f"item_{idx:04d}"
            fetched, fetch_error = fetch_pdf(source_url, item_stage)
            if not fetched:
                processed_ids[source_id] = {
                    "status": "failed",
                    "date": datetime.now().isoformat(timespec="seconds"),
                    "title": title,
                    "source_url": source_url,
                    "year_page_url": item.year_page_url,
                    "year": item.year,
                    "error": str(fetch_error or "download_failed"),
                }
                failure_rows.append(
                    {
                        "run_id": run_stamp,
                        "failed_at": datetime.now().isoformat(timespec="seconds"),
                        "source_url": source_url,
                        "source_id": source_id,
                        "title": title,
                        "year": item.year,
                        "error": str(fetch_error or "download_failed"),
                    }
                )
                save_state(state)
                stats["failed"] += 1
                continue

            created_date = infer_created_date(item.title, source_url, item.year).strftime("%Y%m%d")
            next_doc_number += 1
            while next_doc_number in used_doc_numbers:
                next_doc_number += 1

            document_number = f"{next_doc_number:06d}"
            machine_code = f"{MODE}.{SUBGROUP}.{document_number}.{created_date}.{pulled_today}"
            target_path = APPROVED_ROOT / f"{machine_code}.pdf"

            while target_path.exists():
                next_doc_number += 1
                while next_doc_number in used_doc_numbers:
                    next_doc_number += 1
                document_number = f"{next_doc_number:06d}"
                machine_code = f"{MODE}.{SUBGROUP}.{document_number}.{created_date}.{pulled_today}"
                target_path = APPROVED_ROOT / f"{machine_code}.pdf"

            shutil.move(str(fetched.staged_path), str(target_path))

            manifest_row = {
                "machine_code": machine_code,
                "run_id": run_stamp,
                "mode": MODE,
                "subgroup": SUBGROUP,
                "document_number": document_number,
                "created_date": created_date,
                "pulled_date": pulled_today,
                "file_name": target_path.name,
                "source_url": source_url,
                "source_id": source_id,
                "source_pdf_sha256": fetched.source_pdf_sha256,
                "source_pdf_bytes": fetched.source_pdf_bytes,
                "source_content_type": fetched.content_type,
                "title": title,
                "discovery_year": item.year,
                "year_page_url": item.year_page_url,
                "ingested_at": datetime.now().isoformat(timespec="seconds"),
            }
            append_manifest_row(manifest_row)
            run_rows.append(manifest_row)

            processed_ids[source_id] = {
                "status": "ok",
                "date": datetime.now().isoformat(timespec="seconds"),
                "title": title,
                "source_url": source_url,
                "source_id": source_id,
                "machine_code": machine_code,
                "file_name": target_path.name,
                "source_pdf_sha256": fetched.source_pdf_sha256,
                "source_pdf_bytes": fetched.source_pdf_bytes,
                "source_content_type": fetched.content_type,
                "created_date": created_date,
                "pulled_date": pulled_today,
                "year_page_url": item.year_page_url,
                "year": item.year,
            }
            save_state(state)
            seen_urls.add(source_url)
            used_doc_numbers.add(int(document_number))
            stats["new"] += 1

            time.sleep(random.uniform(*PACE_RANGE_SECONDS))
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)
        save_state(state)

    ending_count = len(list(APPROVED_ROOT.glob("M1.MN.*.pdf")))
    if not dry_run:
        run_manifest = run_dir / "minutes_approved_pull_manifest.jsonl"
        with run_manifest.open("w", encoding="utf-8") as f:
            for row in run_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")
        if failure_rows:
            run_failures = run_dir / "minutes_approved_pull_failures.jsonl"
            with run_failures.open("w", encoding="utf-8") as f:
                for row in failure_rows:
                    f.write(json.dumps(row, ensure_ascii=True) + "\n")
        run_summary = {
            "run_id": run_stamp,
            "started_at": run_started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "destination_root": str(APPROVED_ROOT),
            "total_discovered": len(candidates),
            "existing_count_at_start": starting_count,
            "existing_count_at_end": ending_count,
            "newly_pulled": stats["new"],
            "skipped_already_in_manifest": stats["already_in_manifest"],
            "skipped_state": stats["skipped"],
            "failed": stats["failed"],
            "limit": limit,
            "force": force,
            "since": since,
            "year": specific_year,
            "manifest_path": str(MANIFEST_FILE),
            "state_file": str(STATE_FILE),
        }
        (run_dir / "run_summary.json").write_text(
            json.dumps(run_summary, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )

    print("\n" + "=" * 56)
    print("APPROVED MINUTES PULL SUMMARY")
    print(f"  Destination root: {APPROVED_ROOT}")
    print(f"  Total discovered: {len(candidates)}")
    print(f"  Existing count at start: {starting_count}")
    print(f"  Existing count at end: {ending_count}")
    if dry_run:
        print(f"  Would pull (dry-run): {stats['would_pull']}")
    else:
        print(f"  Newly pulled: {stats['new']}")
    print(f"  Skipped (already in manifest): {stats['already_in_manifest']}")
    print(f"  Skipped (state): {stats['skipped']}")
    print(f"  Failed: {stats['failed']}")
    print(f"  Global manifest: {MANIFEST_FILE}")
    print(f"  State file: {STATE_FILE}")
    if not dry_run:
        print(f"  Run artifacts: {run_dir}")
    print("=" * 56)


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull approved minutes PDFs from the town website.")
    parser.add_argument("--dry-run", action="store_true", help="Discover only; do not download files.")
    parser.add_argument("--limit", type=int, help="Stop after N new pulls.")
    parser.add_argument("--since", type=int, help="Only scan links from this year onward.")
    parser.add_argument("--year", type=int, help="Target a single year exclusively.")
    parser.add_argument("--force", action="store_true", help="Reprocess URLs even if already tracked.")
    args = parser.parse_args()

    run_pull(
        dry_run=args.dry_run,
        limit=args.limit,
        force=args.force,
        since=args.since,
        specific_year=args.year,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
