#!/usr/bin/env python
"""
Richlands Transcript Pull Orchestrator

Pipeline:
  1) Discover meeting videos on the Richlands YouTube channel
  2) Fetch raw VTT
  3) Store source VTT assets in:
     _Sources/M1-Meetings/Transcripts
  4) Name with machine code:
     M1-TS-<document_number>-<created_yyyymmdd>-<pulled_yyyymmdd>-<youtube_id>.<ext>
  5) Update state + manifest
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from fetch_yt import fetch_vtt


REPO_ROOT = Path(__file__).resolve().parents[6]
TARGET_ROOT = REPO_ROOT / "_Sources" / "M1-Meetings" / "Transcripts" / "_Vualt" / "YTT"
STATE_FILE = TARGET_ROOT / "yt_state.json"
MANIFEST_FILE = TARGET_ROOT / "M1_TS_MANIFEST.jsonl"

MODE = "M1"
SUBGROUP = "TS"

RICHLANDS_CHANNELS = [
    "https://www.youtube.com/@townofrichlandsvirginia2118/videos",
    "https://www.youtube.com/@townofrichlandsvirginia2118/streams",
]
MEETING_PATTERNS = [
    "council meeting",
    "town council",
    "public hearing",
    "work session",
    "special called",
    "budget work session",
]

# Machine Code Standard: MODE.SUBGROUP.DOC_NUM.CREATED_DATE.PULLED_DATE
FILE_RE = re.compile(
    r"^M1\.TS\.(\d{6})\.(\d{8})\.(\d{8})(?:\.([A-Za-z0-9_-]{11}))?(?:\.R(\d{2}))?\.(txt|vtt)$",
    re.IGNORECASE,
)


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


def is_meeting(title: str) -> bool:
    text = (title or "").lower()
    return any(pattern in text for pattern in MEETING_PATTERNS)


def discover_videos() -> list[dict]:
    videos: list[dict] = []
    print(">>> Stage 01: Discovering historical videos...")
    for url in RICHLANDS_CHANNELS:
        cmd = [
            "yt-dlp",
            "--no-check-certificate",
            "--flat-playlist",
            "--print",
            "%(id)s\t%(title)s\t%(upload_date)s",
            url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=180)
        except subprocess.TimeoutExpired:
            print(f"  ! Discovery timed out for {url}")
            continue
        except subprocess.CalledProcessError as exc:
            print(f"  ! Error scanning {url}: {exc.stderr}")
            continue

        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            vid_id = parts[0].strip()
            title = parts[1].strip()
            upload_date = parts[2].strip()
            if not vid_id or len(vid_id) != 11:
                continue
            if not is_meeting(title):
                continue
            videos.append({"id": vid_id, "title": title, "upload_date": upload_date})

    # stable deterministic ordering
    videos.sort(key=lambda item: (item["upload_date"], item["id"]))
    return videos


def load_manifest_inventory() -> tuple[int, set[str]]:
    max_doc_number = 0
    seen_video_ids: set[str] = set()

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
            vid = str(row.get("youtube_id") or "").strip()
            if vid:
                seen_video_ids.add(vid)

    if TARGET_ROOT.exists():
        for path in TARGET_ROOT.glob("M1.TS.*.txt"):
            match = FILE_RE.match(path.name)
            if match:
                max_doc_number = max(max_doc_number, int(match.group(1)))
                # Note: vid might not be in filename anymore, but manifest handles it
        for path in TARGET_ROOT.glob("M1.TS.*.vtt"):
            match = FILE_RE.match(path.name)
            if match:
                max_doc_number = max(max_doc_number, int(match.group(1)))

    return max_doc_number, seen_video_ids


def append_manifest_row(row: dict) -> None:
    TARGET_ROOT.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=True) + "\n")


def run_orchestrator(dry_run: bool = False, limit: int | None = None, since: str | None = None) -> None:
    TARGET_ROOT.mkdir(parents=True, exist_ok=True)
    state = load_state()
    next_doc_number, seen_video_ids = load_manifest_inventory()
    starting_vtt_count = len(list(TARGET_ROOT.glob("M1.TS.*.vtt")))

    discovered = discover_videos()
    videos = [v for v in discovered if not since or v["upload_date"] >= since]

    print(f">>> Found {len(videos)} matching meeting videos.")
    print(f">>> Destination: {TARGET_ROOT}")
    print(f">>> Existing transcript VTT count: {starting_vtt_count}")
    print(f">>> Next document number seed: {next_doc_number + 1:06d}")

    stats = {"new": 0, "failed": 0, "skipped_state": 0, "already_in_manifest": 0, "would_ingest": 0}
    pulled_today = datetime.now().strftime("%Y%m%d")
    run_stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    staging_root = TARGET_ROOT / "_staging" / run_stamp
    raw_vtt_dir = staging_root / "raw"
    raw_vtt_dir.mkdir(parents=True, exist_ok=True)

    try:
        for idx, video in enumerate(videos, start=1):
            processed_for_limit = stats["would_ingest"] if dry_run else stats["new"]
            if limit is not None and processed_for_limit >= limit:
                print(f"\n>>> Limit reached ({limit}). Stopping.")
                break

            video_id = video["id"]
            title = video["title"]
            created_date = video["upload_date"] or datetime.now().strftime("%Y%m%d")

            if video_id in seen_video_ids:
                stats["already_in_manifest"] += 1
                continue

            state_status = state.get("processed_ids", {}).get(video_id, {})
            if isinstance(state_status, dict):
                if state_status and state_status.get("status") not in {"failed_fetch", "failed_dedup"}:
                    stats["skipped_state"] += 1
                    continue
            elif isinstance(state_status, str):
                if state_status not in {"failed_fetch", "failed_dedup"}:
                    stats["skipped_state"] += 1
                    continue

            print(f"\n[{idx}] Processing: {title} ({video_id})")

            if dry_run:
                stats["would_ingest"] += 1
                continue

            fetched_vtt = fetch_vtt(video_id, raw_vtt_dir)
            if not fetched_vtt:
                state["processed_ids"][video_id] = {
                    "status": "failed_fetch",
                    "date": datetime.now().isoformat(timespec="seconds"),
                    "title": title,
                }
                save_state(state)
                stats["failed"] += 1
                continue

            fetched_vtt_path = Path(fetched_vtt)

            next_doc_number += 1
            document_number = f"{next_doc_number:06d}"
            # Standard Dot-Separator Code: MODE.SUBGROUP.DOC_NUM.CREATED.PULLED
            machine_code = f"{MODE}.{SUBGROUP}.{document_number}.{created_date}.{pulled_today}"

            vtt_target = TARGET_ROOT / f"{machine_code}.vtt"
            revision = 1
            while vtt_target.exists():
                revision += 1
                vtt_target = TARGET_ROOT / f"{machine_code}.R{revision:02d}.vtt"

            shutil.move(str(fetched_vtt_path), str(vtt_target))

            manifest_row = {
                "machine_code": machine_code,
                "mode": MODE,
                "subgroup": SUBGROUP,
                "document_number": document_number,
                "created_date": created_date,
                "pulled_date": pulled_today,
                "youtube_id": video_id,
                "title": title,
                "vtt_file": vtt_target.name,
                "revision": revision,
                "ingested_at": datetime.now().isoformat(timespec="seconds"),
            }
            append_manifest_row(manifest_row)

            state["processed_ids"][video_id] = {
                "status": "ok",
                "date": datetime.now().isoformat(timespec="seconds"),
                "title": title,
                "youtube_id": video_id,
                "machine_code": machine_code,
                "vtt_file": vtt_target.name,
                "created_date": created_date,
                "pulled_date": pulled_today,
            }
            save_state(state)
            seen_video_ids.add(video_id)
            stats["new"] += 1

            time.sleep(random.uniform(3.0, 7.0))
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)
        save_state(state)

    ending_vtt_count = len(list(TARGET_ROOT.glob("M1.TS.*.vtt")))


    print("\n" + "=" * 48)
    print("RUN SUMMARY")
    print(f"  Destination root: {TARGET_ROOT}")
    print(f"  Total discovered: {len(videos)}")
    print(f"  VTT count start -> end: {starting_vtt_count} -> {ending_vtt_count}")
    if dry_run:
        print(f"  Would ingest (dry-run): {stats['would_ingest']}")
    else:
        print(f"  Newly ingested: {stats['new']}")
    print(f"  Already in manifest: {stats['already_in_manifest']}")
    print(f"  Skipped by state: {stats['skipped_state']}")
    print(f"  Failed: {stats['failed']}")
    print("=" * 48)


def main() -> int:
    parser = argparse.ArgumentParser(description="Richlands Transcript Pull Orchestrator")
    parser.add_argument("--dry-run", action="store_true", help="Discover only.")
    parser.add_argument("--limit", type=int, help="Stop after N new videos.")
    parser.add_argument("--since", help="Only process videos from date YYYYMMDD.")
    args = parser.parse_args()
    run_orchestrator(dry_run=args.dry_run, limit=args.limit, since=args.since)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
