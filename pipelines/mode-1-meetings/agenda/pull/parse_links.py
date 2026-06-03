#!/usr/bin/env python
"""
Richlands Agenda Link Parser (parse_links.py)

Scrapes a single yearly agenda HTML page from town.richlands.va.us
and extracts all agenda PDF download links with structured metadata.

Strict Invariant: Discovery ONLY. No downloading.
"""
import argparse
import json
import re
import sys
from html.parser import HTMLParser
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

BASE_URL = "https://town.richlands.va.us/agenda"
YEAR_PAGE_TEMPLATE = BASE_URL + "/{year}AGENDA.html"


class AgendaLinkParser(HTMLParser):
    """Extracts <a> tags whose href points to a .pdf under /agenda/YYYY/."""

    def __init__(self, year):
        super().__init__()
        self.year = str(year)
        self.links = []
        self._current_href = None
        self._current_text_parts = []
        self._in_link = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            # Match both relative (2025/file.pdf) and absolute (/agenda/2025/file.pdf) hrefs
            if href.lower().endswith(".pdf") and (
                href.startswith(f"{self.year}/") or
                f"/agenda/{self.year}/" in href or
                f"/{self.year}/" in href
            ):
                self._current_href = href
                self._current_text_parts = []
                self._in_link = True

    def handle_data(self, data):
        if self._in_link:
            self._current_text_parts.append(data.strip())

    def handle_endtag(self, tag):
        if tag == "a" and self._in_link:
            title = " ".join(self._current_text_parts).strip()
            if self._current_href and title:
                # Ensure absolute URL
                href = self._current_href
                if href.startswith("http"):
                    pass  # already absolute
                elif href.startswith("/"):
                    href = "https://town.richlands.va.us" + href
                else:
                    # Relative path like '2025/file.pdf'
                    href = BASE_URL + "/" + href

                self.links.append({
                    "url": href,
                    "title": title,
                    "year": int(self.year),
                    "date_str": self._extract_date(title),
                })
            self._in_link = False
            self._current_href = None
            self._current_text_parts = []

    @staticmethod
    def _extract_date(title):
        """
        Best-effort date extraction from link text.
        Handles patterns like 'JANUARY 14, 2025 COUNCIL PACKET'
        Returns 'MONTH DD, YYYY' or the raw title if no match.
        """
        m = re.match(
            r"([A-Za-z]+)\s+(\d{1,2}),?\s*(\d{4})",
            title.strip()
        )
        if m:
            return f"{m.group(1).upper()} {m.group(2)}, {m.group(3)}"
        return title.strip()


def fetch_year_links(year):
    """Fetch and parse a single yearly agenda page. Returns list of link dicts."""
    url = YEAR_PAGE_TEMPLATE.format(year=year)
    print(f"  Scanning {url} ...", file=sys.stderr)

    req = Request(url, headers={"User-Agent": "CatalystCivic/1.0 (agenda-puller)"})
    
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            with urlopen(req, timeout=60) as resp:
                html = resp.read().decode("utf-8", errors="replace")
                parser = AgendaLinkParser(year)
                parser.feed(html)
                return parser.links
        except HTTPError as e:
            if e.code == 404:
                print(f"  ! HTTP 404 (Not Found) for year {year}", file=sys.stderr)
                return []
            print(f"  ! HTTP {e.code} error for year {year} (Attempt {attempt}/{max_retries})", file=sys.stderr)
        except URLError as e:
            print(f"  ! URL error for year {year}: {e.reason} (Attempt {attempt}/{max_retries})", file=sys.stderr)
        except Exception as e:
            print(f"  ! Unexpected error for year {year}: {e} (Attempt {attempt}/{max_retries})", file=sys.stderr)
        
        if attempt < max_retries:
            import time
            time.sleep(5)
            
    return []


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Richlands Agenda Link Parser (parse_links.py)"
    )
    ap.add_argument("--year", type=int, required=True,
                    help="Year to scan (e.g. 2025)")
    ap.add_argument("--json", action="store_true",
                    help="Output as JSON array")
    args = ap.parse_args()

    links = fetch_year_links(args.year)

    if args.json:
        print(json.dumps(links, indent=2))
    else:
        print(f"\n  Found {len(links)} agenda PDF(s) for {args.year}:")
        for i, link in enumerate(links, 1):
            print(f"    [{i:>3}] {link['title']}")
            print(f"          {link['url']}")
        print()
