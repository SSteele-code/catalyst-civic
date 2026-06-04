import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2

# Database Configuration
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_DB = os.getenv("PG_DB", "catalyst_civic")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASS = os.getenv("PG_PASS", "postgres")
HIGH_RECALL = str(os.getenv("M1_GLOSSARY_HIGH_RECALL", "0")).strip().lower() in {"1", "true", "yes", "on"}
CONTEXT_WINDOW = int(os.getenv("M1_GLOSSARY_CONTEXT_WINDOW", "40"))
MAX_EVIDENCE_LEN = int(os.getenv("M1_GLOSSARY_MAX_EVIDENCE", "160"))
MAX_FACT_CONTEXT_LEN = int(os.getenv("M1_GLOSSARY_MAX_FACT_CONTEXT", "120"))

# Paths for Reference
DATA_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic"))
BASE_DIR = Path(__file__).resolve().parent.parent
GOLDEN_ONTOLOGY_PATH = DATA_ROOT / "_CCO" / "CORE" / "CCO_ONTOLOGY_EXTRACT.json"
REPORTS_DIR = BASE_DIR / "tools" / "reports"
REJECTION_LOG = REPORTS_DIR / "glossary_rejections.jsonl"

NOISE_TERMS = {
    "inaudible",
    "unknown",
    "unintelligible",
    "woy",
    "synsoj",
    "ajeutd",
    "tethe",
}

CONCEPT_TERMS = {
    "analysis",
    "report",
    "resolution",
    "ordinance",
    "budget",
    "amendment",
    "balances",
    "balance",
    "update",
    "discussion",
    "plan",
    "appointment",
    "request",
    "comments",
    "meeting",
    "agenda",
    "minutes",
}

VERB_TERMS = {
    "advised",
    "stated",
    "asked",
    "continue",
    "would",
    "could",
    "should",
    "were",
    "said",
    "think",
}

ACTION_LEAD_TERMS = {
    "account",
    "address",
    "addresses",
    "agenda",
    "allocation",
    "amount",
    "analysis",
    "any",
    "approval",
    "approved",
    "appointment",
    "approp",
    "background",
    "budget",
    "capital",
    "concern",
    "concerns",
    "concerning",
    "dedication",
    "designated",
    "discuss",
    "discussion",
    "expenditure",
    "extent",
    "foreclosure",
    "general",
    "greet",
    "impossible",
    "legal",
    "maintenance",
    "meeting",
    "miscellaneous",
    "number",
    "page",
    "period",
    "presentment",
    "regular",
    "report",
    "revenue",
    "request",
    "restore",
    "schedule",
    "special",
    "stop",
    "summary",
    "title",
    "total",
    "update",
    "vacant",
    "welcome",
}

LOCATION_HINTS = {
    "street",
    "st",
    "road",
    "rd",
    "avenue",
    "ave",
    "lane",
    "ln",
    "court",
    "ct",
    "drive",
    "dr",
    "boulevard",
    "blvd",
    "hall",
    "chamber",
    "chambers",
    "building",
    "center",
    "office",
    "department",
}

STREET_SUFFIX_PATTERN = (
    r"(?:STREET|ST|ROAD|RD|AVENUE|AVE|DRIVE|DR|WAY|COURT|CT|LANE|LN|BOULEVARD|BLVD)"
)

PERSON_BLOCK_TERMS = {
    "agenda",
    "allegiance",
    "approval",
    "attorney",
    "award",
    "board",
    "budget",
    "call",
    "called",
    "capital",
    "cancellation",
    "closed",
    "committee",
    "commission",
    "comments",
    "consultation",
    "council",
    "department",
    "discussion",
    "drive",
    "electric",
    "fire",
    "general",
    "hearing",
    "invocation",
    "legal",
    "manager",
    "meeting",
    "minutes",
    "municipal",
    "order",
    "planning",
    "presentation",
    "project",
    "public",
    "rates",
    "recess",
    "report",
    "resolution",
    "scheduled",
    "section",
    "session",
    "special",
    "treatment",
    "truck",
    "update",
    "league",
    "ncc",
    "wwtp",
    "dmv",
    "usda",
    "task",
    "order",
    "investment",
    "policies",
    "collections",
    "lease",
    "event",
    "issues",
    "venture",
    "instructor",
    "pg",
    "what",
    "does",
}

PERSON_TAIL_NOISE_TERMS = {
    "agenda",
    "collections",
    "comments",
    "contract",
    "discussion",
    "event",
    "fluoride",
    "instructor",
    "investment",
    "issues",
    "lease",
    "policies",
    "presentation",
    "project",
    "report",
    "task",
    "teen",
    "update",
    "venture",
    "what",
    "wwtp",
    "pg",
    "does",
    "club",
}


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _normalize_upper(text: str) -> str:
    return _normalize_ws(text).upper()


