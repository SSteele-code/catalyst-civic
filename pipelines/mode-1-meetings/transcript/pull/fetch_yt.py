#!/usr/bin/env python
import argparse
import subprocess
import sys
import os
import re
from pathlib import Path

def _has_caption_payload(vtt_path: Path) -> bool:
    """
    Basic validity check: transcript must include at least one timestamp line
    and at least one non-header text payload line.
    """
    try:
        raw = vtt_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False

    has_timestamp = "-->" in raw
    payload_lines = 0
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("WEBVTT") or text.startswith("Kind:") or text.startswith("Language:"):
            continue
        if "-->" in text:
            continue
        clean = re.sub(r"<[^>]+>", "", text).strip()
        if clean:
            payload_lines += 1

    return has_timestamp and payload_lines > 0

def fetch_vtt(video_id, output_dir):
    """
    Surgical Fetcher: Downloads the raw .vtt auto-captions for a single Video ID.
    Strict Invariant: Stage 01 ONLY. No text deduplication.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    # Use the spec-mandated flags: --no-check-certificate and --js-runtimes deno
    cmd = [
        "yt-dlp",
        "--no-check-certificate",
        "--js-runtimes", "deno",
        "--remote-components", "ejs:github",
        "--write-auto-sub",
        "--sub-lang", "en",
        "--skip-download",
        "--output", f"{output_dir}/%(id)s",
        url
    ]
    
    print(f"Fetching raw VTT for {video_id}...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        # yt-dlp might output .en.vtt or .vtt depending on settings
        vtt_file = list(Path(output_dir).glob(f"{video_id}.*.vtt"))
        if not vtt_file:
            vtt_file = list(Path(output_dir).glob(f"{video_id}.vtt"))
            
        if vtt_file:
            candidate = vtt_file[0]
            if not _has_caption_payload(candidate):
                print(f"Warning: {candidate.name} has no usable caption payload.")
                try:
                    candidate.unlink(missing_ok=True)
                except Exception:
                    pass
                return None
            print(f"Success: {candidate.name} saved.")
            return str(candidate)
        else:
            print(f"Error: No VTT file found for {video_id}.")
            return None
    except subprocess.CalledProcessError as e:
        print(f"Error fetching VTT: {e.stderr}")
        return None

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Richlands YouTube Fetcher (fetch_yt.py)")
    parser.add_argument("--id", required=True, help="YouTube Video ID")
    parser.add_argument("--outdir", default="./raw", help="Directory for raw .vtt files")
    args = parser.parse_args()
    
    fetch_vtt(args.id, args.outdir)
