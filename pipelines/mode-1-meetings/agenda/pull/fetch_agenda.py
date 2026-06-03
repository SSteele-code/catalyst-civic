#!/usr/bin/env python
"""
Richlands Agenda Fetcher (fetch_agenda.py)

Surgical Fetcher: Downloads a single agenda PDF by URL.
Strict Invariant: Stage 01 ONLY. No parsing or transformation.
"""
import argparse
import time
import sys
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import unquote, urlsplit


MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds


def derive_filename(url):
    """Extract a clean filename from the PDF URL."""
    path = urlsplit(url).path
    raw_name = unquote(path.split("/")[-1])
    # Sanitize: keep alphanumeric, spaces, hyphens, dots, commas
    clean = "".join(c if (c.isalnum() or c in " -.,_()") else "_" for c in raw_name)
    # Collapse multiple underscores
    while "__" in clean:
        clean = clean.replace("__", "_")
    return clean.strip("_")


def fetch_pdf(url, output_dir):
    """
    Download a single agenda PDF.
    Returns the saved file path on success, None on failure.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    filename = derive_filename(url)
    out_path = output_dir / filename

    if out_path.exists():
        print(f"  Already exists: {filename}")
        return str(out_path)

    print(f"  Fetching {filename} ...")
    req = Request(url, headers={"User-Agent": "CatalystCivic/1.0 (agenda-puller)"})

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urlopen(req, timeout=60) as resp:
                data = resp.read()

            # Sanity check: PDF should start with %PDF
            if not data[:5].startswith(b"%PDF"):
                print(f"  ! Warning: Response does not look like a PDF ({filename})")

            with open(out_path, "wb") as f:
                f.write(data)

            size_kb = len(data) / 1024
            print(f"  Success: {filename} ({size_kb:.1f} KB)")
            return str(out_path)

        except (HTTPError, URLError, TimeoutError) as e:
            print(f"  ! Attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    print(f"  ! FAILED after {MAX_RETRIES} attempts: {filename}")
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Richlands Agenda Fetcher (fetch_agenda.py)"
    )
    parser.add_argument("--url", required=True, help="Full URL of the agenda PDF")
    parser.add_argument("--outdir", default="./raw",
                        help="Directory for downloaded PDFs")
    args = parser.parse_args()

    result = fetch_pdf(args.url, args.outdir)
    if not result:
        sys.exit(1)