def _clip(text: str, limit: int) -> str:
    s = _normalize_ws(text)
    if limit <= 0 or len(s) <= limit:
        return s
    return s[: max(1, limit - 1)].rstrip() + "…"


def _clean_name(name: str) -> str:
    s = _normalize_ws(name)
    s = s.strip(" ,;:.|-")
    return s


def _infer_org_category(name: str) -> str:
    low = _normalize_ws(name).lower()
    if re.search(r"\b(department|agency|authority|administration|district|division|office)\b", low):
        return "AGENCY"
    if re.search(r"\b(board|commission|committee|council)\b", low):
        return "BOARD"
    return "ORGANIZATION"


def _normalize_ocr_suffix(name: str) -> str:
    text = name
    fixes = {
        r"\bCounciy\b": "Council",
        r"\bCounci\b": "Council",
        r"\bComnission\b": "Commission",
        r"\bComnittee\b": "Committee",
        r"\bDepartnent\b": "Department",
        r"\bAuthorty\b": "Authority",
        r"\bAuthorlty\b": "Authority",
    }
    for pat, repl in fixes.items():
        text = re.sub(pat, repl, text, flags=re.IGNORECASE)
    return _clean_name(text)


def _extract_street_addresses(text: str) -> list[str]:
    if not text:
        return []
    pattern = (
        r"\b(\d{1,5}\s+[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){0,6}\s+"
        + STREET_SUFFIX_PATTERN
        + r")\.?\b"
    )
    seen: set[str] = set()
    out: list[str] = []
    for m in re.finditer(pattern, text, re.IGNORECASE):
        addr = _clean_name(m.group(1))
        key = addr.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(addr)
    return out


def _normalize_meeting_location(loc_raw: str) -> str:
    loc = _clean_name(loc_raw)
    if not loc:
        return loc
    addresses = _extract_street_addresses(loc)
    if addresses and " or " in f" {loc.lower()} ":
        return addresses[-1]
    return loc


def _normalize_location_canonical(name: str, fact_key: str) -> str:
    s = _clean_name(name)
    if not s:
        return s

    # Normalize duplicated OCR suffix artifacts like "Rd- Road".
    s = re.sub(r"\b(?:Rd|Road)\s*-\s*Road\b", "Road", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(?:St|Street)\s*-\s*Street\b", "Street", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(?:Ave|Avenue)\s*-\s*Avenue\b", "Avenue", s, flags=re.IGNORECASE)
    s = re.sub(r"\bBrags\b", "Bragg", s, flags=re.IGNORECASE)
    s = _clean_name(s)
    low = s.lower()

    # Canonicalize recurring civic venue aliases.
    if "town hall" in low:
        if "council chamber" in low:
            return "Council Chambers at Richlands Town Hall"
        if "200" in low and "washington" in low:
            return "Town Hall at 200 Washington Square, Richlands, VA"
        if low in {"me town hall", "at richlands town hall", "the richlands town hall", "richlands town hall"}:
            return "Richlands Town Hall"
    if low in {"council chambers", "council chamber"}:
        return "Council Chambers"
    if low in {"the municipal building", "municipal building"}:
        return "Municipal Building"
    if low in {"the police department", "police department"}:
        return "Police Department"

    if fact_key == "MEETING_LOCATION":
        s = re.sub(r"^(?:the|at)\s+", "", s, flags=re.IGNORECASE)
        if re.search(r"richlands town hall at 200\b", s, flags=re.IGNORECASE):
            return "Town Hall at 200 Washington Square, Richlands, VA"

    return s


def _strip_context_prefix(name: str, category: str) -> str:
    if category not in {"ORGANIZATION", "BOARD", "AGENCY"}:
        return name
    if "-" not in name:
        return name
    left, right = name.split("-", 1)
    left = _normalize_ws(left)
    right = _clean_name(right)
    if not left or not right:
        return name
    left_words = _word_tokens(left)
    right_words = _word_tokens(right)
    if not right_words:
        return name
    right_last = right_words[-1].lower()
    if right_last not in {
        "agency",
        "association",
        "authority",
        "board",
        "center",
        "centre",
        "chamber",
        "clinic",
        "commission",
        "committee",
        "council",
        "department",
        "project",
    }:
        return name
    left_is_title = left.lower() in {"chair", "mayor", "manager", "clerk", "attorney", "chief"}
    left_is_person = len(left_words) in {2, 3} and all(re.fullmatch(r"[A-Z][a-z]+", w) for w in left_words)
    if left_is_title or left_is_person:
        return right
    return name


