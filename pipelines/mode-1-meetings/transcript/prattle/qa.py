import argparse
import hashlib
import json
import re
import shutil
from collections import Counter
from datetime import datetime
from html import unescape
from pathlib import Path

# PRATTLE Stage 4: QA
# Mission: Final structural validation, integrity gating, glossary candidate emission,
# and delivery to _output.

OUTPUT_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Transcripts\_output")
TRANSCRIPTS_ROOT = OUTPUT_ROOT.parent
GLOSSARY_OUTPUT_ROOT = OUTPUT_ROOT / "_glossary"
GLOSSARY_MANIFEST_FILE = TRANSCRIPTS_ROOT / "M1_TS_GLOSSARY_HOVER_MANIFEST.jsonl"
GLOSSARY_STATE_FILE = TRANSCRIPTS_ROOT / "transcript_glossary_hover_state.json"

SCHEMA_VERSION = "m1.transcript.glossary_hover.v1"
SOURCE_LANE = "transcript_output_glossary_hover"
JURISDICTION = "Richlands"

MIN_SQUEEZED_COVERAGE_RATIO = 0.95
MIN_TURNS = 5

STOP_WORDS = {
    "and", "the", "this", "that", "you", "uh", "please", "yes", "no", "aye", "vote",
    "be", "it", "say", "right", "they", "have", "hear", "motion", "can", "questions",
    "get", "now", "up", "else", "okay", "been", "set", "us", "changes", "he", "time",
    "evening", "one", "two", "three", "four", "being", "said", "or",
}

PLACEHOLDER_NAMES = {
    "UNKNOWN",
    "CHAIR_INCUMBENT",
    "CLERK_INCUMBENT",
    "SECONDER_UNKNOWN",
    "MOVER_UNKNOWN",
    "VOTER_UNKNOWN",
    "UNKNOWN_CITIZEN",
    "SPEAKER_UNKNOWN",
    "PUBLIC_COMMENTER",
    "PLEDGE_GROUP",
    "INVOCATION_SPEAKER",
    "MEMBER_PROCEDURAL",
}

ORG_PATTERNS = [
    re.compile(r"\bTown of Richlands\b", re.IGNORECASE),
    re.compile(r"\bRichlands Town Council\b", re.IGNORECASE),
    re.compile(r"\bTown Council\b", re.IGNORECASE),
    re.compile(r"\bPlanning Commission\b", re.IGNORECASE),
    re.compile(r"\bTown Manager(?:'s Office)?\b", re.IGNORECASE),
    re.compile(r"\bTown Clerk(?:'s Office)?\b", re.IGNORECASE),
    re.compile(r"\bTown Attorney(?:'s Office)?\b", re.IGNORECASE),
    re.compile(r"\bVCEDA\b", re.IGNORECASE),
    re.compile(r"\bPSA\b", re.IGNORECASE),
    re.compile(r"\bPCA\b", re.IGNORECASE),
]

LEGAL_PATTERNS = [
    re.compile(r"\bVirginia Code(?:\s*§?\s*[0-9A-Za-z\.\-:]+)?\b", re.IGNORECASE),
]


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def tokenize_words(text: str) -> list[str]:
    return re.findall(r"\w+", (text or "").lower())


def word_count(text: str) -> int:
    return len(tokenize_words(text))


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def clip(text: str, limit: int = 220) -> str:
    t = normalize_ws(text)
    if len(t) <= limit:
        return t
    return t[: max(1, limit - 1)].rstrip() + "…"


def load_glossary_state() -> dict:
    if not GLOSSARY_STATE_FILE.exists():
        return {"records": {}}
    try:
        return json.loads(GLOSSARY_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"records": {}}


def save_glossary_state(state: dict) -> None:
    TRANSCRIPTS_ROOT.mkdir(parents=True, exist_ok=True)
    GLOSSARY_STATE_FILE.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def load_manifest_codes() -> set[str]:
    codes: set[str] = set()
    if not GLOSSARY_MANIFEST_FILE.exists():
        return codes
    for line in GLOSSARY_MANIFEST_FILE.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            continue
        code = str(row.get("machine_code") or "").strip()
        if code:
            codes.add(code)
    return codes


