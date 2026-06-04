#!/usr/bin/env python
"""
Minutes OCR (Approved-PDF Lane)

Transforms approved minutes PDFs from `_vaulted` into normalized Minutes schema
records in `_output`.

Strict invariant:
  - OCR/normalize only
  - No DB writes
  - No glossary writes
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Sequence


MINUTES_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Minutes"
VAULT_ROOT = MINUTES_ROOT / "_vaulted"
OUTPUT_ROOT = MINUTES_ROOT / "_output"
RUNS_ROOT = OUTPUT_ROOT / "_runs"

APPROVED_MANIFEST_FILE = MINUTES_ROOT / "M1_MINUTES_APPROVED_PULL_MANIFEST.jsonl"
STATE_FILE = MINUTES_ROOT / "minutes_ocr_state.json"
MANIFEST_FILE = MINUTES_ROOT / "M1_MINUTES_OCR_MANIFEST.jsonl"

SCHEMA_VERSION = "m1.minutes.preparse.v1"
SOURCE_LANE = "approved_minutes_pdf_ocr"
JURISDICTION = "Richlands"
DATE_PAST_YEAR_WINDOW = 5
DATE_FUTURE_YEAR_WINDOW = 2
DATE_FALLBACK_MIN_YEAR = 1990

MINUTES_CODE_RE = re.compile(r"^M1\.MN\.(\d{6})\.(\d{8})\.(\d{8})$", re.IGNORECASE)
MINUTES_APPROVAL_RE = re.compile(r"\b(approve|approval|approved|adopt)\b.*\bminutes?\b", re.IGNORECASE)

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
        return {"records": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"records": {}}


def save_state(state: dict) -> None:
    MINUTES_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def append_manifest_rows(rows: Sequence[dict]) -> None:
    if not rows:
        return
    MINUTES_ROOT.mkdir(parents=True, exist_ok=True)
    with MANIFEST_FILE.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")


def load_manifest_codes() -> set[str]:
    codes: set[str] = set()
    if not MANIFEST_FILE.exists():
        return codes
    for line in MANIFEST_FILE.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            continue
        code = str(row.get("minutes_code") or "").strip()
        if code:
            codes.add(code)
    return codes


def load_approved_manifest_map() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not APPROVED_MANIFEST_FILE.exists():
        return out
    for line in APPROVED_MANIFEST_FILE.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            continue
        code = str(row.get("machine_code") or "").strip()
        if code:
            out[code] = row
    return out


def parse_minutes_filename(pdf_path: Path) -> tuple[str, str, str, str] | None:
    name = pdf_path.stem
    m = MINUTES_CODE_RE.match(name)
    if not m:
        return None
    return name, m.group(1), m.group(2), m.group(3)


def ymd_to_iso(ymd: str) -> str | None:
    if not re.fullmatch(r"\d{8}", ymd):
        return None
    try:
        return datetime.strptime(ymd, "%Y%m%d").date().isoformat()
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


def is_plausible_mention_year(year: int, year_hint: int | None) -> bool:
    if year_hint is not None:
        return (year_hint - DATE_PAST_YEAR_WINDOW) <= year <= (year_hint + DATE_FUTURE_YEAR_WINDOW)
    current_year = datetime.now().year
    return DATE_FALLBACK_MIN_YEAR <= year <= (current_year + DATE_FUTURE_YEAR_WINDOW)


def extract_date_mentions(text: str, year_hint: int | None = None) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    lower = text.lower()

    month_names = "|".join(sorted(MONTH_MAP.keys(), key=len, reverse=True))
    for match in re.finditer(
        rf"\b({month_names})\b[\s._,-]*(\d{{1,2}})(?:st|nd|rd|th)?(?:[\s._,-]+(\d{{2,4}}))?",
        lower,
    ):
        raw = text[match.start() : match.end()]
        month = MONTH_MAP[match.group(1)]
        day = int(match.group(2))
        year_token = match.group(3)
        year = normalize_year(year_token, year_hint) if year_token else year_hint
        if year is None:
            continue
        if not is_plausible_mention_year(year, year_hint):
            continue
        iso_date = ymd_to_iso(f"{year:04d}{month:02d}{day:02d}")
        if not iso_date:
            continue
        key = f"{iso_date}|{raw.strip().lower()}"
        if key in seen:
            continue
        seen.add(key)
        found.append({"iso_date": iso_date, "raw": raw.strip()})

    for match in re.finditer(r"(?<!\d)(\d{1,2})[.\-_/ ]+(\d{1,2})[.\-_/ ]+(\d{2,4})(?!\d)", lower):
        raw = text[match.start() : match.end()]
        month = int(match.group(1))
        day = int(match.group(2))
        year = normalize_year(match.group(3), year_hint)
        if not is_plausible_mention_year(year, year_hint):
            continue
        iso_date = ymd_to_iso(f"{year:04d}{month:02d}{day:02d}")
        if not iso_date:
            continue
        key = f"{iso_date}|{raw.strip().lower()}"
        if key in seen:
            continue
        seen.add(key)
        found.append({"iso_date": iso_date, "raw": raw.strip()})

    found.sort(key=lambda d: (d["iso_date"], d["raw"]))
    return found


def infer_meeting_type(seed_text: str) -> str | None:
    upper = seed_text.upper()
    if "PUBLIC HEARING" in upper:
        return "PUBLIC_HEARING"
    if "SPECIAL MEETING" in upper or "SPECIAL CALL" in upper:
        return "SPECIAL_MEETING"
    if "REGULAR MEETING" in upper or "REGULAR SCHEDULED" in upper:
        return "REGULAR_MEETING"
    if "WORKSHOP" in upper or "WORK SESSION" in upper or "BUDGET RETREAT" in upper:
        return "WORKSHOP"
    if "BUDGET" in upper:
        return "BUDGET_MEETING"
    return None


def resolve_tool(name: str, env_var: str, default_path: str | None = None) -> str | None:
    explicit = str(os.getenv(env_var, "")).strip()
    if explicit:
        p = Path(explicit)
        if p.exists():
            return str(p)
        return explicit
    if default_path:
        p = Path(default_path)
        if p.exists():
            return str(p)
    found = shutil.which(name)
    return found


def run_cmd(cmd: list[str], timeout_sec: int = 600) -> str:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"command_failed({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}")
    return proc.stdout


def get_page_count(pdfinfo_exe: str, pdf_path: Path) -> int:
    out = run_cmd([pdfinfo_exe, str(pdf_path)], timeout_sec=120)
    m = re.search(r"^Pages:\s+(\d+)\s*$", out, flags=re.MULTILINE)
    if not m:
        raise RuntimeError(f"Unable to parse page count from pdfinfo for {pdf_path}")
    return int(m.group(1))


def extract_native_pages(pdftotext_exe: str, pdf_path: Path, page_count: int, tmp_dir: Path) -> list[str]:
    out_path = tmp_dir / "native.txt"
    run_cmd([pdftotext_exe, "-layout", str(pdf_path), str(out_path)], timeout_sec=600)
    text = out_path.read_text(encoding="utf-8", errors="replace") if out_path.exists() else ""
    parts = text.split("\f")
    while parts and not parts[-1].strip():
        parts.pop()
    if len(parts) < page_count:
        parts += [""] * (page_count - len(parts))
    if len(parts) > page_count:
        parts = parts[:page_count]
    return parts


def ocr_one_page(
    pdftoppm_exe: str,
    tesseract_exe: str,
    pdf_path: Path,
    page_num: int,
    tmp_dir: Path,
    dpi: int,
    psm: int = 6,
) -> str:
    stem = tmp_dir / f"page_{page_num:04d}_dpi{dpi}"
    run_cmd(
        [
            pdftoppm_exe,
            "-f",
            str(page_num),
            "-l",
            str(page_num),
            "-singlefile",
            "-gray",
            "-r",
            str(dpi),
            "-png",
            str(pdf_path),
            str(stem),
        ],
        timeout_sec=600,
    )
    image_path = stem.with_suffix(".png")
    if not image_path.exists():
        raise RuntimeError(f"pdftoppm image missing for page {page_num}: {image_path}")
    out = run_cmd(
        [tesseract_exe, str(image_path), "stdout", "-l", "eng", "--oem", "1", "--psm", str(psm)],
        timeout_sec=600,
    )
    return out


def text_quality_metrics(text: str) -> dict:
    total = max(1, len(text))
    alpha = sum(ch.isalpha() for ch in text)
    weird = sum((ord(ch) > 126 and not ch.isspace()) for ch in text)
    whitespace = sum(ch.isspace() for ch in text)
    return {
        "char_count": len(text),
        "alpha_ratio": alpha / total,
        "weird_ratio": weird / total,
        "whitespace_ratio": whitespace / total,
    }


def text_quality_score(metrics: dict) -> float:
    # Score favored by substantive alphabetic content with low weird-char noise.
    chars = float(metrics.get("char_count") or 0.0)
    alpha_ratio = float(metrics.get("alpha_ratio") or 0.0)
    weird_ratio = float(metrics.get("weird_ratio") or 0.0)
    whitespace_ratio = float(metrics.get("whitespace_ratio") or 0.0)
    score = (alpha_ratio * 100.0) - (weird_ratio * 250.0) + min(chars, 6000.0) / 120.0
    if whitespace_ratio < 0.05:
        score -= 8.0
    return score


def needs_ocr_rescue(metrics: dict) -> bool:
    chars = int(metrics.get("char_count") or 0)
    alpha_ratio = float(metrics.get("alpha_ratio") or 0.0)
    weird_ratio = float(metrics.get("weird_ratio") or 0.0)
    return chars < 1500 or alpha_ratio < 0.45 or weird_ratio > 0.02


def ocr_best_page(
    pdftoppm_exe: str,
    tesseract_exe: str,
    pdf_path: Path,
    page_num: int,
    tmp_dir: Path,
    base_dpi: int,
) -> tuple[str, str, int]:
    attempts = [
        (max(base_dpi, 300), 6, "ocr_psm6"),
        (max(base_dpi, 400), 4, "ocr_psm4"),
        (max(base_dpi, 450), 11, "ocr_psm11"),
        # Orientation-aware fallback for sideways scans.
        (max(base_dpi, 450), 1, "ocr_psm1"),
    ]
    best_text = ""
    best_method = "ocr_psm6"
    best_score = -math.inf
    attempts_run = 0
    attempt_results: list[dict] = []

    first_metrics: dict | None = None
    for idx, (dpi, psm, method) in enumerate(attempts):
        if idx > 0 and first_metrics is not None and not needs_ocr_rescue(first_metrics):
            break
        text = ocr_one_page(
            pdftoppm_exe=pdftoppm_exe,
            tesseract_exe=tesseract_exe,
            pdf_path=pdf_path,
            page_num=page_num,
            tmp_dir=tmp_dir,
            dpi=dpi,
            psm=psm,
        ).strip()
        attempts_run += 1
        metrics = text_quality_metrics(text)
        if idx == 0:
            first_metrics = metrics
        score = text_quality_score(metrics)
        attempt_results.append(
            {
                "text": text,
                "method": f"{method}_dpi{dpi}",
                "metrics": metrics,
                "score": score,
            }
        )
        if score > best_score:
            best_score = score
            best_text = text
            best_method = f"{method}_dpi{dpi}"

    # Guardrail: when the "best score" is a very short snippet, but another
    # attempt has substantial body text, prefer the substantial text.
    if attempt_results:
        best_result = max(attempt_results, key=lambda r: float(r["score"]))
        max_chars_result = max(attempt_results, key=lambda r: int(r["metrics"]["char_count"]))
        best_chars = int(best_result["metrics"]["char_count"])
        max_chars = int(max_chars_result["metrics"]["char_count"])
        max_alpha = float(max_chars_result["metrics"]["alpha_ratio"])
        if best_chars < 250 and max_chars >= 700 and max_alpha >= 0.20:
            best_text = str(max_chars_result["text"])
            best_method = str(max_chars_result["method"])

    return best_text, best_method, attempts_run


def render_summary_text(payload: dict) -> str:
    header = [
        f"MINUTES_CODE: {payload['minutes_code']}",
        f"SOURCE_LANE: {payload['source_lane']}",
        f"MEETING_TYPE: {payload['meeting_context']['anchor_meeting_type']}",
        f"PAGES: {payload['ocr_summary']['total_pages']}",
        f"EXCERPTS: {payload['minutes_excerpt_summary']['excerpt_count']}",
        "",
    ]
    body: list[str] = []
    for ex in payload.get("minutes_excerpts", [])[:12]:
        snippet = ex["text"][:300].replace("\n", " ")
        body.append(
            f"[{ex['excerpt_id']}] page={ex['page_number']} source={ex['source_method']} "
            f"chars={len(ex['text'])} approval={ex['signals']['contains_approval_terms']}"
        )
        body.append(snippet)
        body.append("")
    if len(payload.get("minutes_excerpts", [])) > 12:
        body.append(f"... ({len(payload['minutes_excerpts']) - 12} more page excerpts)")
    return "\n".join(header + body).strip() + "\n"


def build_manifest_row_from_payload(
    run_id: str,
    minutes_code: str,
    payload: dict,
    payload_sha256: str,
    output_json: Path,
    output_txt: Path,
) -> dict:
    ocr_summary = payload.get("ocr_summary") or {}
    lineage = payload.get("lineage") or {}
    return {
        "run_id": run_id,
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "schema_version": SCHEMA_VERSION,
        "minutes_code": minutes_code,
        "source_pdf_path": str(lineage.get("source_pdf_path") or ""),
        "source_pdf_sha256": str(lineage.get("source_pdf_sha256") or ""),
        "payload_sha256": payload_sha256,
        "total_pages": int(ocr_summary.get("total_pages") or 0),
        "pages_with_text": int(ocr_summary.get("pages_with_text") or 0),
        "native_pages_used": int(ocr_summary.get("native_pages_used") or 0),
        "ocr_pages_used": int(ocr_summary.get("ocr_pages_used") or 0),
        "output_json": str(output_json),
        "output_txt": str(output_txt),
    }


def reconcile_existing_outputs(dry_run: bool = False) -> dict:
    run_id = datetime.now().strftime("RUN_%Y%m%dT%H%M%S")
    state = load_state()
    records = state.setdefault("records", {})
    manifest_codes = load_manifest_codes()
    manifest_additions: list[dict] = []

    scanned = 0
    repaired_state = 0
    repaired_manifest = 0
    invalid = 0

    for d in sorted(OUTPUT_ROOT.iterdir()) if OUTPUT_ROOT.exists() else []:
        if not d.is_dir() or not d.name.startswith("M1.MN."):
            continue
        minutes_code = d.name
        output_json = d / f"{minutes_code}.preparse.json"
        output_txt = d / f"{minutes_code}.preparse.txt"
        if not output_json.exists() or not output_txt.exists():
            continue
        scanned += 1
        try:
            text = output_json.read_text(encoding="utf-8")
            payload = json.loads(text)
        except Exception:
            invalid += 1
            continue

        payload_sha256 = sha256_text(text)
        row = build_manifest_row_from_payload(
            run_id=run_id,
            minutes_code=minutes_code,
            payload=payload,
            payload_sha256=payload_sha256,
            output_json=output_json,
            output_txt=output_txt,
        )

        rec = {
            "last_run_id": run_id,
            "last_status": "prepared",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "source_pdf_path": row["source_pdf_path"],
            "source_pdf_sha256": row["source_pdf_sha256"],
            "payload_sha256": payload_sha256,
            "output_json": str(output_json),
            "output_txt": str(output_txt),
        }
        existing = records.get(minutes_code)
        if existing != rec:
            records[minutes_code] = rec
            repaired_state += 1

        if minutes_code not in manifest_codes:
            manifest_codes.add(minutes_code)
            manifest_additions.append(row)
            repaired_manifest += 1

    if not dry_run:
        save_state(state)
        append_manifest_rows(manifest_additions)

        run_dir = RUNS_ROOT / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "run_id": run_id,
            "mode": "reconcile_existing_outputs",
            "scanned_output_records": scanned,
            "repaired_state_records": repaired_state,
            "added_manifest_rows": repaired_manifest,
            "invalid_payloads": invalid,
        }
        (run_dir / "minutes_ocr_reconcile_summary.json").write_text(
            json.dumps(summary, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )

    return {
        "run_id": run_id,
        "scanned_output_records": scanned,
        "repaired_state_records": repaired_state,
        "added_manifest_rows": repaired_manifest,
        "invalid_payloads": invalid,
        "dry_run": dry_run,
    }


def count_vaulted_minutes_pdfs() -> int:
    return len(list(VAULT_ROOT.glob("M1.MN.*.pdf")))


def count_ocr_state_records() -> int:
    state = load_state()
    return len(state.get("records", {}))


def scan_low_text_outputs(
    max_chars_per_page: float,
    min_total_chars: int,
    target_codes: set[str] | None = None,
) -> list[dict]:
    rows: list[dict] = []
    if not OUTPUT_ROOT.exists():
        return rows

    for d in sorted(OUTPUT_ROOT.iterdir()):
        if not d.is_dir() or not d.name.startswith("M1.MN."):
            continue
        minutes_code = d.name
        if target_codes is not None and minutes_code not in target_codes:
            continue

        output_json = d / f"{minutes_code}.preparse.json"
        if not output_json.exists():
            continue
        try:
            payload = json.loads(output_json.read_text(encoding="utf-8"))
        except Exception:
            continue

        ocr_summary = payload.get("ocr_summary") or {}
        total_pages = int(ocr_summary.get("total_pages") or 0)
        total_chars = int(ocr_summary.get("total_text_chars") or 0)
        pages_with_text = int(ocr_summary.get("pages_with_text") or 0)
        chars_per_page = (float(total_chars) / total_pages) if total_pages > 0 else 0.0

        is_low = chars_per_page < max_chars_per_page or total_chars < min_total_chars
        if not is_low:
            continue

        likely_empty = total_chars == 0 or (total_chars < 250 and pages_with_text <= 1)
        rows.append(
            {
                "minutes_code": minutes_code,
                "total_pages": total_pages,
                "pages_with_text": pages_with_text,
                "total_text_chars": total_chars,
                "chars_per_page": round(chars_per_page, 2),
                "likely_empty": likely_empty,
                "output_json": str(output_json),
            }
        )

    rows.sort(
        key=lambda r: (
            not r["likely_empty"],
            r["chars_per_page"],
            r["total_text_chars"],
            r["minutes_code"],
        )
    )
    return rows


def run_ocr(
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    native_page_min_chars: int = 80,
    ocr_dpi: int = 300,
    ocr_all_pages: bool = False,
    target_codes: set[str] | None = None,
) -> dict:
    run_id = datetime.now().strftime("RUN_%Y%m%dT%H%M%S")
    started_at = datetime.now().isoformat(timespec="seconds")

    pdftotext_exe = resolve_tool("pdftotext", "M1_MINUTES_PDFTOTEXT_EXE")
    pdftoppm_exe = resolve_tool("pdftoppm", "M1_MINUTES_PDFTOPPM_EXE")
    pdfinfo_exe = resolve_tool("pdfinfo", "M1_MINUTES_PDFINFO_EXE")
    tesseract_exe = resolve_tool(
        "tesseract",
        "M1_MINUTES_TESSERACT_EXE",
        default_path=r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    )

    if not pdftotext_exe or not pdftoppm_exe or not pdfinfo_exe or not tesseract_exe:
        missing = []
        if not pdftotext_exe:
            missing.append("pdftotext")
        if not pdftoppm_exe:
            missing.append("pdftoppm")
        if not pdfinfo_exe:
            missing.append("pdfinfo")
        if not tesseract_exe:
            missing.append("tesseract")
        raise RuntimeError(f"Missing required OCR tools: {', '.join(missing)}")

    approved_meta = load_approved_manifest_map()
    state = load_state()
    state_records = state.setdefault("records", {})

    candidates = []
    for pdf in sorted(VAULT_ROOT.glob("M1.MN.*.pdf")):
        parsed = parse_minutes_filename(pdf)
        if parsed and (target_codes is None or parsed[0] in target_codes):
            candidates.append((pdf, parsed))

    discovered = len(candidates)

    prepared = 0
    skipped_unchanged = 0
    failed = 0
    run_rows: list[dict] = []
    failure_rows: list[dict] = []
    manifest_codes = load_manifest_codes()

    if not dry_run:
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        run_dir = RUNS_ROOT / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        run_dir = RUNS_ROOT / run_id

    for pdf_path, parsed in candidates:
        if limit is not None and prepared >= limit:
            break
        minutes_code, _docnum, created_ymd, _pulled_ymd = parsed
        source_pdf_sha256 = sha256_file(pdf_path)

        output_dir = OUTPUT_ROOT / minutes_code
        output_json = output_dir / f"{minutes_code}.preparse.json"
        output_txt = output_dir / f"{minutes_code}.preparse.txt"

        prev = state_records.get(minutes_code, {})
        if (
            not force
            and prev.get("source_pdf_sha256") == source_pdf_sha256
            and output_json.exists()
        ):
            skipped_unchanged += 1
            continue

        if dry_run:
            prepared += 1
            continue

        try:
            page_count = get_page_count(pdfinfo_exe, pdf_path)
            with tempfile.TemporaryDirectory(prefix=f"{minutes_code}_") as td:
                tmp_dir = Path(td)
                native_pages = extract_native_pages(pdftotext_exe, pdf_path, page_count, tmp_dir)

                excerpts: list[dict] = []
                kind_counts: dict[str, int] = {}
                date_mentions_global: list[dict] = []
                date_keys: set[str] = set()
                has_approval_language = False
                native_used = 0
                ocr_used = 0
                native_chars = 0
                ocr_chars = 0
                ocr_rescue_pages = 0
                ocr_attempts_total = 0

                year_hint = int(created_ymd[:4]) if created_ymd else None
                for page_idx in range(page_count):
                    page_num = page_idx + 1
                    native_text = (native_pages[page_idx] or "").strip()
                    use_ocr = ocr_all_pages or len(native_text) < native_page_min_chars

                    if use_ocr:
                        text, source_method, attempts_run = ocr_best_page(
                            pdftoppm_exe=pdftoppm_exe,
                            tesseract_exe=tesseract_exe,
                            pdf_path=pdf_path,
                            page_num=page_num,
                            tmp_dir=tmp_dir,
                            base_dpi=ocr_dpi,
                        )
                        if attempts_run > 1:
                            ocr_rescue_pages += 1
                        ocr_attempts_total += attempts_run
                        ocr_used += 1
                        ocr_chars += len(text)
                    else:
                        text = native_text
                        source_method = "native_text"
                        native_used += 1
                        native_chars += len(text)

                    if not text:
                        continue

                    mentions = extract_date_mentions(text, year_hint=year_hint)
                    for dm in mentions:
                        key = f"{dm['iso_date']}|{dm['raw'].lower()}"
                        if key not in date_keys:
                            date_keys.add(key)
                            date_mentions_global.append(dm)

                    approval = bool(MINUTES_APPROVAL_RE.search(text))
                    if approval:
                        has_approval_language = True

                    kind = "minutes_page"
                    kind_counts[kind] = kind_counts.get(kind, 0) + 1

                    excerpts.append(
                        {
                            "excerpt_id": f"EX{len(excerpts)+1:03d}",
                            "kind": kind,
                            "page_number": page_num,
                            "start_line": 0,
                            "end_line": 0,
                            "source_method": source_method,
                            "text": text,
                            "text_sha256": sha256_text(text),
                            "signals": {
                                "contains_approval_terms": approval,
                                "date_mentions": mentions,
                            },
                        }
                    )

            date_mentions_global.sort(key=lambda d: (d["iso_date"], d["raw"]))
            full_text = "\n\n".join(ex["text"] for ex in excerpts)
            seed_text = (
                (approved_meta.get(minutes_code, {}).get("title") or "") + "\n" + full_text[:6000]
            )
            inferred_type = infer_meeting_type(seed_text)

            payload = {
                "schema_version": SCHEMA_VERSION,
                "record_type": "minutes_preparse_record",
                "prepared_at": datetime.now().isoformat(timespec="seconds"),
                "preparse_run_id": run_id,
                "source_lane": SOURCE_LANE,
                "jurisdiction": JURISDICTION,
                "minutes_code": minutes_code,
                "artifact_machine_code": minutes_code,
                "linked_source_pdf_code": minutes_code,
                "meeting_context": {
                    "anchor_meeting_date": ymd_to_iso(created_ymd),
                    "anchor_meeting_type": inferred_type,
                },
                "lineage": {
                    "source_pdf_path": str(pdf_path),
                    "source_pdf_sha256": source_pdf_sha256,
                    "source_pdf_file_name": pdf_path.name,
                    "source_title": approved_meta.get(minutes_code, {}).get("title"),
                    "source_url": approved_meta.get(minutes_code, {}).get("source_url"),
                    "tooling": {
                        "pdftotext_exe": pdftotext_exe,
                        "pdftoppm_exe": pdftoppm_exe,
                        "pdfinfo_exe": pdfinfo_exe,
                        "tesseract_exe": tesseract_exe,
                        "ocr_dpi": ocr_dpi,
                        "native_page_min_chars": native_page_min_chars,
                        "ocr_all_pages": ocr_all_pages,
                    },
                },
                "ocr_summary": {
                    "total_pages": page_count,
                    "pages_with_text": len(excerpts),
                    "native_pages_used": native_used,
                    "ocr_pages_used": ocr_used,
                    "ocr_rescue_pages": ocr_rescue_pages,
                    "ocr_attempts_total": ocr_attempts_total,
                    "native_text_chars": native_chars,
                    "ocr_text_chars": ocr_chars,
                    "total_text_chars": native_chars + ocr_chars,
                },
                "minutes_excerpt_summary": {
                    "excerpt_count": len(excerpts),
                    "excerpt_kind_counts": kind_counts,
                    "contains_approval_language": has_approval_language,
                    "date_mentions": date_mentions_global,
                },
                "minutes_excerpts": excerpts,
                "pusher_ready": {
                    "meeting_id": minutes_code,
                    "source_id": minutes_code,
                    "content_mode": "ocr_full_document",
                    "is_complete_minutes_document": True,
                    "glossary_scope_text_hint": "minutes_excerpts[].text",
                },
            }

            payload_text = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
            payload_sha256 = sha256_text(payload_text)

            if not dry_run:
                output_dir.mkdir(parents=True, exist_ok=True)
                output_json.write_text(payload_text, encoding="utf-8")
                output_txt.write_text(render_summary_text(payload), encoding="utf-8")

            prepared += 1
            row = build_manifest_row_from_payload(
                run_id=run_id,
                minutes_code=minutes_code,
                payload=payload,
                payload_sha256=payload_sha256,
                output_json=output_json,
                output_txt=output_txt,
            )
            run_rows.append(row)

            state_records[minutes_code] = {
                "last_run_id": run_id,
                "last_status": "prepared",
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "source_pdf_path": str(pdf_path),
                "source_pdf_sha256": source_pdf_sha256,
                "payload_sha256": payload_sha256,
                "output_json": str(output_json),
                "output_txt": str(output_txt),
            }

            # Crash-safe bookkeeping: flush per-record.
            save_state(state)
            if minutes_code not in manifest_codes:
                append_manifest_rows([row])
                manifest_codes.add(minutes_code)
        except Exception as exc:
            failed += 1
            failure_rows.append(
                {
                    "run_id": run_id,
                    "failed_at": datetime.now().isoformat(timespec="seconds"),
                    "source_pdf_path": str(pdf_path),
                    "minutes_code": minutes_code,
                    "error": str(exc),
                }
            )

    if not dry_run:
        run_manifest = run_dir / "minutes_ocr_manifest.jsonl"
        with run_manifest.open("w", encoding="utf-8") as f:
            for row in run_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

        if failure_rows:
            failure_out = run_dir / "minutes_ocr_failures.jsonl"
            with failure_out.open("w", encoding="utf-8") as f:
                for row in failure_rows:
                    f.write(json.dumps(row, ensure_ascii=True) + "\n")

        run_summary = {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "source_lane": SOURCE_LANE,
            "schema_version": SCHEMA_VERSION,
            "vault_root": str(VAULT_ROOT),
            "output_root": str(OUTPUT_ROOT),
            "discovered_pdfs": discovered,
            "prepared_records": prepared,
            "skipped_unchanged": skipped_unchanged,
            "failed": failed,
            "limit": limit,
            "force": force,
            "native_page_min_chars": native_page_min_chars,
            "ocr_dpi": ocr_dpi,
            "ocr_all_pages": ocr_all_pages,
            "target_codes_count": len(target_codes or ()),
        }
        (run_dir / "run_summary.json").write_text(json.dumps(run_summary, ensure_ascii=True, indent=2) + "\n")

        save_state(state)

    summary = {
        "run_id": run_id,
        "source_lane": SOURCE_LANE,
        "vault_root": str(VAULT_ROOT),
        "output_root": str(OUTPUT_ROOT),
        "discovered_pdfs": discovered,
        "prepared_records": prepared,
        "skipped_unchanged": skipped_unchanged,
        "failed": failed,
        "dry_run": dry_run,
    }

    print("=" * 60)
    print("MINUTES OCR SUMMARY")
    print(f"  Run ID: {summary['run_id']}")
    print(f"  Source lane: {summary['source_lane']}")
    print(f"  Vault root: {summary['vault_root']}")
    print(f"  Output root: {summary['output_root']}")
    print(f"  PDFs discovered: {summary['discovered_pdfs']}")
    print(f"  Prepared records: {summary['prepared_records']}")
    print(f"  Skipped (unchanged): {summary['skipped_unchanged']}")
    print(f"  Failed: {summary['failed']}")
    if dry_run:
        print("  Dry run: yes (no files written)")
    else:
        print(f"  Global manifest: {MANIFEST_FILE}")
        print(f"  State file: {STATE_FILE}")
        print(f"  Run artifacts: {run_dir}")
    print("=" * 60)
    return summary


def run_low_text_rerun(
    max_chars_per_page: float,
    min_total_chars: int,
    low_text_limit: int | None,
    dry_run: bool,
    native_page_min_chars: int,
    ocr_dpi: int,
) -> int:
    low_rows = scan_low_text_outputs(
        max_chars_per_page=max_chars_per_page,
        min_total_chars=min_total_chars,
    )
    if not low_rows:
        print("=" * 60)
        print("MINUTES OCR LOW-TEXT RERUN")
        print("  No low-text OCR records found; nothing to rerun.")
        print("=" * 60)
        return 0

    selected_rows = low_rows[:low_text_limit] if low_text_limit else low_rows
    selected_codes = {str(r["minutes_code"]) for r in selected_rows}
    likely_empty_before = sum(1 for r in selected_rows if r["likely_empty"])

    print("=" * 60)
    print("MINUTES OCR LOW-TEXT RERUN")
    print(f"  Low-text candidates found: {len(low_rows)}")
    print(f"  Selected for rerun: {len(selected_rows)}")
    print(f"  Likely-empty among selected: {likely_empty_before}")
    print(f"  Heuristic max chars/page: {max_chars_per_page}")
    print(f"  Heuristic min total chars: {min_total_chars}")
    print(f"  OCR mode: force + all-pages + dpi={max(ocr_dpi, 450)}")
    if dry_run:
        print("  Dry run: yes (no files written)")
    print("=" * 60)

    preview = selected_rows[:20]
    for row in preview:
        print(
            f"  - {row['minutes_code']} cpp={row['chars_per_page']} "
            f"total_chars={row['total_text_chars']} likely_empty={row['likely_empty']}"
        )
    if len(selected_rows) > len(preview):
        print(f"  ... and {len(selected_rows) - len(preview)} more")

    if dry_run:
        return 0

    run_summary = run_ocr(
        limit=None,
        force=True,
        dry_run=False,
        native_page_min_chars=native_page_min_chars,
        ocr_dpi=max(ocr_dpi, 450),
        ocr_all_pages=True,
        target_codes=selected_codes,
    )

    remaining_low_rows = scan_low_text_outputs(
        max_chars_per_page=max_chars_per_page,
        min_total_chars=min_total_chars,
        target_codes=selected_codes,
    )
    likely_empty_after = sum(1 for r in remaining_low_rows if r["likely_empty"])
    improved = len(selected_rows) - len(remaining_low_rows)

    run_dir = RUNS_ROOT / run_summary["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    low_text_summary = {
        "run_id": run_summary["run_id"],
        "mode": "low_text_rerun",
        "selected_count": len(selected_rows),
        "selected_likely_empty_before": likely_empty_before,
        "remaining_low_text_after": len(remaining_low_rows),
        "remaining_likely_empty_after": likely_empty_after,
        "improved_count": improved,
        "max_chars_per_page": max_chars_per_page,
        "min_total_chars": min_total_chars,
        "selected_minutes_codes": [r["minutes_code"] for r in selected_rows],
    }
    (run_dir / "minutes_ocr_low_text_rerun_summary.json").write_text(
        json.dumps(low_text_summary, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )

    print("=" * 60)
    print("MINUTES OCR LOW-TEXT RERUN RESULT")
    print(f"  Selected for rerun: {len(selected_rows)}")
    print(f"  Improved (no longer low-text): {improved}")
    print(f"  Still low-text: {len(remaining_low_rows)}")
    print(f"  Still likely-empty: {likely_empty_after}")
    print(f"  Run artifacts: {run_dir}")
    print("=" * 60)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR approved minutes PDFs into normalized Minutes schema output.")
    parser.add_argument("--limit", type=int, default=None, help="Process first N vaulted PDFs.")
    parser.add_argument("--force", action="store_true", help="Rebuild outputs even if unchanged by state.")
    parser.add_argument("--dry-run", action="store_true", help="Scan only; do not write outputs.")
    parser.add_argument(
        "--native-page-min-chars",
        type=int,
        default=80,
        help="Minimum native page text length before OCR fallback is triggered.",
    )
    parser.add_argument("--ocr-dpi", type=int, default=300, help="DPI for pdftoppm rasterization during OCR fallback.")
    parser.add_argument("--ocr-all-pages", action="store_true", help="Force OCR for all pages instead of fallback mode.")
    parser.add_argument(
        "--until-complete",
        action="store_true",
        help="Run OCR in resumable batches until all vaulted PDFs are represented in OCR state.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Batch size per pass when using --until-complete.",
    )
    parser.add_argument(
        "--max-passes",
        type=int,
        default=1000,
        help="Safety cap for passes when using --until-complete.",
    )
    parser.add_argument(
        "--reconcile-existing",
        action="store_true",
        help="Repair OCR state/manifest from existing M1.MN output records without OCRing PDFs.",
    )
    parser.add_argument(
        "--rerun-low-text",
        action="store_true",
        help="Rerun OCR for low-text M1.MN output records only (does not touch M1.AG.MN mined outputs).",
    )
    parser.add_argument(
        "--low-text-max-chars-per-page",
        type=float,
        default=500.0,
        help="Low-text heuristic threshold: avg chars/page below this is flagged for rerun.",
    )
    parser.add_argument(
        "--low-text-min-total-chars",
        type=int,
        default=1000,
        help="Low-text heuristic threshold: total text chars below this is flagged for rerun.",
    )
    parser.add_argument(
        "--low-text-limit",
        type=int,
        default=None,
        help="Optional cap for how many low-text records to rerun.",
    )
    args = parser.parse_args()

    if args.reconcile_existing:
        summary = reconcile_existing_outputs(dry_run=args.dry_run)
        print("=" * 60)
        print("MINUTES OCR RECONCILE SUMMARY")
        print(f"  Run ID: {summary['run_id']}")
        print(f"  Scanned output records: {summary['scanned_output_records']}")
        print(f"  Repaired state records: {summary['repaired_state_records']}")
        print(f"  Added manifest rows: {summary['added_manifest_rows']}")
        print(f"  Invalid payloads: {summary['invalid_payloads']}")
        print(f"  Dry run: {'yes' if summary['dry_run'] else 'no'}")
        print("=" * 60)
        return 0

    if args.rerun_low_text:
        if args.low_text_max_chars_per_page <= 0:
            print("--low-text-max-chars-per-page must be > 0")
            return 2
        if args.low_text_min_total_chars < 0:
            print("--low-text-min-total-chars must be >= 0")
            return 2
        if args.low_text_limit is not None and args.low_text_limit <= 0:
            print("--low-text-limit must be > 0 when provided")
            return 2
        if args.until_complete:
            print("--rerun-low-text cannot be combined with --until-complete")
            return 2
        if args.limit is not None:
            print("--rerun-low-text ignores --limit; use --low-text-limit instead")
            return 2

        return run_low_text_rerun(
            max_chars_per_page=args.low_text_max_chars_per_page,
            min_total_chars=args.low_text_min_total_chars,
            low_text_limit=args.low_text_limit,
            dry_run=args.dry_run,
            native_page_min_chars=args.native_page_min_chars,
            ocr_dpi=args.ocr_dpi,
        )

    if args.until_complete:
        if args.dry_run:
            print("--until-complete with --dry-run is not meaningful; run without --dry-run.")
            return 2
        if args.batch_size <= 0:
            print("--batch-size must be > 0")
            return 2
        if args.max_passes <= 0:
            print("--max-passes must be > 0")
            return 2

        total = count_vaulted_minutes_pdfs()
        print("=" * 60)
        print("MINUTES OCR UNTIL-COMPLETE")
        print(f"  Vaulted PDFs total: {total}")
        print(f"  Starting OCR state records: {count_ocr_state_records()}")
        print(f"  Batch size: {args.batch_size}")
        print(f"  Max passes: {args.max_passes}")
        print("=" * 60)

        passes = 0
        previous_state_count = count_ocr_state_records()
        while passes < args.max_passes:
            current_state_count = count_ocr_state_records()
            remaining = max(0, total - current_state_count)
            if remaining <= 0:
                break

            passes += 1
            print(f"Pass {passes}: remaining={remaining}")
            summary = run_ocr(
                limit=args.batch_size,
                force=args.force,
                dry_run=False,
                native_page_min_chars=args.native_page_min_chars,
                ocr_dpi=args.ocr_dpi,
                ocr_all_pages=args.ocr_all_pages,
            )
            now_count = count_ocr_state_records()
            gained = now_count - previous_state_count
            previous_state_count = now_count
            if summary["prepared_records"] == 0 and gained <= 0:
                print("No progress in this pass; stopping to avoid infinite loop.")
                break

        final_count = count_ocr_state_records()
        print("=" * 60)
        print("MINUTES OCR UNTIL-COMPLETE RESULT")
        print(f"  Passes run: {passes}")
        print(f"  Vaulted PDFs total: {total}")
        print(f"  OCR state records: {final_count}")
        print(f"  Remaining: {max(0, total - final_count)}")
        print("=" * 60)
        return 0

    run_ocr(
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
        native_page_min_chars=args.native_page_min_chars,
        ocr_dpi=args.ocr_dpi,
        ocr_all_pages=args.ocr_all_pages,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