def _looks_action_phrase(words: list[str]) -> bool:
    if not words:
        return False
    lead = words[0].split("-", 1)[0].lower()
    return lead in ACTION_LEAD_TERMS


def _name_tokens_lower(name: str) -> list[str]:
    return re.findall(r"[a-z]+", (name or "").lower())


def _looks_table_phrase(lower_words: list[str]) -> bool:
    table_terms = {
        "account",
        "amount",
        "approp",
        "description",
        "expenditure",
        "line",
        "number",
        "page",
        "period",
        "revenue",
        "total",
        "ytd",
    }
    return any(w in table_terms for w in lower_words)


def _word_tokens(name: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'\-]*", name or "")


def _normalize_person_name(name: str) -> str:
    cleaned = _clean_name(name)
    toks = _word_tokens(cleaned)
    if not toks:
        return cleaned
    norm: list[str] = []
    for tok in toks:
        if tok.isupper() and len(tok) > 1:
            norm.append(tok.title())
        else:
            norm.append(tok)
    return " ".join(norm)


def _person_dedup_key(name: str) -> str:
    """Reduce 'John E. Smith' → 'JOHN SMITH' for within-run dedup.

    Keeps only the first and last token so middle names and initials don't
    create separate entries for the same person.
    """
    toks = _word_tokens(name)
    if len(toks) <= 2:
        return name.upper()
    return f"{toks[0]} {toks[-1]}".upper()


def _is_likely_person_name(name: str) -> bool:
    toks = _word_tokens(name)
    if len(toks) < 2 or len(toks) > 4:
        return False
    low = [t.lower() for t in toks]
    if any(t in PERSON_BLOCK_TERMS for t in low):
        return False
    bad_caps = [t for t in toks if len(t) >= 3 and t.isupper() and t not in {"III", "II", "IV", "JR", "SR"}]
    if bad_caps:
        return False
    if low[-1] in PERSON_TAIL_NOISE_TERMS:
        return False
    for raw_tok in toks:
        if "-" not in raw_tok:
            continue
        parts = [p.lower() for p in raw_tok.split("-") if p]
        if len(parts) >= 2 and any(p in PERSON_TAIL_NOISE_TERMS for p in parts[1:]):
            return False
    disallowed_last = {
        "agency",
        "association",
        "authority",
        "avenue",
        "bluff",
        "board",
        "center",
        "chamber",
        "clinic",
        "commission",
        "committee",
        "council",
        "department",
        "drive",
        "league",
        "park",
        "project",
        "road",
    }
    if low[-1] in disallowed_last:
        return False
    good_shape = 0
    for t in toks:
        if re.fullmatch(r"[A-Z][a-zA-Z'\-]{1,}", t) or re.fullmatch(r"[A-Z]{2,}", t):
            good_shape += 1
    return good_shape >= 2


def _looks_sentence_like(name: str) -> bool:
    words = _word_tokens(name)
    if len(words) > 8:
        return True
    low = (name or "").lower()
    if sum(1 for v in VERB_TERMS if f" {v} " in f" {low} ") >= 1 and len(words) > 5:
        return True
    return False


def _has_weird_chars(name: str) -> bool:
    return re.search(r"[^A-Za-z0-9\s\-\.'&,/()]", name or "") is not None


def _rejection_entry(
    source_id: str,
    category: str,
    canonical_name: str,
    reasons: list[str],
    evidence: str,
) -> dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source_id": source_id,
        "category": category,
        "canonical_name": canonical_name,
        "reasons": reasons,
        "evidence": _clip(evidence, 500),
    }


def log_rejections(entries: list[dict[str, Any]]) -> None:
    if not entries:
        return
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with REJECTION_LOG.open("a", encoding="utf-8") as f:
        for row in entries:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")