def append_manifest_row(row: dict) -> None:
    TRANSCRIPTS_ROOT.mkdir(parents=True, exist_ok=True)
    with GLOSSARY_MANIFEST_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=True) + "\n")


class QA:
    def __init__(self, staging_path: Path):
        self.staging_path = staging_path
        self.quoted_file = staging_path / "quoted.json"
        self.source_vtt = staging_path / "source.vtt"

        self.data: dict = {}
        self.source_text_squeezed = ""
        self.metrics = {
            "total_turns": 0,
            "resolved_turns": 0,
            "unknown_turns": 0,
            "unknown_ratio": None,
            "source_words_squeezed": 0,
            "output_words": 0,
            "squeezed_coverage_ratio": None,
            "coverage_gate_threshold": MIN_SQUEEZED_COVERAGE_RATIO,
            "disposition_code": None,
            "machine_code_with_disposition": None,
            "structural_issues": [],
        }

    def load_data(self) -> bool:
        if not self.quoted_file.exists():
            return False
        self.data = json.loads(self.quoted_file.read_text(encoding="utf-8"))
        return True

    def parse_vtt_with_timestamps(self, raw_content: str) -> list[dict]:
        entries: list[dict] = []
        current_time = None

        for line in raw_content.splitlines():
            line_unescaped = unescape(line)
            if "-->" in line_unescaped:
                match = re.search(r"(?:(\d{1,2}):)?(\d{2}):(\d{2}\.\d{3})", line_unescaped)
                if match:
                    h_part, m_part, s_part = match.groups()
                    hours = int(h_part) if h_part is not None else 0
                    minutes = int(m_part)
                    seconds = float(s_part)
                    current_time = hours * 3600 + minutes * 60 + seconds
            elif (
                current_time is not None
                and line_unescaped.strip()
                and not line_unescaped.startswith("WEBVTT")
                and not line_unescaped.startswith("Kind:")
                and not line_unescaped.startswith("Language:")
            ):
                text = re.sub(r"<[^>]+>", "", line_unescaped).strip()
                if text:
                    entries.append({"ts": current_time, "text": text})
        return entries

    def merge_overlapping_text(self, parts: list[str]) -> list[str]:
        if not parts:
            return []

        merged = [parts[0]]
        for i in range(1, len(parts)):
            prev = merged[-1]
            curr = parts[i]

            prev_words = prev.split()
            curr_words = curr.split()
            prev_lower = [w.lower().strip(".,!?:;") for w in prev_words]
            curr_lower = [w.lower().strip(".,!?:;") for w in curr_words]

            max_overlap = 0
            search_limit = min(len(prev_lower), len(curr_lower))
            for length in range(1, search_limit + 1):
                if prev_lower[-length:] == curr_lower[:length]:
                    max_overlap = length

            if max_overlap > 0:
                new_part_words = curr_words[max_overlap:]
                if new_part_words:
                    merged[-1] += " " + " ".join(new_part_words)
            else:
                curr_str_clean = " ".join(curr_lower)
                prev_str_clean = " ".join(prev_lower)
                if curr_str_clean and curr_str_clean in prev_str_clean:
                    continue
                merged.append(curr)
        return merged

    def build_squeezed_source_text(self) -> str:
        if not self.source_vtt.exists():
            return ""

        raw_content = self.source_vtt.read_text(encoding="utf-8", errors="replace")
        entries = self.parse_vtt_with_timestamps(raw_content)
        if not entries:
            return ""

        parts: list[str] = []
        for i, entry in enumerate(entries):
            text = entry["text"]
            if i > 0:
                gap = entry["ts"] - entries[i - 1]["ts"]
                if gap > 1.5 and parts and not parts[-1].endswith((".", "!", "?")):
                    parts[-1] += "."
            parts.append(text)

        squeezed_parts = self.merge_overlapping_text(parts)
        squeezed_text = normalize_ws(unescape(" ".join(squeezed_parts)))

        # Speaker markers are structural caption artifacts and should not count as content loss.
        squeezed_text = normalize_ws(squeezed_text.replace(">>", " ").replace("&gt;&gt;", " "))
        return squeezed_text

    def build_output_text(self) -> str:
        turns = self.data.get("turns", [])
        return normalize_ws(" ".join(str((t or {}).get("text", "")) for t in turns if isinstance(t, dict)))

    def find_evidence(self, text: str, needle: str) -> str:
        if not needle:
            return ""
        pattern = re.compile(re.escape(needle), re.IGNORECASE)
        m = pattern.search(text)
        if not m:
            return needle
        start = max(0, m.start() - 80)
        end = min(len(text), m.end() + 80)
        return clip(text[start:end], 260)

    def clean_person_name(self, name: str) -> str:
        s = normalize_ws(name)
        s = s.strip(" ,;:.|-")
        return normalize_ws(s)

    def is_noise_name(self, name: str) -> bool:
        if not name:
            return True
        up = name.upper()
        low = name.lower()
        if up in PLACEHOLDER_NAMES:
            return True
        if low in STOP_WORDS:
            return True
        if len(low) < 3:
            return True
        if not re.match(r"^[A-Za-z][A-Za-z'\- ]*$", name):
            return True
        return False

    def extract_glossary_entities(self, transcript_text: str) -> list[dict]:
        entities: list[dict] = []
        seen: set[tuple[str, str]] = set()

        def add_entity(category: str, canonical_name: str, confidence: float, match_type: str):
            cname = self.clean_person_name(canonical_name)
            if not cname:
                return
            key = (category.upper(), cname.lower())
            if key in seen:
                return
            seen.add(key)

            evidence = self.find_evidence(transcript_text, cname)
            qa_flags: list[str] = []
            if len(cname.split()) == 1:
                qa_flags.append("single_token_name")

            entities.append(
                {
                    "entity_id": f"GE{len(entities) + 1:03d}",
                    "category": category.upper(),
                    "canonical_name": cname,
                    "fact_key": f"{category.upper()}::{cname.lower().replace(' ', '_')}",
                    "confidence": round(float(confidence), 3),
                    "match_type": match_type,
                    "evidence": evidence,
                    "evidence_sha256": sha256_text(evidence),
                    "qa_flags": qa_flags,
                }
            )

        roster = self.data.get("roster") or {}
        for group in ("members", "others"):
            group_obj = roster.get(group) or {}
            for name, meta in group_obj.items():
                cname = self.clean_person_name(str(name))
                if self.is_noise_name(cname):
                    continue
                conf = float((meta or {}).get("confidence", 0.7))
                add_entity("PERSON", cname, conf, "roster_seed")

        for turn in self.data.get("turns", []):
            if not isinstance(turn, dict):
                continue
            sp = turn.get("speaker") or {}
            raw_name = self.clean_person_name(str(sp.get("name", "")))
            if self.is_noise_name(raw_name):
                continue
            conf = float(sp.get("confidence", 0.65))
            if conf < 0.65:
                continue
            add_entity("PERSON", raw_name, conf, "speaker_tag")

        for pattern in ORG_PATTERNS:
            for m in pattern.finditer(transcript_text):
                add_entity("ORGANIZATION", m.group(0), 0.7, "regex_scan")

        for pattern in LEGAL_PATTERNS:
            for m in pattern.finditer(transcript_text):
                add_entity("LEGAL_REFERENCE", m.group(0), 0.7, "regex_scan")

        # Stable ordering by category then name for deterministic outputs.
        entities.sort(key=lambda x: (x["category"], x["canonical_name"].lower()))
        for i, ent in enumerate(entities, start=1):
            ent["entity_id"] = f"GE{i:03d}"
        return entities

    def build_glossary_payload(
        self,
        machine_code: str,
        source_transcript_path: Path,
        source_transcript_sha256: str,
        transcript_text: str,
        entities: list[dict],
    ) -> dict:
        by_category = dict(Counter(ent["category"] for ent in entities))

        return {
            "schema_version": SCHEMA_VERSION,
            "record_type": "transcript_glossary_candidates_record",
            "prepared_at": datetime.now().isoformat(timespec="seconds"),
            "source_lane": SOURCE_LANE,
            "jurisdiction": JURISDICTION,
            "machine_code": machine_code,
            "artifact_machine_code": machine_code,
            "lineage": {
                "source_transcript_json_path": str(source_transcript_path),
                "source_transcript_sha256": source_transcript_sha256,
                "source_vtt_staging_path": str(self.source_vtt),
                "source_vtt_exists": self.source_vtt.exists(),
            },
            "glossary_hover_summary": {
                "entities_total": len(entities),
                "entities_by_category": by_category,
                "source_text_chars": len(transcript_text),
                "source_turns_count": int(self.metrics["total_turns"]),
            },
            "glossary_entities": entities,
            "qa": {
                "is_candidate_output_only": True,
                "notes": "Extraction-only glossary hover output for transcript lane.",
            },
        }

    def render_glossary_summary(self, payload: dict) -> str:
        lines = [
            f"MACHINE_CODE: {payload.get('machine_code', '')}",
            f"SCHEMA_VERSION: {payload.get('schema_version', '')}",
            f"ENTITIES_TOTAL: {payload.get('glossary_hover_summary', {}).get('entities_total', 0)}",
            "ENTITIES_BY_CATEGORY:",
        ]
        by_cat = payload.get("glossary_hover_summary", {}).get("entities_by_category", {}) or {}
        for cat in sorted(by_cat.keys()):
            lines.append(f"  - {cat}: {by_cat[cat]}")
        lines.append("")
        lines.append("TOP_ENTITIES:")
        for ent in (payload.get("glossary_entities", []) or [])[:30]:
            lines.append(
                f"  - [{ent.get('category','')}] {ent.get('canonical_name','')} "
                f"(conf={ent.get('confidence',0):.2f}, match={ent.get('match_type','')})"
            )
        return "\n".join(lines).strip() + "\n"

    def write_glossary_artifacts(self, machine_code: str, payload: dict, source_transcript_sha256: str) -> tuple[Path, Path]:
        GLOSSARY_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        glossary_json = GLOSSARY_OUTPUT_ROOT / f"{machine_code}.glossary_hover.json"
        glossary_txt = GLOSSARY_OUTPUT_ROOT / f"{machine_code}.glossary_hover.txt"

        payload_text = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
        payload_sha256 = sha256_text(payload_text)
        glossary_json.write_text(payload_text, encoding="utf-8")
        glossary_txt.write_text(self.render_glossary_summary(payload), encoding="utf-8")

        manifest_codes = load_manifest_codes()
        manifest_row = {
            "machine_code": machine_code,
            "prepared_at": datetime.now().isoformat(timespec="seconds"),
            "schema_version": SCHEMA_VERSION,
            "record_type": "transcript_glossary_candidates_record",
            "source_lane": SOURCE_LANE,
            "entities_total": len(payload.get("glossary_entities", [])),
            "source_transcript_sha256": source_transcript_sha256,
            "payload_sha256": payload_sha256,
            "output_json": str(glossary_json),
            "output_txt": str(glossary_txt),
        }
        if machine_code not in manifest_codes:
            append_manifest_row(manifest_row)

        state = load_glossary_state()
        records = state.setdefault("records", {})
        records[machine_code] = {
            "last_status": "prepared",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "schema_version": SCHEMA_VERSION,
            "source_transcript_sha256": source_transcript_sha256,
            "payload_sha256": payload_sha256,
            "output_json": str(glossary_json),
            "output_txt": str(glossary_txt),
            "entities_total": len(payload.get("glossary_entities", [])),
        }
        save_glossary_state(state)
        return glossary_json, glossary_txt

    def validate_structure(self) -> bool:
        turns = self.data.get("turns", [])
        self.metrics["total_turns"] = len(turns)

        unknowns = 0
        for turn in turns:
            speaker = (turn or {}).get("speaker") or {}
            role = str(speaker.get("role", "")).upper()
            name = str(speaker.get("name", "")).upper()
            if role == "UNKNOWN" or name == "UNKNOWN":
                unknowns += 1

        self.metrics["unknown_turns"] = unknowns
        self.metrics["resolved_turns"] = self.data.get("quoter_metrics", {}).get("resolved_count", 0)
        self.metrics["unknown_ratio"] = (
            round(unknowns / self.metrics["total_turns"], 3) if self.metrics["total_turns"] > 0 else 1.0
        )

        self.source_text_squeezed = self.build_squeezed_source_text()
        output_text = self.build_output_text()
        source_words = word_count(self.source_text_squeezed)
        output_words = word_count(output_text)

        self.metrics["source_words_squeezed"] = source_words
        self.metrics["output_words"] = output_words
        self.metrics["squeezed_coverage_ratio"] = (
            round(output_words / source_words, 3) if source_words > 0 else None
        )

        issues = self.metrics["structural_issues"]
        if source_words == 0:
            issues.append("empty_or_unparseable_source_vtt")
        if self.metrics["total_turns"] < MIN_TURNS:
            issues.append("insufficient_turns")
        if self.metrics["total_turns"] > 0 and self.metrics["unknown_ratio"] > 0.98:
            issues.append("unknown_saturation")
        if source_words > 0 and (output_words / source_words) < MIN_SQUEEZED_COVERAGE_RATIO:
            issues.append("low_squeezed_coverage")

        return len(issues) == 0

    def deliver(self) -> bool:
        machine_code = self.data.get("machine_code")
        if not machine_code:
            print("    Error: No machine code found in data.")
            return False

        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

        # Attach QA metrics to final data before persistence.
        self.data["qa_metrics"] = self.metrics
        self.data["completed_at"] = datetime.now().isoformat()

        final_output = OUTPUT_ROOT / f"{machine_code}.json"
        self.metrics["disposition_code"] = "OK95"
        self.metrics["machine_code_with_disposition"] = f"{machine_code}.OK95"
        final_output.write_text(json.dumps(self.data, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

        transcript_payload_text = final_output.read_text(encoding="utf-8")
        transcript_payload_sha256 = sha256_text(transcript_payload_text)
        transcript_text = self.build_output_text()
        entities = self.extract_glossary_entities(transcript_text)
        glossary_payload = self.build_glossary_payload(
            machine_code=machine_code,
            source_transcript_path=final_output,
            source_transcript_sha256=transcript_payload_sha256,
            transcript_text=transcript_text,
            entities=entities,
        )
        glossary_json, glossary_txt = self.write_glossary_artifacts(
            machine_code=machine_code,
            payload=glossary_payload,
            source_transcript_sha256=transcript_payload_sha256,
        )

        self.data["glossary_hover"] = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "transcript_glossary_candidates_record",
            "entities_total": len(entities),
            "output_json": str(glossary_json),
            "output_txt": str(glossary_txt),
        }
        final_output.write_text(json.dumps(self.data, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

        print(f"    [DELIVERY] Final transcript pushed to _output: {final_output.name}")
        print(f"    [DELIVERY] Glossary hover artifact: {glossary_json.name}")

        # INDUSTRIAL RULE: Wipe staging area after successful delivery.
        print(f"    [CLEANUP] Wiping staging area: {self.staging_path}")
        shutil.rmtree(self.staging_path)
        return True

    def process(self) -> bool:
        if not self.load_data():
            print(f"    Error: {self.quoted_file} not found.")
            return False

        print(f">>> [QA] Performing final validation for {self.data.get('machine_code')}...")
        if self.validate_structure():
            return self.deliver()

        print(f"    [QA] Validation failed. Delivery aborted. Issues: {self.metrics['structural_issues']}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging", required=True, help="Path to staging machine-code folder")
    args = parser.parse_args()

    qa_engine = QA(Path(args.staging))
    ok = qa_engine.process()
    raise SystemExit(0 if ok else 1)
