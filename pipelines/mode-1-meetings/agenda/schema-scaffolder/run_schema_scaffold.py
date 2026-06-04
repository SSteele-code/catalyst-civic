import os
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


DEFAULT_SOURCE_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Modes" / "M1" / "Agenda"
DEFAULT_SCHEMA_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Schema" / "M1" / "Agenda"

SOURCE_MACHINE_CODE_RE = re.compile(r"^M\d+\.AG\.(\d{6})\.(\d{8})\.(\d{8})$", re.IGNORECASE)
ROMAN_SECTION_RE = re.compile(r"^\s*(\(?)\b(I|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII|XIII|XIV|XV|XVI|XVII|XVIII|XIX|XX)\b([\.\)\s]+)(.*)$", re.IGNORECASE)
# Nested item must not look like a standalone Roman numeral header (e.g. IV.)
# It should only match I-XV if it has a trailing parenthesis, not a period.
NESTED_ITEM_RE = re.compile(r"^(?!(?:I|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII|XIII|XIV|XV)\b[\.\s])([a-z0-9]{1,3})[\.\)]\s+(.*)$", re.IGNORECASE)
MONTH_MAP = {
    "january": "January",
    "february": "February",
    "march": "March",
    "april": "April",
    "may": "May",
    "june": "June",
    "july": "July",
    "august": "August",
    "september": "September",
    "october": "October",
    "november": "November",
    "december": "December",
}
MONTH_NAMES_PATTERN = r"(January|February|March|April|May|June|July|August|September|October|November|December)"
DATE_LONG_RE = re.compile(
    rf"\b(?P<month>{MONTH_NAMES_PATTERN})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s*[,.;:\-]{{0,3}}\s*(?P<year>[12][\d\.\s]{{3,7}})\b",
    re.IGNORECASE,
)
DATE_NUMERIC_RE = re.compile(r"\b(?P<month>\d{1,2})[/-](?P<day>\d{1,2})[/-](?P<year>\d{2,4})\b")
TIME_PATTERNS = [
    re.compile(r"\b(?P<hour>\d{1,2})\s*[:\.]\s*(?P<minute>\d{2})\s*(?P<ampm>[AP])\.?\s*M\.?\b", re.IGNORECASE),
    re.compile(r"\b(?P<hour>\d{1,2})\s*[:\.]\s*(?P<minute>\d{1,2})\s*(?P<ampm>[AP])\.?\s*M\.?\b", re.IGNORECASE),
    re.compile(r"\b(?P<hour>\d{1,2})\s+(?P<minute>\d{2})\s*(?P<ampm>[AP])\.?\s*M\.?\b", re.IGNORECASE),
    re.compile(r"\b(?P<hour>\d{1,2})\s*(?P<ampm>[AP])\.?\s*M\.?\b", re.IGNORECASE),
]
MEETING_TYPE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bPUBLIC\s+HEARING\b", re.IGNORECASE), "PUBLIC HEARING"),
    (
        re.compile(
            r"\b(?:JOINT\s+)?SPECIAL(?:\s+CALL(?:ED)?|\s+CALLED)?(?:\s+TOWN\s+COUNCIL)?\s+MEETING\b",
            re.IGNORECASE,
        ),
        "SPECIAL CALLED MEETING",
    ),
    (re.compile(r"\bBUDGET\s+WORK\s+SESSION\b", re.IGNORECASE), "BUDGET WORK SESSION"),
    (re.compile(r"\bWORK\s*SESSION\b", re.IGNORECASE), "WORK SESSION"),
    (re.compile(r"\bREGULAR(?:\s+TOWN\s+COUNCIL)?\s+MEETING\b", re.IGNORECASE), "REGULAR MEETING"),
    (re.compile(r"\bTOWN\s+COUNCIL\s+MEETING\b", re.IGNORECASE), "TOWN COUNCIL MEETING"),
    (re.compile(r"\bCOUNCIL\s+MEETING\b", re.IGNORECASE), "COUNCIL MEETING"),
]
LOCATION_PATTERNS = [
    re.compile(
        r"\b(?:AT\s+)?(?:THE\s+)?[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,4}\s+TOWN\s+HALL(?:\s+AT\s+\d{1,5}[^,\n]{0,80})?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:AT\s+)?(?:THE\s+)?(?:COUNCIL\s+CHAMBERS?|MUNICIPAL\s+BUILDING|TOWN\s+OFFICE|POLICE\s+DEPARTMENT)\b(?:\s+AT\s+\d{1,5}[^,\n]{0,80})?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:AT\s+)?(?:THE\s+)?(?:HISTORIC\s+)?[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,6}\s+MUSEUM\b(?:\s*,?\s+\d{1,5}[^,\n]{0,80})?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b\d{1,5}\s+[A-Za-z0-9'\-]+(?:\s+[A-Za-z0-9'\-]+){0,7}\s+(?:ST(?:REET)?|RD|ROAD|READ|AVE(?:NUE)?|DR(?:IVE)?|LN|LANE|CT|COURT|BLVD)\b",
        re.IGNORECASE,
    ),
]
LOCATION_VERBS = {"advised", "stated", "asked", "continued", "continue", "requested", "discussed", "reported"}
MEETING_CONTEXT_PATTERNS = [
    re.compile(r"\bregular\s+(?:monthly\s+)?meeting\b", re.IGNORECASE),
    re.compile(r"\bspecial(?:\s+called)?\s+meeting\b", re.IGNORECASE),
    re.compile(r"\bpublic\s+hearing\b", re.IGNORECASE),
    re.compile(r"\btown\s+council\s+held\b", re.IGNORECASE),
    re.compile(r"\bcalled\s+to\s+order\b", re.IGNORECASE),
    re.compile(r"\bcouncil\s+chambers?\b", re.IGNORECASE),
]
COUNCIL_HEADER_RE = re.compile(
    r"\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,4}\s+(?:TOWN|CITY)\s+COUNCIL)\b",
    re.IGNORECASE,
)
SECTION_FALLBACK_MARKER_RE = re.compile(
    r"^\s*[\(\[]?(?P<marker>(?:[IVXLC]{1,6}|[0-9]{1,2}|[A-Z]))[\)\].:\-]?\s+(?P<title>.+)$",
    re.IGNORECASE,
)
FALLBACK_ACTION_PATTERNS = [
    re.compile(r"\bcall\b.*\border\b", re.IGNORECASE),
    re.compile(r"\binvocation\b", re.IGNORECASE),
    re.compile(r"\bpledge\b", re.IGNORECASE),
    re.compile(r"\bapproval\b.*\bagenda\b", re.IGNORECASE),
    re.compile(r"\bconsent\s+agenda\b", re.IGNORECASE),
    re.compile(r"\bauthorization\b.*\bbills\b", re.IGNORECASE),
    re.compile(r"\bpublic\s+comment\b", re.IGNORECASE),
    re.compile(r"\bpublic\s+hearing\b", re.IGNORECASE),
    re.compile(r"\bexecutive\b.*\bclosed\s+session\b", re.IGNORECASE),
    re.compile(r"\battorney\b.*\bcomment", re.IGNORECASE),
    re.compile(r"\btown\s+manager\b.*\breport", re.IGNORECASE),
    re.compile(r"\bcouncil\s+members?\b.*\breport", re.IGNORECASE),
    re.compile(r"\badjourn\b", re.IGNORECASE),
]