def extract_suggestion_meta(scaffold_text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    suggestion_box = re.search(
        r"\[SCHEMA_SUGGESTION_BOX\](.*?)\[/SCHEMA_SUGGESTION_BOX\]",
        scaffold_text,
        re.DOTALL,
    )
    if not suggestion_box:
        return meta
    for line in suggestion_box.group(1).strip().splitlines():
        if ":" in line and not line.strip().startswith("-"):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta


def extract_agenda_scope(scaffold_text: str) -> str:
    m = re.search(
        r"## Scaffolded Agenda\s*[\r\n]+(.*?)(?=[\r\n]+#{1,2}\s+|$)",
        scaffold_text,
        re.DOTALL,
    )
    if not m:
        return scaffold_text
    return m.group(1).strip()


def extract_full_page_text_from_mode(scaffold_path: Path) -> str:
    mode_dir = scaffold_path.resolve().parent
    chunks: list[str] = []
    for page_json in sorted(mode_dir.glob("page_*.json")):
        try:
            payload = json.loads(page_json.read_text(encoding="utf-8"))
            text = str(((payload.get("text") or {}).get("content")) or "").strip()
            if text:
                chunks.append(text)
        except Exception:
            continue
    return "\n".join(chunks).strip()


class OntologicalHoover:
    def __init__(self, golden_path: Path):
        self.golden_path = golden_path
        self.known_people: dict[str, dict[str, str]] = {}
        self.known_orgs: dict[str, dict[str, str]] = {}
        self.known_locations: set[str] = set()
        self._load_knowledge()

    def _load_knowledge(self) -> None:
        if not self.golden_path.exists():
            return
        try:
            data = json.loads(self.golden_path.read_text(encoding="utf-8"))
            for p in data.get("people", []):
                name = str(p.get("full_name") or "").strip()
                if not name:
                    continue
                self.known_people[name.upper()] = {
                    "id": str(p.get("entity_id") or ""),
                    "role": str(p.get("role") or "PEOPLE"),
                }
                for alias in p.get("aliases", []):
                    alias = str(alias or "").strip()
                    if alias:
                        self.known_people[alias.upper()] = {
                            "id": str(p.get("entity_id") or ""),
                            "role": str(p.get("role") or "PEOPLE"),
                        }

            for b in data.get("boards", []):
                name = str(b.get("legal_name") or "").strip()
                if name:
                    self.known_orgs[name.upper()] = {
                        "id": str(b.get("entity_id") or ""),
                        "cat": "BOARD",
                    }

            for a in data.get("agencies", []):
                name = str(a.get("legal_name") or "").strip()
                if name:
                    self.known_orgs[name.upper()] = {
                        "id": str(a.get("entity_id") or ""),
                        "cat": "AGENCY",
                    }

            for s in data.get("sessions", []):
                loc = str(s.get("location") or "").strip()
                if loc:
                    self.known_locations.add(loc.upper())
        except Exception as e:
            print(f"Hoover warning: Failed to load golden knowledge: {e}")

    def _get_context(self, m: re.Match[str], text: str, window: int = CONTEXT_WINDOW) -> str:
        start = max(0, m.start() - window)
        end = min(len(text), m.end() + window)
        snippet = _clip(text[start:end], MAX_FACT_CONTEXT_LEN)
        return f"...{snippet}..."

    def _source_contains_inferred_pair(self, source_text: str, first: str, last: str) -> bool:
        first = _clean_name(first)
        last = _clean_name(last)
        if not source_text or not first or not last:
            return False
        pattern = (
            rf"\b{re.escape(first)}\b\s*(?:&|and)\s+"
            rf"[A-Za-z][A-Za-z'\-]+\s+\b{re.escape(last)}\b"
        )
        return re.search(pattern, source_text, flags=re.IGNORECASE) is not None

    def _accept(self, ent: dict[str, Any], source_text: str) -> tuple[bool, list[str]]:
        category = str(ent.get("category") or "").upper()
        name = _clean_name(str(ent.get("canonical_name") or ""))
        fact_key = str(ent.get("fact_key") or "").upper()
        reasons: list[str] = []

        if not name:
            reasons.append("empty_name")
            return False, reasons
        if len(name) < 3:
            reasons.append("too_short")
        if len(name) > 90:
            reasons.append("too_long")
        if _has_weird_chars(name):
            reasons.append("odd_chars")
        low = name.lower()

        words = _word_tokens(name)
        lower_words = [w.lower() for w in words]
        clean_tokens = _name_tokens_lower(name)

        if HIGH_RECALL:
            # High-recall mode: keep minimal structural guards only.
            # Goal is to maximize capture of people/org/location mentions.
            if source_text and _normalize_upper(name) not in _normalize_upper(source_text):
                reasons.append("not_in_source_text")
            return len(reasons) == 0, reasons

        if _looks_sentence_like(name):
            reasons.append("sentence_like")
        if any(t in low.split() for t in NOISE_TERMS):
            reasons.append("known_noise")
        if any(len(w) >= 5 and not re.search(r"[aeiou]", w.lower()) for w in words):
            reasons.append("vowelless_token")

        if category == "PEOPLE":
            if len(words) < 2 or len(words) > 4:
                reasons.append("people_bad_word_count")
            if any(w in CONCEPT_TERMS for w in lower_words):
                reasons.append("people_is_concept")
            if not re.search(r"\b[A-Z][a-z]", name):
                reasons.append("people_not_title_case")

        if category in {"ORGANIZATION", "BOARD", "AGENCY"}:
            if len(words) > 8:
                reasons.append("org_too_many_words")
            if _looks_action_phrase(words):
                reasons.append("org_action_phrase")
            if any(w in ACTION_LEAD_TERMS for w in clean_tokens):
                reasons.append("org_context_terms")
            if _looks_table_phrase(lower_words):
                reasons.append("org_table_phrase")
            if category == "BOARD":
                suffix = lower_words[-1] if lower_words else ""
                if (
                    suffix in {"council", "board", "commission", "committee"}
                    and len(words) == 3
                    and all(re.fullmatch(r"[A-Z][a-z]+", w) for w in words[:2])
                ):
                    reasons.append("board_person_phrase")
                if len(lower_words) == 2 and lower_words[0] == "the" and suffix in {"board", "council", "committee", "commission"}:
                    reasons.append("board_generic_phrase")

        if category == "LOCATION":
            if len(words) > 7:
                reasons.append("location_too_many_words")
            if any(w in VERB_TERMS for w in lower_words):
                reasons.append("location_contains_verb")
            if _looks_action_phrase(words):
                reasons.append("location_action_phrase")
            has_number = re.search(r"\b\d{1,5}\b", name) is not None
            has_street_suffix = bool(lower_words) and lower_words[-1] in {
                "street",
                "st",
                "road",
                "rd",
                "avenue",
                "ave",
                "lane",
                "ln",
                "court",
                "ct",
                "drive",
                "dr",
                "boulevard",
                "blvd",
                "way",
            }
            has_hint = any(w in LOCATION_HINTS for w in lower_words)
            weak_signal = not (
                has_hint
                or "town hall" in low
                or (has_street_suffix and (has_number or len(words) <= 3))
            )
            # Meeting-location metadata often uses venue names without street numbers.
            if weak_signal and fact_key != "MEETING_LOCATION":
                reasons.append("location_weak_signal")
            if has_street_suffix and not has_number and len(words) > 3:
                reasons.append("location_verbose_street_phrase")
            if re.search(r"\b\d+\s+feet\b", low):
                reasons.append("location_measurement_phrase")
            if any(
                w in {
                    "approval",
                    "because",
                    "between",
                    "comes",
                    "extent",
                    "for",
                    "fund",
                    "impossible",
                    "includes",
                    "read",
                    "run",
                    "this",
                }
                for w in clean_tokens
            ):
                reasons.append("location_context_noise")
            number_tokens = re.findall(r"\b\d+\b", name)
            if has_street_suffix and len(number_tokens) > 1:
                reasons.append("location_multi_number_phrase")
            if " or " in f" {low} ":
                reasons.append("location_disjunction_phrase")
            if fact_key == "MEETING_LOCATION":
                if not (has_hint or has_street_suffix or "town hall" in low or "municipal building" in low):
                    reasons.append("meeting_location_unstructured")

        if source_text and _normalize_upper(name) not in _normalize_upper(source_text):
            allow_pair_inference = False
            if category == "PEOPLE" and fact_key == "POTENTIAL_IDENTITY":
                fv = ent.get("fact_value")
                if isinstance(fv, dict) and fv.get("pair_inferred"):
                    first = str(fv.get("pair_first") or "").strip()
                    last = str(fv.get("pair_last") or "").strip()
                    allow_pair_inference = self._source_contains_inferred_pair(source_text, first, last)
            if not allow_pair_inference:
                reasons.append("not_in_source_text")

        return len(reasons) == 0, reasons

    def hoover(
        self,
        source_text: str,
        meeting_meta: dict[str, str] | None = None,
        source_id: str = "UNKNOWN",
        validation_text: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        found: list[dict[str, Any]] = []
        rejected_log: list[dict[str, Any]] = []
        upper_text = (source_text or "").upper()

        def _append_person(
            name_raw: str,
            context: str,
            evidence_prefix: str,
            pair_inferred: tuple[str, str] | None = None,
            speaker_section: str | None = None,
        ) -> None:
            name = _normalize_person_name(name_raw)
            name = re.sub(
                r"^(?:Mr|Mrs|Ms|Dr|Mayor|Mayo|Clerk|Attorney|Chief|Member|Council\s+Member|Councilman|Councilwoman)\.?\s+",
                "",
                name,
                flags=re.IGNORECASE,
            )
            name = _clean_name(name)
            if not _is_likely_person_name(name):
                return
            fact_value: dict[str, Any] = {"context": context}
            if pair_inferred:
                fact_value["pair_inferred"] = True
                fact_value["pair_first"] = pair_inferred[0]
                fact_value["pair_last"] = pair_inferred[1]
            if speaker_section:
                fact_value["speaker_section"] = speaker_section
            found.append(
                {
                    "category": "PEOPLE",
                    "canonical_name": name,
                    "fact_key": "POTENTIAL_IDENTITY",
                    "fact_value": fact_value,
                    "evidence": f"{evidence_prefix}: {context}",
                }
            )

        # 1) Known people
        for name, info in self.known_people.items():
            for m in re.finditer(re.escape(name), upper_text):
                context = self._get_context(m, source_text)
                found.append(
                    {
                        "category": "PEOPLE",
                        "canonical_name": name.title(),
                        "fact_key": "MENTIONED_IN_RECORD",
                        "fact_value": {
                            "role": info.get("role", "PEOPLE"),
                            "known_id": info.get("id", ""),
                            "context": context,
                        },
                        "evidence": f"Found '{name}' in context: {context}",
                    }
                )

        # 2) Known orgs/boards/agencies
        for name, info in self.known_orgs.items():
            for m in re.finditer(re.escape(name), upper_text):
                context = self._get_context(m, source_text)
                found.append(
                    {
                        "category": info.get("cat", "ORGANIZATION"),
                        "canonical_name": name.title(),
                        "fact_key": "MENTIONED_IN_RECORD",
                        "fact_value": {"known_id": info.get("id", ""), "context": context},
                        "evidence": f"Found '{name}' in context: {context}",
                    }
                )

        # 3) Discover organizations
        org_patterns = [
            r"\b([A-Z][A-Za-z&'\-]+(?:\s+[A-Z][A-Za-z&'\-]+){0,6}\s+(?i:Committee|Board|Commission|Authority|Association|Department|Agency|Center|Centre|Clinic|Chamber|Project|Council))\b",
            r"\b(?:VDOT|PCA|SGR|RFP|CDBG|WWTP)\b",
        ]
        for pattern in org_patterns:
            for m in re.finditer(pattern, source_text):
                context = self._get_context(m, source_text)
                org_name = m.group(0).strip()
                found.append(
                    {
                        "category": _infer_org_category(org_name),
                        "canonical_name": org_name,
                        "fact_key": "MENTIONED_IN_RECORD",
                        "fact_value": {"context": context},
                        "evidence": f"Detected organization in context: {context}",
                    }
                )

        # 3b) High-recall OCR-tolerant org/agency/board discovery
        if HIGH_RECALL:
            fallback_org_pattern = (
                r"\b([A-Za-z][A-Za-z&'\-]+(?:\s+[A-Za-z][A-Za-z&'\-]+){0,6}\s+"
                r"(?:Council|Counciy|Counci|Committee|Comnittee|Commission|Comnission|"
                r"Department|Departnent|Authority|Authorty|Authorlty|Agency|Board))\b"
            )
            for m in re.finditer(fallback_org_pattern, source_text, re.IGNORECASE):
                context = self._get_context(m, source_text)
                org_name = _normalize_ocr_suffix(m.group(0).strip())
                found.append(
                    {
                        "category": _infer_org_category(org_name),
                        "canonical_name": org_name,
                        "fact_key": "MENTIONED_IN_RECORD",
                        "fact_value": {"context": context, "ocr_tolerant": True},
                        "evidence": f"OCR-tolerant organization detected in context: {context}",
                    }
                )

        # 4) People by title + name
        person_patterns = [
            r"(?:MAYOR|COUNCIL\s+MEMBER|TOWN\s+MANAGER|CLERK|ATTORNEY|CHIEF)\s+([A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+){1,2})",
            r"\((?:Mr|Mrs|Ms|Dr)\)\s*([A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+){1,2})",
        ]
        for pattern in person_patterns:
            for m in re.finditer(pattern, source_text, re.MULTILINE):
                context = self._get_context(m, source_text)
                _append_person(m.group(1).strip(), context, "Detected potential person in context")

        # 4b) Section-anchored speaker extraction from scaffolded agenda blocks.
        section_keywords = {
            "council member reports",
            "department head reports",
            "scheduled public comments",
            "unscheduled public comments",
            "recognition",
        }
        section_block_pattern = r"###\s+\d+\.\s+\[[^\]]+\]\s+([^\n]+)\n(.*?)(?=\n###\s+\d+\.\s+\[[^\]]+\]\s+|\Z)"
        for sec in re.finditer(section_block_pattern, source_text, re.IGNORECASE | re.DOTALL):
            header = (sec.group(1) or "").strip().lower()
            body = sec.group(2) or ""
            if not any(k in header for k in section_keywords):
                continue
            for line in body.splitlines():
                line = line.strip()
                if not line.startswith("-"):
                    continue
                item_match = re.match(r"-\s*\d+\.\s*\([^)]+\)\s*(.+?)(?:\s*\[p\d+\])?$", line, re.IGNORECASE)
                if not item_match:
                    continue
                item = _clean_name(item_match.group(1))
                if not item:
                    continue

                pair = re.match(r"^([A-Z][A-Za-z'\-]+)\s*&\s*([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){1,2})", item)
                if pair:
                    context = self._get_context(pair, item)
                    right_name = _clean_name(pair.group(2).split("-", 1)[0])
                    _append_person(right_name, context, f"Detected section speaker ({header})", speaker_section=header)
                    right_tokens = _word_tokens(right_name)
                    if len(right_tokens) >= 2:
                        inferred_first = _clean_name(pair.group(1))
                        inferred_last = right_tokens[-1]
                        inferred = f"{inferred_first} {inferred_last}"
                        _append_person(
                            inferred,
                            context,
                            f"Detected section speaker ({header})",
                            pair_inferred=(inferred_first, inferred_last),
                            speaker_section=header,
                        )
                    continue

                lead = re.match(
                    r"^([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){1,2})(?=\s*(?:,|[-–—]|$))",
                    item,
                )
                if lead:
                    context = self._get_context(lead, item)
                    _append_person(lead.group(1), context, f"Detected section speaker ({header})", speaker_section=header)

        if HIGH_RECALL:
            fuzzy_title_patterns = [
                r"(?:MAYOR|MAYO|CLERK|MANAGER|ATTORNEY|CHIEF)\W+([A-Z][A-Za-z'\-]{2,}(?:\s+[A-Z][A-Za-z'\-]{2,}){1,2})",
            ]
            for pattern in fuzzy_title_patterns:
                for m in re.finditer(pattern, source_text, re.IGNORECASE):
                    name = _clean_name(m.group(1).strip(" ,.;:"))
                    context = self._get_context(m, source_text)
                    found.append(
                        {
                            "category": "PEOPLE",
                            "canonical_name": name,
                            "fact_key": "POTENTIAL_IDENTITY",
                            "fact_value": {"context": context, "ocr_tolerant": True},
                            "evidence": f"OCR-tolerant person detected in context: {context}",
                        }
                    )

        # 5) Locations
        for addr in _extract_street_addresses(source_text):
            m = re.search(re.escape(addr), source_text, re.IGNORECASE)
            if not m:
                continue
            context = self._get_context(m, source_text)
            found.append(
                {
                    "category": "LOCATION",
                    "canonical_name": addr,
                    "fact_key": "PROPERTY_STREET_ADDRESS",
                    "fact_value": {"full": addr, "context": context},
                    "evidence": f"Detected address in context: {context}",
                }
            )

        # 6) Metadata location hint
        if meeting_meta:
            loc = _normalize_meeting_location(str(meeting_meta.get("location") or ""))
            if loc and loc.upper() not in {"UNKNOWN", "N/A"}:
                found.append(
                    {
                        "category": "LOCATION",
                        "canonical_name": loc,
                        "fact_key": "MEETING_LOCATION",
                        "fact_value": {"source": "suggestion_box"},
                        "evidence": f"Meeting location from metadata: {loc}",
                    }
                )

        # 7) Dedup + coherence filter
        # Key: (category, dedup_name, fact_key) so multiple fact_keys per entity
        # survive, and PEOPLE middle names/initials don't create phantom duplicates.
        unique: dict[tuple[str, str, str], dict[str, Any]] = {}
        guard_text = validation_text if validation_text else source_text
        for ent in found:
            category = str(ent.get("category") or "").upper()
            canonical_name = _clean_name(str(ent.get("canonical_name") or ""))
            fact_key = str(ent.get("fact_key") or "").upper()
            if category == "LOCATION":
                canonical_name = _normalize_location_canonical(canonical_name, fact_key)
            canonical_name = _strip_context_prefix(canonical_name, category)
            ent["category"] = category
            ent["canonical_name"] = canonical_name
            accepted, reasons = self._accept(ent, guard_text)
            if not accepted:
                rejected_log.append(
                    _rejection_entry(
                        source_id=source_id,
                        category=category,
                        canonical_name=canonical_name,
                        reasons=reasons,
                        evidence=str(ent.get("evidence") or ""),
                    )
                )
                continue
            dedup_name = _person_dedup_key(canonical_name) if category == "PEOPLE" else canonical_name.upper()
            key = (category, dedup_name, fact_key)
            existing = unique.get(key)
            if existing is None:
                unique[key] = ent
            elif category == "PEOPLE" and len(canonical_name) > len(str(existing.get("canonical_name") or "")):
                unique[key] = ent

        return list(unique.values()), rejected_log


def _compact_fact_value(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(v, str):
                limit = MAX_FACT_CONTEXT_LEN if str(k).lower() == "context" else 140
                out[k] = _clip(v, limit)
            else:
                out[k] = _compact_fact_value(v)
        return out
    if isinstance(value, list):
        return [_compact_fact_value(v) for v in value]
    if isinstance(value, str):
        return _clip(value, 140)
    return value


def generate_id(category: str, name: str) -> str:
    normalized = _normalize_ws(name)
    safe = re.sub(r"[^a-zA-Z0-9]", "_", normalized.upper()).strip("_")
    safe = re.sub(r"_+", "_", safe)
    return f"{category.upper()}_{safe}"


def parse_entities(file_path: str) -> tuple[str, str | None, list[dict[str, Any]]]:
    scaffold_path = Path(file_path)
    content = scaffold_path.read_text(encoding="utf-8")
    hoover = OntologicalHoover(GOLDEN_ONTOLOGY_PATH)

    meta = extract_suggestion_meta(content)
    source_id = meta.get("artifact_machine_code", meta.get("source_machine_code", "UNKNOWN"))
    meeting_date_raw = str(meta.get("meeting_date") or "").strip()
    meeting_date: str | None = None
    if meeting_date_raw and meeting_date_raw.upper() not in {"UNKNOWN", "N/A", "NONE", "NULL"}:
        try:
            meeting_date = datetime.strptime(meeting_date_raw, "%B %d, %Y").date().isoformat()
        except Exception:
            meeting_date = None

    agenda_text = extract_agenda_scope(content)
    full_source_text = extract_full_page_text_from_mode(scaffold_path)
    extraction_text = (
        _normalize_ws("\n".join([agenda_text or "", full_source_text or ""]))
        if HIGH_RECALL
        else agenda_text
    )
    entities, rejected = hoover.hoover(
        extraction_text,
        meeting_meta=meta,
        source_id=source_id,
        validation_text=full_source_text or agenda_text,
    )
    log_rejections(rejected)

    return source_id, meeting_date, entities


def load_to_registry(source_id: str, meeting_date: str | None, entities: list[dict[str, Any]]) -> None:
    try:
        conn = psycopg2.connect(
            host=PG_HOST,
            port=PG_PORT,
            database=PG_DB,
            user=PG_USER,
            password=PG_PASS,
        )
        cur = conn.cursor()

        for ent in entities:
            registry_id = generate_id(str(ent["category"]), str(ent["canonical_name"]))
            print(f"Loading {ent['category']}: {ent['canonical_name']} -> {registry_id}")

            cur.execute(
                """
                INSERT INTO cco.registry (registry_id, category, canonical_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (registry_id) DO UPDATE SET updated_at = CURRENT_TIMESTAMP;
                """,
                (registry_id, ent["category"], ent["canonical_name"]),
            )

            cur.execute(
                """
                INSERT INTO cco.identities (registry_id, alias_name, source_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (registry_id, alias_name) DO NOTHING;
                """,
                (registry_id, ent["canonical_name"], source_id),
            )

            cur.execute(
                """
                SELECT 1
                FROM cco.observations
                WHERE registry_id = %s AND source_id = %s AND fact_key = %s
                LIMIT 1
                """,
                (registry_id, source_id, ent["fact_key"]),
            )
            if cur.fetchone() is None:
                compact_fact_value = _compact_fact_value(ent["fact_value"])
                compact_evidence = _clip(str(ent["evidence"]), MAX_EVIDENCE_LEN)
                cur.execute(
                    """
                    INSERT INTO cco.observations (registry_id, fact_key, fact_value, source_id, evidence, effective_date)
                    VALUES (%s, %s, %s, %s, %s, %s);
                    """,
                    (
                        registry_id,
                        ent["fact_key"],
                        json.dumps(compact_fact_value),
                        source_id,
                        compact_evidence,
                        meeting_date,
                    ),
                )

        conn.commit()
        print(f"Registry Hoovering Complete. Total Entities: {len(entities)}")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Registry Loading Error: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python registry_loader.py <path_to_scaffold.md>")
    else:
        file_path = sys.argv[1]
        source_id, meeting_date, entities = parse_entities(file_path)
        if entities:
            load_to_registry(source_id, meeting_date, entities)
        else:
            print("No entities detected to load.")
