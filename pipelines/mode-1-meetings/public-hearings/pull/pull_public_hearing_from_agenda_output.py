import os
#!/usr/bin/env python
"""
Public Hearing Pull (Agenda Output Pass)

Stages public-hearing notice material out of existing Agenda parser outputs into:
  _Sources/M1-Meetings/Public_Hearings/_staging/<RUN_ID>/

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
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence


AGENDA_OUTPUT_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Agendas" / "_output"
PUBLIC_HEARING_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Public_Hearings"
SOURCE_ROOT = PUBLIC_HEARING_ROOT / "_sources"
STAGING_ROOT = PUBLIC_HEARING_ROOT / "_staging"
STATE_FILE = PUBLIC_HEARING_ROOT / "public_hearing_output_pull_state.json"
MANIFEST_FILE = PUBLIC_HEARING_ROOT / "M1_PUBLIC_HEARING_OUTPUT_PULL_MANIFEST.jsonl"


STRONG_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "notice_of_public_hearing",
        re.compile(r'^\s*["“\(\[]*\s*(?:amended\s+)?notice\s+of\s+public\s+hearing\b', re.IGNORECASE),
    ),
    (
        "public_hearing_notice",
        re.compile(r'^\s*["“\(\[]*\s*public\s+hearing\s+notice\b', re.IGNORECASE),
    ),
    (
        "notice_hereby_public_hearing",
        re.compile(r'^\s*["“\(\[]*\s*notice\s+is\s+hereby\s+given\b', re.IGNORECASE),
    ),
    (
        "please_take_notice_public_hearing",
        re.compile(r'^\s*["“\(\[]*\s*please\s+take\s+notice\b', re.IGNORECASE),
    ),
    (
        "town_council_will_hold_public_hearing",
        re.compile(
            r'^\s*["“\(\[]*\s*(?:the\s+)?(?:richlands\s+)?town\s+council\s+will\s+hold\s+a\s+public\s+hearing\b',
            re.IGNORECASE,
        ),
    ),
    ("will_hold_public_hearing", re.compile(r"\b(will|shall)\s+hold\s+a\s+public\s+hearing\b", re.IGNORECASE)),
    (
        "amended_notice_public_hearing",
        re.compile(r'^\s*["“\(\[]*\s*amended\s+notice\s+of\s+public\s+hearing\b', re.IGNORECASE),
    ),
]

PUBLIC_HEARING_RE = re.compile(r"\bpublic\s+hearing\b", re.IGNORECASE)
PUBLIC_HEARING_LIKE_RE = re.compile(r"\bpublic\s+(?:h|f)e[a-z]{4,8}\b", re.IGNORECASE)
NOTICE_LANGUAGE_RE = re.compile(
    r"\b("
    r"notice\s+is\s+hereby\s+given|please\s+take\s+notice|notice\s+of\s+public\s+hearing|public\s+hearing\s+notice|"
    r"public\s+notice|amended\s+notice\s+of\s+public\s+hearing"
    r")\b",
    re.IGNORECASE,
)
DATE_RE = re.compile(
    r"\b("
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:,\s*\d{4})?"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
    r")\b",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?)\b", re.IGNORECASE)
LOCATION_RE = re.compile(
    r"\b("
    r"council\s+chambers?|municipal\s+building|town\s+hall|located\s+at|washington\s+square|"
    r"richlands,\s+virginia|richlands\s+virginia"
    r")\b",
    re.IGNORECASE,
)
SPECIAL_CALLED_RE = re.compile(r"\bspecial\s+called\s+meeting\b", re.IGNORECASE)
PUBLIC_NOTICE_RE = re.compile(r"\bpublic\s+notice\b", re.IGNORECASE)
FUTURE_HOLD_RE = re.compile(
    r"\b(will|shall)\s+hold\s+(?:a\s+)?(?:joint\s+)?public\s+(?:h|f)e[a-z]{4,8}\b",
    re.IGNORECASE,
)
PURPOSE_RE = re.compile(
    r"\b("
    r"for\s+the\s+purpose\s+of\s+taking\s+public\s+comment|"
    r"to\s+take\s+public\s+comments?|"
    r"to\s+receive\s+public\s+comment|"
    r"opportunity\s+to\s+be\s+heard|"
    r"to\s+consider|"
    r"regarding"
    r")\b",
    re.IGNORECASE,
)
PAST_MEETING_RE = re.compile(
    r"\b("
    r"held\s+a\s+public\s+hearing|"
    r"opened\s+the\s+public\s+hearing|"
    r"close(?:d)?\s+the\s+public\s+hearing|"
    r"adjourn(?:ed)?\s+the\s+public\s+hearing|"
    r"motion\s+to\s+adjourn|"
    r"minutes\s+of\s+public\s+hearing|"
    r"mayor\s+cury"
    r")\b",
    re.IGNORECASE,
)
HEARING_AGENDA_CALL_RE = re.compile(r"\bcall\s+hearing\s+to\s+order\b", re.IGNORECASE)
HEARING_AGENDA_COMMENT_RE = re.compile(
    r"\b(receive\s+public\s+comment|public\s+comment)\b",
    re.IGNORECASE,
)
HEARING_AGENDA_ADJOURN_RE = re.compile(
    r"\badjourn\s+public\s+(?:h|f)e[a-z]{4,8}\b",
    re.IGNORECASE,
)
AGENDA_RE = re.compile(r"\bagenda\b", re.IGNORECASE)
SPEAKER_LABEL_RE = re.compile(
    r"^\s*[A-Z][A-Za-z\.' ]{1,40}\s*(?:-|:)\s+\S+",
    re.IGNORECASE,
)
PAGE_MARKER_RE = re.compile(r"^\s*---\s*page\b", re.IGNORECASE)
ROMAN_HEADING_RE = re.compile(r"^\s*[ivxlcdm]+\s*[\.\)]\s+\S+", re.IGNORECASE)
TITLE_HEADING_RE = re.compile(r"^\s*[A-Z][A-Z/&,\- ]{3,}:\s*$")


@dataclass(frozen=True)
class Excerpt:
    kind: str
    start_line: int
    end_line: int
    text: str
    signals: dict

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
    PUBLIC_HEARING_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def append_manifest_rows(rows: Sequence[dict]) -> None:
    if not rows:
        return
    PUBLIC_HEARING_ROOT.mkdir(parents=True, exist_ok=True)
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


def render_notice_source_text(excerpts: Sequence[Excerpt]) -> str:
    blocks: list[str] = []
    seen: set[str] = set()
    for ex in excerpts:
        text = ex.text.strip()
        if not text:
            continue
        key = sha256_text(text)
        if key in seen:
            continue
        seen.add(key)
        blocks.append(text)
    return ("\n\n".join(blocks).strip() + "\n") if blocks else ""


def write_public_hearing_source_bundle(
    source_txt: Path,
    machine_code: str,
    excerpts: Sequence[Excerpt],
    dry_run: bool,
) -> tuple[Path, Path | None, int]:
    dest_packet_dir = SOURCE_ROOT / machine_code
    notice_txt = dest_packet_dir / f"{machine_code}.public_hearing_notice.txt"
    source_factsheet = source_txt.parent / f"{machine_code}.factsheet.json"
    dest_factsheet = dest_packet_dir / f"{machine_code}.factsheet.json"

    has_factsheet = source_factsheet.exists()
    planned_files = 2 if has_factsheet else 1
    if dry_run:
        return notice_txt, (dest_factsheet if has_factsheet else None), planned_files

    SOURCE_ROOT.mkdir(parents=True, exist_ok=True)
    if dest_packet_dir.exists():
        shutil.rmtree(dest_packet_dir)
    dest_packet_dir.mkdir(parents=True, exist_ok=True)

    notice_txt.write_text(render_notice_source_text(excerpts), encoding="utf-8")
    written_files = 1

    factsheet_path: Path | None = None
    if has_factsheet:
        shutil.copy2(source_factsheet, dest_factsheet)
        factsheet_path = dest_factsheet
        written_files += 1

    return notice_txt, factsheet_path, written_files


def is_section_boundary(line: str) -> bool:
    if not line.strip():
        return False
    if PAGE_MARKER_RE.search(line):
        return True
    if ROMAN_HEADING_RE.search(line):
        return True
    if TITLE_HEADING_RE.search(line):
        return True
    return False


def classify_official_announcement_block(block_text: str) -> tuple[bool, str, float]:
    if not block_text.strip():
        return False, "", 0.0

    first_non_empty = ""
    for line in block_text.splitlines():
        if line.strip():
            first_non_empty = line.strip()
            break
    if first_non_empty and SPEAKER_LABEL_RE.search(first_non_empty):
        return False, "", 0.0

    has_notice = bool(NOTICE_LANGUAGE_RE.search(block_text) or PUBLIC_NOTICE_RE.search(block_text))
    has_hearing = bool(PUBLIC_HEARING_RE.search(block_text) or PUBLIC_HEARING_LIKE_RE.search(block_text))
    has_special_called = bool(SPECIAL_CALLED_RE.search(block_text))
    has_future = bool(FUTURE_HOLD_RE.search(block_text))
    has_schedule = bool(DATE_RE.search(block_text) or TIME_RE.search(block_text))
    has_location = bool(LOCATION_RE.search(block_text))
    has_purpose = bool(PURPOSE_RE.search(block_text))
    has_past = bool(PAST_MEETING_RE.search(block_text))

    if not has_notice:
        return False, "", 0.0
    if not has_schedule:
        return False, "", 0.0
    if not has_location:
        return False, "", 0.0

    if has_special_called:
        return True, "special_called_meeting_notice", 0.94

    if has_hearing:
        if has_past and not (has_future or has_purpose):
            return False, "", 0.0
        confidence = 0.97 if has_future else 0.92
        return True, "public_hearing_notice", confidence

    return True, "public_notice_general", 0.88


def is_official_hearing_agenda_block(block_text: str) -> bool:
    if not block_text.strip():
        return False
    if not AGENDA_RE.search(block_text):
        return False
    if not (PUBLIC_HEARING_RE.search(block_text) or PUBLIC_HEARING_LIKE_RE.search(block_text)):
        return False
    if not HEARING_AGENDA_COMMENT_RE.search(block_text):
        return False
    if not HEARING_AGENDA_ADJOURN_RE.search(block_text):
        return False
    if not (DATE_RE.search(block_text) or TIME_RE.search(block_text)):
        return False
    return True


def extract_notice_block(lines: Sequence[str], start_idx: int, max_forward: int) -> tuple[int, int, str]:
    collected: list[str] = []
    end_idx = start_idx
    blank_streak = 0
    limit = min(len(lines), start_idx + max_forward + 1)

    for idx in range(start_idx, limit):
        line = lines[idx]
        if idx > start_idx and is_section_boundary(line):
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

    text = "\n".join(collected).strip()
    return start_idx + 1, end_idx + 1, text


def extract_public_hearing_excerpts(text: str) -> list[Excerpt]:
    lines = text.splitlines()
    excerpts: list[Excerpt] = []
    seen: set[str] = set()
    covered_indices: set[int] = set()

    for idx, line in enumerate(lines):
        if idx in covered_indices:
            continue

        strong_match = None
        for pattern_name, pattern in STRONG_PATTERNS:
            if pattern.search(line):
                strong_match = pattern_name
                break

        is_structural_candidate = bool(
            PUBLIC_HEARING_RE.search(line)
            or PUBLIC_HEARING_LIKE_RE.search(line)
            or PUBLIC_NOTICE_RE.search(line)
            or NOTICE_LANGUAGE_RE.search(line)
            or SPECIAL_CALLED_RE.search(line)
        )
        if not strong_match and not is_structural_candidate:
            continue

        start_idx = idx
        for back in range(1, 4):
            prev_idx = idx - back
            if prev_idx < 0:
                break
            prev_line = lines[prev_idx]
            if (
                "notice" in prev_line.lower()
                or PUBLIC_NOTICE_RE.search(prev_line)
                or STRONG_PATTERNS[0][1].search(prev_line)
            ):
                start_idx = prev_idx

        start_line, end_line, block = extract_notice_block(lines, start_idx, max_forward=56)
        if not block:
            continue
        accepted, announcement_type, confidence = classify_official_announcement_block(block)
        if not accepted:
            continue

        if announcement_type == "special_called_meeting_notice":
            kind = "special_called_meeting_notice_strong"
        elif announcement_type == "public_notice_general":
            kind = "public_notice_general_strong"
        else:
            kind = "public_hearing_notice_strong"

        if strong_match and kind == "public_hearing_notice_strong":
            match_pattern = strong_match
        else:
            match_pattern = f"structural_{announcement_type}"

        ex = Excerpt(
            kind=kind,
            start_line=start_line,
            end_line=end_line,
            text=block,
            signals={
                "match_strength": "strong",
                "match_pattern": match_pattern,
                "confidence": confidence,
                "official_notice_gate": True,
                "announcement_type": announcement_type,
            },
        )
        if ex.key in seen:
            continue
        seen.add(ex.key)
        excerpts.append(ex)
        for n in range(ex.start_line - 1, ex.end_line):
            covered_indices.add(n)

    for idx, line in enumerate(lines):
        if idx in covered_indices:
            continue

        if not (AGENDA_RE.search(line) or PUBLIC_HEARING_RE.search(line) or PUBLIC_HEARING_LIKE_RE.search(line)):
            continue

        start_idx = idx
        if not AGENDA_RE.search(line):
            found_agenda = False
            for back in range(1, 13):
                prev_idx = idx - back
                if prev_idx < 0:
                    break
                if AGENDA_RE.search(lines[prev_idx]):
                    start_idx = prev_idx
                    found_agenda = True
                    break
            if not found_agenda:
                continue

        lookahead = " ".join(lines[start_idx : min(len(lines), start_idx + 28)])
        if not (PUBLIC_HEARING_RE.search(lookahead) or PUBLIC_HEARING_LIKE_RE.search(lookahead)):
            continue

        start_line, end_line, block = extract_notice_block(lines, start_idx, max_forward=90)
        if not block:
            continue
        if not is_official_hearing_agenda_block(block):
            continue

        ex = Excerpt(
            kind="public_hearing_notice_strong",
            start_line=start_line,
            end_line=end_line,
            text=block,
            signals={
                "match_strength": "strong",
                "match_pattern": "hearing_agenda_official",
                "confidence": 0.93,
                "official_notice_gate": True,
                "announcement_type": "hearing_agenda_notice",
            },
        )
        if ex.key in seen:
            continue
        seen.add(ex.key)
        excerpts.append(ex)
        for n in range(ex.start_line - 1, ex.end_line):
            covered_indices.add(n)

    excerpts.sort(key=lambda e: (e.start_line, e.end_line, e.kind))
    return excerpts


def render_txt_summary(source_txt: Path, excerpts: Sequence[Excerpt]) -> str:
    strong_count = sum(1 for ex in excerpts if ex.kind.endswith("_strong"))
    weak_count = sum(1 for ex in excerpts if ex.kind.endswith("_weak"))
    special_called_count = sum(1 for ex in excerpts if ex.kind.startswith("special_called_meeting_notice"))
    public_notice_general_count = sum(1 for ex in excerpts if ex.kind.startswith("public_notice_general"))
    public_hearing_count = sum(1 for ex in excerpts if ex.kind.startswith("public_hearing_notice"))
    header = [
        f"SOURCE: {source_txt}",
        f"EXCERPTS: {len(excerpts)}",
        f"STRONG_EXCERPTS: {strong_count}",
        f"WEAK_EXCERPTS: {weak_count}",
        f"PUBLIC_HEARING_NOTICE_EXCERPTS: {public_hearing_count}",
        f"SPECIAL_CALLED_NOTICE_EXCERPTS: {special_called_count}",
        f"PUBLIC_NOTICE_GENERAL_EXCERPTS: {public_notice_general_count}",
        "",
    ]
    body: list[str] = []
    for i, ex in enumerate(excerpts, start=1):
        body.append(
            f"[{i:03d}] {ex.kind} lines {ex.start_line}-{ex.end_line} "
            f"(pattern={ex.signals.get('match_pattern')}, confidence={ex.signals.get('confidence')})"
        )
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
    source_notices_written = 0
    source_factsheets_copied = 0
    source_files_written = 0

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
        excerpts = extract_public_hearing_excerpts(text)

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
        notice_source_txt, notice_factsheet, source_file_count = write_public_hearing_source_bundle(
            source_txt=txt_path,
            machine_code=machine_code,
            excerpts=excerpts,
            dry_run=dry_run,
        )
        source_notices_written += 1
        source_files_written += source_file_count
        if notice_factsheet is not None:
            source_factsheets_copied += 1
        strong_count = sum(1 for ex in excerpts if ex.kind.endswith("_strong"))
        weak_count = sum(1 for ex in excerpts if ex.kind.endswith("_weak"))
        payload = {
            "run_id": run_id,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "source_txt": str(notice_source_txt),
            "source_factsheet": str(notice_factsheet) if notice_factsheet else None,
            "source_type": "official_announcement_notice_only",
            "source_agenda_txt": source_key,
            "machine_code": machine_code,
            "excerpt_count": len(excerpts),
            "strong_excerpt_count": strong_count,
            "weak_excerpt_count": weak_count,
            "excerpts": [
                {
                    "kind": ex.kind,
                    "start_line": ex.start_line,
                    "end_line": ex.end_line,
                    "text": ex.text,
                    "signals": ex.signals,
                }
                for ex in excerpts
            ],
        }
        payload_text = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
        extract_hash = sha256_text(payload_text)

        json_out = staging_dir / f"{machine_code}.public_hearing.json"
        txt_out = staging_dir / f"{machine_code}.public_hearing.txt"

        if not dry_run:
            json_out.write_text(payload_text, encoding="utf-8")
            txt_out.write_text(render_txt_summary(txt_path, excerpts), encoding="utf-8")

        row = {
            "run_id": run_id,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "machine_code": machine_code,
            "source_txt": str(notice_source_txt),
            "source_factsheet": str(notice_factsheet) if notice_factsheet else None,
            "source_type": "official_announcement_notice_only",
            "source_agenda_txt": source_key,
            "source_sha256": source_hash,
            "extract_sha256": extract_hash,
            "excerpt_count": len(excerpts),
            "strong_excerpt_count": strong_count,
            "weak_excerpt_count": weak_count,
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
            "strong_excerpt_count": strong_count,
            "weak_excerpt_count": weak_count,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "source_txt": str(notice_source_txt),
            "source_factsheet": str(notice_factsheet) if notice_factsheet else None,
            "source_type": "official_announcement_notice_only",
            "source_agenda_txt": source_key,
            "staged_json": str(json_out),
            "staged_txt": str(txt_out),
        }
        if not dry_run:
            append_manifest_rows([row])
            save_state(state)

    if not dry_run:
        manifest_path = staging_dir / "public_hearing_output_pull_manifest.jsonl"
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
            "source_notices_written": source_notices_written,
            "source_factsheets_copied": source_factsheets_copied,
            "source_files_written": source_files_written,
            "agenda_output_root": str(AGENDA_OUTPUT_ROOT),
            "public_hearing_source_root": str(SOURCE_ROOT),
            "staging_dir": str(staging_dir),
        }
        (staging_dir / "run_summary.json").write_text(
            json.dumps(summary, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )

    print("=" * 64)
    print("PUBLIC HEARING OUTPUT PULL SUMMARY")
    print(f"  Run ID: {run_id}")
    print(f"  Agenda output root: {AGENDA_OUTPUT_ROOT}")
    print(f"  Files scanned: {scanned}")
    print(f"  Files staged: {staged}")
    print(f"  Files skipped (unchanged): {skipped_unchanged}")
    print(f"  Files with no official-announcement hits: {no_hits}")
    if dry_run:
        print("  Dry run: yes (no files written)")
    else:
        print(f"  PH source root: {SOURCE_ROOT}")
        print(f"  Source notices written: {source_notices_written}")
        print(f"  Source factsheets copied: {source_factsheets_copied}")
        print(f"  Source files written: {source_files_written}")
        print(f"  Staging dir: {staging_dir}")
        print(f"  Global manifest: {MANIFEST_FILE}")
        print(f"  State file: {STATE_FILE}")
    print("=" * 64)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pull public-hearing notice material from agenda parser outputs into Public_Hearings staging."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after staging N source files with official-announcement hits.",
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