@dataclass
class AgendaPage:
    page_number: int
    page_id: str
    page_machine_code: str
    text_lines: list[str]
    extraction_items: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build schema scaffold artifacts from agenda page JSON files."
    )
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--schema-root", type=Path, default=DEFAULT_SCHEMA_ROOT)
    parser.add_argument("--machine-code", type=str, help="Optional source machine code folder to process.")
    return parser.parse_args()


def ensure_ascii_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def artifact_machine_code(source_machine_code: str) -> str:
    match = SOURCE_MACHINE_CODE_RE.fullmatch(source_machine_code)
    if not match:
        return f"{source_machine_code}.SCF1"
    doc_num, created_date, pulled_date = match.groups()
    prefix = source_machine_code.split(".")[:2]
    return f"{prefix[0]}.{prefix[1]}.{doc_num}.SCF1.{created_date}.{pulled_date}"


def parse_page_number_from_filename(path: Path) -> int:
    match = re.search(r"page_(\d+)", path.name, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return 10**9


def read_page(path: Path) -> AgendaPage:
    payload = json.loads(path.read_text(encoding="utf-8"))
    page = payload.get("page") or {}
    extraction = payload.get("extraction") or {}
    text = (payload.get("text") or {}).get("content") or ""
    lines = [line.strip() for line in ensure_ascii_text(text).split("\n") if line.strip()]
    page_number_raw = page.get("source_page_number")
    try:
        page_number = int(page_number_raw)
    except Exception:
        page_number = parse_page_number_from_filename(path)

    extraction_items = []
    raw_items = extraction.get("agenda_items") or []
    for item in raw_items:
        item_str = str(item).strip()
        if item_str:
            extraction_items.append(item_str)

    return AgendaPage(
        page_number=page_number,
        page_id=str(page.get("page_id") or ""),
        page_machine_code=str(page.get("page_machine_code") or ""),
        text_lines=lines,
        extraction_items=extraction_items,
    )


def load_agenda_pages(machine_dir: Path) -> list[AgendaPage]:
    pages: list[AgendaPage] = []
    for page_file in sorted(machine_dir.glob("page_*.json"), key=parse_page_number_from_filename):
        pages.append(read_page(page_file))
    pages.sort(key=lambda p: (p.page_number, p.page_id))
    return pages


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _clean_year_token(raw_year: str) -> int | None:
    digits = "".join(ch for ch in raw_year if ch.isdigit())
    if len(digits) >= 4:
        digits = digits[:4]
    elif len(digits) == 2:
        digits = f"20{digits}"
    else:
        return None
    year = int(digits)
    if 1900 <= year <= 2100:
        return year
    return None


def select_meeting_context_lines(lines: list[str], base_limit: int = 220, scan_limit: int = 1800, window: int = 2) -> list[str]:
    if not lines:
        return []
    selected_indices: set[int] = set(range(0, min(base_limit, len(lines))))
    max_scan = min(scan_limit, len(lines))
    for idx in range(max_scan):
        text = lines[idx]
        if not text:
            continue
        if any(p.search(text) for p in MEETING_CONTEXT_PATTERNS):
            for j in range(max(0, idx - window), min(len(lines), idx + window + 1)):
                selected_indices.add(j)
    return [lines[i] for i in sorted(selected_indices)]


def _format_meeting_date(month: int, day: int, year: int) -> str:
    dt = datetime(year=year, month=month, day=day)
    return f"{dt.strftime('%B')} {dt.day}, {dt.year}"


def extract_meeting_date(lines: list[str]) -> str:
    header = select_meeting_context_lines(lines, base_limit=220, scan_limit=1800, window=2)
    for line in header:
        match = DATE_LONG_RE.search(line)
        if not match:
            continue
        month_name = MONTH_MAP.get(match.group("month").lower())
        year = _clean_year_token(match.group("year"))
        day = int(match.group("day"))
        if month_name and year:
            try:
                month_num = datetime.strptime(month_name, "%B").month
                return _format_meeting_date(month_num, day, year)
            except Exception:
                continue

    for line in header:
        match = DATE_NUMERIC_RE.search(line)
        if not match:
            continue
        month = int(match.group("month"))
        day = int(match.group("day"))
        year_token = match.group("year")
        year = int(f"20{year_token}") if len(year_token) == 2 else int(year_token)
        try:
            return _format_meeting_date(month, day, year)
        except Exception:
            continue
    return ""


def _format_meeting_time(hour: int, minute: int, ampm: str) -> str:
    return f"{hour}:{minute:02d} {ampm.upper()}M"


def extract_meeting_time(lines: list[str]) -> str:
    header = select_meeting_context_lines(lines, base_limit=220, scan_limit=1800, window=2)
    for line in header:
        for pattern in TIME_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            hour = int(match.group("hour"))
            minute = int(match.groupdict().get("minute") or "0")
            ampm = str(match.group("ampm")).upper()
            if hour < 1 or hour > 12 or minute < 0 or minute > 59:
                continue
            return _format_meeting_time(hour, minute, ampm)
    return ""


def extract_meeting_type(lines: list[str]) -> str:
    header_text = "\n".join(select_meeting_context_lines(lines, base_limit=220, scan_limit=1800, window=2))
    for pattern, label in MEETING_TYPE_PATTERNS:
        if pattern.search(header_text):
            return label

    # OCR-tolerant fallback pass for older/noisy scans.
    folded = header_text.lower().translate(str.maketrans({"0": "o", "1": "i", "5": "s"}))
    folded = re.sub(r"[^a-z\s]", " ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    loose_patterns: list[tuple[re.Pattern[str], str]] = [
        (re.compile(r"\bpublic\s+hear[a-z]*\b"), "PUBLIC HEARING"),
        (re.compile(r"\b(?:joint\s+)?speci[a-z]*\s+cal[a-z]*\s+meet[a-z]*\b"), "SPECIAL CALLED MEETING"),
        (re.compile(r"\brecess\s+meeting\b"), "RECESS MEETING"),
        (re.compile(r"\bbudget\s+workshop\b"), "BUDGET WORK SESSION"),
        (re.compile(r"\bwork\s*session\b"), "WORK SESSION"),
        (re.compile(r"\btown\s+council\s+meeting\b"), "TOWN COUNCIL MEETING"),
        (re.compile(r"\bcouncil\s+meeting\b"), "COUNCIL MEETING"),
    ]
    for pattern, label in loose_patterns:
        if pattern.search(folded):
            return label
    return ""


def _looks_sentence_like(text: str) -> bool:
    words = re.findall(r"[A-Za-z]+", text or "")
    if len(words) > 14:
        return True
    lower = f" {_normalize_ws(text).lower()} "
    return any(f" {verb} " in lower for verb in LOCATION_VERBS)


def _clean_location_candidate(value: str) -> str:
    cleaned = _normalize_ws(value).strip(" ,;:-.")
    match = re.search(
        r"(?:[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,4}\s+Town Hall(?:\s+at\s+\d{1,5}.*)?)",
        cleaned,
        re.IGNORECASE,
    )
    if match:
        cleaned = _normalize_ws(match.group(0))
    cleaned = re.sub(r"^(At|at)\s+", "", cleaned)
    if cleaned.isupper():
        cleaned = cleaned.title()
    return cleaned.strip(" ,;:-.")


def _location_score(candidate: str) -> int:
    lower = candidate.lower()
    score = 0
    if "town hall" in lower:
        score += 5
    if re.search(r"\b\d{1,5}\b", candidate):
        score += 2
    if "richlands" in lower:
        score += 2
    score -= max(0, len(candidate.split()) - 10)
    return score


def extract_location(lines: list[str]) -> str:
    context_lines = select_meeting_context_lines(lines, base_limit=220, scan_limit=1800, window=2)
    candidates: list[str] = []
    for line in context_lines:
        line_text = _normalize_ws(line)
        if not line_text:
            continue
        for pattern in LOCATION_PATTERNS:
            for match in pattern.finditer(line_text):
                candidate = _clean_location_candidate(match.group(0))
                if not candidate or _looks_sentence_like(candidate):
                    continue
                candidates.append(candidate)

        lower = line_text.lower()
        if "town hall" in lower and len(line_text.split()) <= 12 and not _looks_sentence_like(line_text):
            candidates.append(_clean_location_candidate(line_text))

    if not candidates:
        for line in context_lines[:160]:
            line_text = _normalize_ws(line)
            if not line_text:
                continue
            match = COUNCIL_HEADER_RE.search(line_text)
            if not match:
                continue
            candidate = _clean_location_candidate(match.group(1))
            if candidate and not _looks_sentence_like(candidate):
                candidates.append(candidate)
                break

    if not candidates:
        return ""
    ranked = sorted(set(candidates), key=lambda c: (_location_score(c), len(c)), reverse=True)
    return ranked[0]


def canonical_unknown(value: str) -> str:
    cleaned = _normalize_ws(value)
    return cleaned if cleaned and cleaned.upper() not in {"UNKNOWN", "N/A", "NONE"} else ""


def normalize_section_title(value: str) -> str:
    return value.strip().rstrip(":")

def title_is_metadata(title: str) -> bool:
    t = title.lower()
    if not t or len(t) < 3: return True
    # 'agenda' alone is often header noise, but 'approval of agenda' is a section.
    # We only block it if it's EXACTLY 'agenda' or 'a g e n d a'
    if t.replace(" ", "") == "agenda": return True
    if any(x in t for x in ["town council meeting", "public hearing", "p.m.", "7:30", "7:00", "town hall"]): return True
    if re.search(r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d", t): return True
    return False


def line_is_readable_text(line: str) -> bool:
    cleaned = _normalize_ws(line)
    if not cleaned:
        return False
    alpha = sum(1 for ch in cleaned if ch.isalpha())
    printable = sum(1 for ch in cleaned if ch.isprintable() and not ch.isspace())
    if printable == 0:
        return False
    if alpha / printable < 0.55:
        return False
    alpha_words = re.findall(r"[A-Za-z]{2,}", cleaned)
    return len(alpha_words) >= 2


def build_fallback_sections(pages: list[AgendaPage]) -> list[dict]:
    sections: list[dict] = []
    seen_titles: set[str] = set()

    def _maybe_add(title: str, page_number: int) -> None:
        normalized_title = normalize_section_title(_normalize_ws(title))
        if not normalized_title:
            return
        key = normalized_title.lower()
        if key in seen_titles:
            return
        if len(normalized_title.split()) > 18:
            return
        if title_is_metadata(normalized_title):
            return
        if not line_is_readable_text(normalized_title):
            return
        section_code = f"F{len(sections) + 1}"
        sections.append(
            {
                "section_code": section_code,
                "section_title": normalized_title,
                "source_page_number": page_number,
                "items": [],
            }
        )
        seen_titles.add(key)

    for page in pages:
        for raw_line in page.text_lines[:220]:
            line = _normalize_ws(raw_line)
            if not line:
                continue

            marker_match = SECTION_FALLBACK_MARKER_RE.match(line)
            if marker_match:
                candidate_title = marker_match.group("title")
                _maybe_add(candidate_title, page.page_number)
                continue

            if any(pattern.search(line) for pattern in FALLBACK_ACTION_PATTERNS):
                _maybe_add(line, page.page_number)

    return sections


def build_sections(pages: list[AgendaPage]) -> list[dict]:
    sections: list[dict] = []
    current: dict | None = None
    seen_codes: set[str] = set()

    for page in pages:
        lines = page.text_lines
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            # 1. Check for Roman Header (Absolute Priority)
            m_header = ROMAN_SECTION_RE.match(line)
            if m_header:
                raw_code = m_header.group(2).upper().strip("()")
                raw_title = m_header.group(4).strip()
                
                # Logic Guard: Prevent false restarts and duplicates
                is_false_restart = False
                if raw_code == "I" and sections and len(sections) > 5:
                    is_false_restart = True
                
                if raw_code in seen_codes:
                    is_false_restart = True

                if not is_false_restart:
                    code = None
                    title = None
                    if raw_title and not title_is_metadata(raw_title):
                        code = raw_code
                        title = normalize_section_title(raw_title)
                    else:
                        for j in range(i + 1, min(i + 31, len(lines))):
                            candidate = lines[j].strip()
                            if ROMAN_SECTION_RE.match(candidate): break 
                            if not title_is_metadata(candidate):
                                code = raw_code
                                title = normalize_section_title(candidate)
                                break
                    
                    if code and title:
                        seen_codes.add(code)
                        title_item_match = re.search(r"^(.*?)(?:\s+|:)(\d[\)\.\-].*)$", title)
                        overflow_item = None
                        if title_item_match:
                            title = title_item_match.group(1).strip().rstrip(":")
                            overflow_item = title_item_match.group(2).strip()

                        current = {
                            "section_code": code,
                            "section_title": title,
                            "source_page_number": page.page_number,
                            "items": [],
                        }
                        
                        if overflow_item:
                            nested_overflow = NESTED_ITEM_RE.match(overflow_item)
                            if nested_overflow:
                                current["items"].append({
                                    "item_label": nested_overflow.group(1).strip(),
                                    "item_text": normalize_section_title(nested_overflow.group(2)),
                                    "source_page_number": page.page_number,
                                })
                            else:
                                current["items"].append({
                                    "item_label": "1",
                                    "item_text": overflow_item,
                                    "source_page_number": page.page_number,
                                })

                        sections.append(current)
                        i += 1
                        continue

            # 2. Check for Nested Items (Secondary)
            nested = NESTED_ITEM_RE.match(line)
            if nested and current is not None:
                label = nested.group(1).strip()
                if not re.match(r"^(I|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII|XIII|XIV|XV)$", label, re.IGNORECASE):
                    text = normalize_section_title(nested.group(2))
                    current["items"].append({
                        "item_label": label,
                        "item_text": text,
                        "source_page_number": page.page_number,
                    })
            
            i += 1
    return sections


def render_schema_suggestion_box(
    source_machine_code: str,
    artifact_code: str,
    meeting_type: str,
    meeting_date: str,
    meeting_time: str,
    location: str,
    sections: list[dict],
    qa_flags: list[str],
) -> str:
    lines: list[str] = []
    lines.append("[SCHEMA_SUGGESTION_BOX]")
    lines.append(f"source_machine_code: {source_machine_code}")
    lines.append(f"artifact_machine_code: {artifact_code}")
    lines.append(f"meeting_type: {meeting_type or 'UNKNOWN'}")
    lines.append(f"meeting_date: {meeting_date or 'UNKNOWN'}")
    lines.append(f"meeting_time: {meeting_time or 'UNKNOWN'}")
    lines.append(f"location: {location or 'UNKNOWN'}")
    lines.append("proposed_tables:")
    lines.append("  - m1_ag_meeting")
    lines.append("  - m1_ag_section")
    lines.append("  - m1_ag_item")
    lines.append("keys:")
    lines.append("  meeting_id: artifact_machine_code")
    lines.append("  section_id: artifact_machine_code + section_ordinal")
    lines.append("  item_id: artifact_machine_code + section_ordinal + item_ordinal")
    lines.append("required_fields:")
    lines.append("  meeting: [artifact_machine_code, source_machine_code, meeting_type, meeting_date, meeting_time, location]")
    lines.append("  section: [artifact_machine_code, section_ordinal, section_code, section_title, source_page_number]")
    lines.append("  item: [artifact_machine_code, section_ordinal, item_ordinal, item_label, item_text, source_page_number]")
    lines.append(f"section_count: {len(sections)}")
    lines.append("qa_flags:")
    if qa_flags:
        for flag in qa_flags:
            lines.append(f"  - {flag}")
    else:
        lines.append("  - none")
    lines.append("[/SCHEMA_SUGGESTION_BOX]")
    return "\n".join(lines)


def render_scaffold_markdown(
    source_machine_code: str,
    artifact_code: str,
    pages: list[AgendaPage],
    meeting_type: str,
    meeting_date: str,
    meeting_time: str,
    location: str,
    sections: list[dict],
    suggestion_box: str,
) -> str:
    lines: list[str] = []
    lines.append(f"# Agenda Scaffold {artifact_code}")
    lines.append("")
    lines.append(f"- Source Machine Code: `{source_machine_code}`")
    lines.append(f"- Artifact Machine Code: `{artifact_code}`")
    lines.append(f"- Source Pages: {', '.join(str(p.page_number) for p in pages) if pages else 'none'}")
    lines.append(f"- Meeting Type: {meeting_type or 'UNKNOWN'}")
    lines.append(f"- Meeting Date: {meeting_date or 'UNKNOWN'}")
    lines.append(f"- Meeting Time: {meeting_time or 'UNKNOWN'}")
    lines.append(f"- Location: {location or 'UNKNOWN'}")
    lines.append("")
    lines.append("## Scaffolded Agenda")

    if not sections:
        lines.append("- No structured sections detected from agenda page text.")
    else:
        for idx, section in enumerate(sections, start=1):
            lines.append("")
            lines.append(f"### {idx}. [{section['section_code']}] {section['section_title']}")
            lines.append(f"- Source Page: {section['source_page_number']}")
            items = section.get("items") or []
            if not items:
                lines.append("- Items: none detected")
            else:
                for item_idx, item in enumerate(items, start=1):
                    lines.append(
                        f"- {item_idx}. ({item['item_label']}) {item['item_text']} "
                        f"[p{item['source_page_number']}]"
                    )

    lines.append("")
    lines.append("## Schema Suggestion")
    lines.append("")
    lines.append("```text")
    lines.append(suggestion_box)
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def build_schema_payload(
    source_machine_code: str,
    artifact_code: str,
    meeting_type: str,
    meeting_date: str,
    meeting_time: str,
    location: str,
    sections: list[dict],
    qa_flags: list[str],
) -> dict:
    section_rows: list[dict] = []
    item_rows: list[dict] = []
    for section_idx, section in enumerate(sections, start=1):
        section_rows.append(
            {
                "artifact_machine_code": artifact_code,
                "section_ordinal": section_idx,
                "section_code": section["section_code"],
                "section_title": section["section_title"],
                "source_page_number": section["source_page_number"],
            }
        )
        for item_idx, item in enumerate(section.get("items") or [], start=1):
            item_rows.append(
                {
                    "artifact_machine_code": artifact_code,
                    "section_ordinal": section_idx,
                    "item_ordinal": item_idx,
                    "item_label": item["item_label"],
                    "item_text": item["item_text"],
                    "source_page_number": item["source_page_number"],
                }
            )

    return {
        "schema_suggestion_version": "SCF1",
        "source_machine_code": source_machine_code,
        "artifact_machine_code": artifact_code,
        "meeting": {
            "artifact_machine_code": artifact_code,
            "source_machine_code": source_machine_code,
            "meeting_type": meeting_type or None,
            "meeting_date": meeting_date or None,
            "meeting_time": meeting_time or None,
            "location": location or None,
        },
        "sections": section_rows,
        "items": item_rows,
        "db_candidate": {
            "tables": ["m1_ag_meeting", "m1_ag_section", "m1_ag_item"],
            "keys": {
                "meeting_id": "artifact_machine_code",
                "section_id": "artifact_machine_code + section_ordinal",
                "item_id": "artifact_machine_code + section_ordinal + item_ordinal",
            },
        },
        "qa_flags": qa_flags,
    }


def process_one_machine_dir(source_root: Path, schema_root: Path, machine_code: str) -> tuple[Path, Path]:
    machine_dir = source_root / machine_code
    pages = load_agenda_pages(machine_dir)
    all_lines = [line for page in pages for line in page.text_lines]

    meeting_type = canonical_unknown(extract_meeting_type(all_lines))
    meeting_date = canonical_unknown(extract_meeting_date(all_lines))
    meeting_time = canonical_unknown(extract_meeting_time(all_lines))
    location = canonical_unknown(extract_location(all_lines))
    sections = build_sections(pages)
    if not sections:
        sections = build_fallback_sections(pages)

    qa_flags: list[str] = []
    if not meeting_type:
        qa_flags.append("missing_meeting_type")
    if not meeting_date:
        qa_flags.append("missing_date")
    if not meeting_time:
        qa_flags.append("missing_time")
    if not location:
        qa_flags.append("missing_location")
    if not sections:
        qa_flags.append("no_sections_detected")

    artifact_code = artifact_machine_code(machine_code)
    suggestion_box = render_schema_suggestion_box(
        source_machine_code=machine_code,
        artifact_code=artifact_code,
        meeting_type=meeting_type,
        meeting_date=meeting_date,
        meeting_time=meeting_time,
        location=location,
        sections=sections,
        qa_flags=qa_flags,
    )

    markdown_payload = render_scaffold_markdown(
        source_machine_code=machine_code,
        artifact_code=artifact_code,
        pages=pages,
        meeting_type=meeting_type,
        meeting_date=meeting_date,
        meeting_time=meeting_time,
        location=location,
        sections=sections,
        suggestion_box=suggestion_box,
    )

    schema_payload = build_schema_payload(
        source_machine_code=machine_code,
        artifact_code=artifact_code,
        meeting_type=meeting_type,
        meeting_date=meeting_date,
        meeting_time=meeting_time,
        location=location,
        sections=sections,
        qa_flags=qa_flags,
    )

    text_out = machine_dir / f"{artifact_code}.md"
    schema_dir = schema_root / machine_code
    schema_dir.mkdir(parents=True, exist_ok=True)
    schema_out = schema_dir / f"{artifact_code}.schema.json"

    text_out.write_text(markdown_payload, encoding="utf-8")
    schema_out.write_text(json.dumps(schema_payload, indent=2) + "\n", encoding="utf-8")
    return text_out, schema_out


def discover_machine_codes(source_root: Path, machine_code: str | None) -> list[str]:
    if machine_code:
        return [machine_code]
    codes: list[str] = []
    for child in source_root.iterdir():
        if child.is_dir() and SOURCE_MACHINE_CODE_RE.fullmatch(child.name):
            codes.append(child.name)
    codes.sort()
    return codes


def main() -> int:
    args = parse_args()
    source_root = args.source_root.resolve()
    schema_root = args.schema_root.resolve()
    schema_root.mkdir(parents=True, exist_ok=True)

    machine_codes = discover_machine_codes(source_root, args.machine_code)
    if not machine_codes:
        print("No machine code folders found to scaffold.")
        return 0

    for code in machine_codes:
        text_out, schema_out = process_one_machine_dir(source_root, schema_root, code)
        print(f"SCAFFOLDED machine_code={code}")
        print(f"  text={text_out}")
        print(f"  schema={schema_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
