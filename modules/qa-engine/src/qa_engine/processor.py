from __future__ import annotations

from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import datetime
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import fitz

from src.common.logger import PipelineLogger

try:
    from PIL import Image
    import pytesseract
except ImportError:  # pragma: no cover - optional OCR fallback
    Image = None
    pytesseract = None


ALNUM_RE = re.compile(r"[a-z0-9]+")
SPACE_RE = re.compile(r"\s+")

if pytesseract is not None:
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


@dataclass
class QAInputDescriptor:
    job_folder: Path
    parser_root: Path
    parser_output_root: Path
    parser_machine_readable_folder: Path
    parser_run_id: str
    source_pdf_path: Path | None
    source_pdf_hash: str | None


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def normalize_text(text: str) -> str:
    lowered = text.lower()
    alnum_space = "".join(ch if ch.isalnum() else " " for ch in lowered)
    return SPACE_RE.sub(" ", alnum_space).strip()


def token_list(text: str) -> list[str]:
    return ALNUM_RE.findall(text.lower())


def token_set(text: str) -> set[str]:
    return set(token_list(text))


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_qa_page_machine_code(document_machine_code: str | None, qa_run_id: str, source_page_number: int) -> str:
    document_prefix = (document_machine_code or "DOC_UNKNOWN").strip() or "DOC_UNKNOWN"
    return f"{document_prefix}.QA.{qa_run_id}.P{source_page_number:04d}"


def _render_page_grayscale_bytes(page: fitz.Page, ocr_dpi: int) -> tuple[bytes, int, int]:
    zoom = max(72, int(ocr_dpi)) / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
    return bytes(pix.samples), int(pix.width), int(pix.height)


def _candidate_is_better(candidate: dict, best_candidate: dict | None) -> bool:
    if best_candidate is None:
        return True
    if candidate["selection_score"] > best_candidate["selection_score"]:
        return True
    if candidate["selection_score"] == best_candidate["selection_score"] and candidate["quality"] > best_candidate["quality"]:
        return True
    if (
        candidate["selection_score"] == best_candidate["selection_score"]
        and candidate["quality"] == best_candidate["quality"]
        and candidate["normalized_len"] > best_candidate["normalized_len"]
    ):
        return True
    return False


def _candidate_is_cascade_confident(
    candidate: dict,
    parser_hint_norm: str,
    cascade_quality_threshold: float,
    cascade_alignment_threshold: float,
    cascade_min_chars: int,
    native_norm_len: int,
) -> bool:
    if float(candidate.get("quality") or 0.0) < float(cascade_quality_threshold):
        return False
    min_chars_gate = max(int(cascade_min_chars), int(native_norm_len) + 24)
    if int(candidate.get("normalized_len") or 0) < min_chars_gate:
        return False
    if parser_hint_norm and float(candidate.get("parser_alignment") or 0.0) < float(cascade_alignment_threshold):
        return False
    return True


def _evaluate_ocr_candidates_from_image(
    samples: bytes,
    width: int,
    height: int,
    ocr_psm_candidates: list[int],
    ocr_rotation_candidates: list[int],
    parser_hint_norm: str,
    parser_alignment_weight: float,
    native_norm_len: int = 0,
    cascade_enabled: bool = False,
    cascade_quality_threshold: float = 0.55,
    cascade_alignment_threshold: float = 0.16,
    cascade_min_chars: int = 100,
    ocr_dpi_used: int = 0,
    ocr_timeout_seconds: float = 0.0,
) -> dict:
    if pytesseract is None or Image is None:
        return {"best_candidate": None, "candidate_count": 0, "elapsed_seconds": 0.0, "early_stop": False}

    started = time.perf_counter()
    candidate_count = 0
    best_candidate = None
    early_stop = False
    base_image = Image.frombytes("L", [int(width), int(height)], samples)
    for rotation in ocr_rotation_candidates:
        rotation_value = int(rotation) % 360
        rotated_image = base_image.rotate(rotation_value, expand=True) if rotation_value else base_image
        for psm in ocr_psm_candidates:
            candidate_count += 1
            try:
                timeout_seconds = float(ocr_timeout_seconds or 0.0)
                if timeout_seconds > 0:
                    ocr_text = str(
                        pytesseract.image_to_string(
                            rotated_image,
                            config=f"--psm {int(psm)}",
                            timeout=timeout_seconds,
                        )
                        or ""
                    )
                else:
                    ocr_text = str(pytesseract.image_to_string(rotated_image, config=f"--psm {int(psm)}") or "")
            except Exception:
                ocr_text = ""
            metrics = text_quality_metrics(ocr_text)
            quality = score_source_reference_quality(metrics)
            candidate_norm = normalize_text(ocr_text)
            normalized_len = len(candidate_norm)
            parser_alignment = SequenceMatcher(None, candidate_norm, parser_hint_norm).ratio() if parser_hint_norm else 0.0
            selection_score = quality + (float(parser_alignment_weight) * parser_alignment)
            candidate = {
                "text": ocr_text,
                "quality": quality,
                "parser_alignment": round(parser_alignment, 4),
                "selection_score": round(selection_score, 4),
                "normalized_len": normalized_len,
                "psm": int(psm),
                "rotation": rotation_value,
                "dpi": int(ocr_dpi_used),
            }
            if _candidate_is_better(candidate, best_candidate):
                best_candidate = candidate
                if cascade_enabled and _candidate_is_cascade_confident(
                    candidate=best_candidate,
                    parser_hint_norm=parser_hint_norm,
                    cascade_quality_threshold=cascade_quality_threshold,
                    cascade_alignment_threshold=cascade_alignment_threshold,
                    cascade_min_chars=cascade_min_chars,
                    native_norm_len=native_norm_len,
                ):
                    early_stop = True
                    break
        if early_stop:
            break

    return {
        "best_candidate": best_candidate,
        "candidate_count": candidate_count,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "early_stop": early_stop,
    }


def _evaluate_ocr_job(payload: dict[str, Any]) -> dict:
    return _evaluate_ocr_candidates_from_image(
        samples=bytes(payload.get("samples") or b""),
        width=int(payload.get("width") or 0),
        height=int(payload.get("height") or 0),
        ocr_psm_candidates=[int(value) for value in (payload.get("ocr_psm_candidates") or [])],
        ocr_rotation_candidates=[int(value) for value in (payload.get("ocr_rotation_candidates") or [])],
        parser_hint_norm=str(payload.get("parser_hint_norm") or ""),
        parser_alignment_weight=float(payload.get("parser_alignment_weight") or 0.0),
        native_norm_len=int(payload.get("native_norm_len") or 0),
        cascade_enabled=bool(payload.get("cascade_enabled", False)),
        cascade_quality_threshold=float(payload.get("cascade_quality_threshold") or 0.55),
        cascade_alignment_threshold=float(payload.get("cascade_alignment_threshold") or 0.16),
        cascade_min_chars=int(payload.get("cascade_min_chars") or 100),
        ocr_dpi_used=int(payload.get("ocr_dpi_used") or 0),
        ocr_timeout_seconds=float(payload.get("ocr_timeout_seconds") or 0.0),
    )


def _run_ocr_jobs(
    jobs: list[dict[str, Any]],
    max_workers: int,
    backend: str,
    max_inflight_factor: int,
) -> dict:
    if not jobs:
        return {
            "results_by_page": {},
            "backend_requested": backend,
            "backend_effective": backend,
            "errors": [],
            "elapsed_seconds": 0.0,
        }

    backend_norm = str(backend or "thread").strip().lower()
    if backend_norm not in {"thread", "process"}:
        backend_norm = "thread"
    max_workers_safe = max(1, int(max_workers))
    max_inflight = max(1, int(max_inflight_factor) * max_workers_safe)

    def execute_with_backend(backend_name: str) -> tuple[dict[int, dict], list[str]]:
        executor_cls = ProcessPoolExecutor if backend_name == "process" else ThreadPoolExecutor
        results_by_page: dict[int, dict] = {}
        errors: list[str] = []
        with executor_cls(max_workers=max_workers_safe) as executor:
            future_map: dict[Any, int] = {}

            def collect_done_future(done_future: Any) -> None:
                page_number = future_map.pop(done_future)
                try:
                    results_by_page[page_number] = done_future.result()
                except Exception as exc:
                    errors.append(f"page_{page_number}:{exc}")
                    results_by_page[page_number] = {
                        "best_candidate": None,
                        "candidate_count": 0,
                        "elapsed_seconds": 0.0,
                        "early_stop": False,
                    }

            for job in jobs:
                page_number = int(job.get("page_number") or 0)
                payload = dict(job)
                payload.pop("page_number", None)
                future = executor.submit(_evaluate_ocr_job, payload)
                future_map[future] = page_number
                if len(future_map) >= max_inflight:
                    collect_done_future(next(as_completed(future_map)))

            for done_future in as_completed(list(future_map.keys())):
                collect_done_future(done_future)
        return results_by_page, errors

    started = time.perf_counter()
    try:
        results_by_page, errors = execute_with_backend(backend_norm)
        return {
            "results_by_page": results_by_page,
            "backend_requested": backend_norm,
            "backend_effective": backend_norm,
            "errors": errors,
            "elapsed_seconds": round(time.perf_counter() - started, 4),
        }
    except Exception:
        if backend_norm != "process":
            raise
        results_by_page, errors = execute_with_backend("thread")
        return {
            "results_by_page": results_by_page,
            "backend_requested": backend_norm,
            "backend_effective": "thread",
            "errors": errors,
            "elapsed_seconds": round(time.perf_counter() - started, 4),
        }


def text_quality_metrics(text: str) -> dict:
    raw = str(text or "")
    normalized = normalize_text(raw)
    tokens = token_list(normalized)
    token_count = len(tokens)
    unique_token_count = len(set(tokens))
    alnum_count = sum(1 for ch in raw if ch.isalnum())
    symbol_count = sum(1 for ch in raw if not ch.isalnum() and not ch.isspace())
    symbol_ratio = (symbol_count / len(raw)) if raw else 0.0
    short_token_ratio = (sum(1 for tok in tokens if len(tok) <= 1) / token_count) if token_count else 1.0

    alpha_tokens = [tok for tok in tokens if any(ch.isalpha() for ch in tok)]
    lexical_ratio = (len(alpha_tokens) / token_count) if token_count else 0.0
    vowel_tokens = [tok for tok in alpha_tokens if any(v in tok for v in "aeiou")]
    vowel_ratio = (len(vowel_tokens) / len(alpha_tokens)) if alpha_tokens else 0.0
    numeric_tokens = [tok for tok in tokens if any(ch.isdigit() for ch in tok)]
    numeric_ratio = (len(numeric_tokens) / token_count) if token_count else 0.0

    return {
        "char_count": len(raw),
        "normalized_char_count": len(normalized),
        "alnum_count": alnum_count,
        "token_count": token_count,
        "unique_token_count": unique_token_count,
        "lexical_ratio": lexical_ratio,
        "vowel_ratio": vowel_ratio,
        "numeric_ratio": numeric_ratio,
        "symbol_ratio": symbol_ratio,
        "short_token_ratio": short_token_ratio,
    }


def score_source_reference_quality(metrics: dict) -> float:
    alnum_term = min(1.0, float(metrics.get("alnum_count", 0)) / 320.0)
    token_term = min(1.0, float(metrics.get("token_count", 0)) / 120.0)
    lexical_term = min(1.0, float(metrics.get("lexical_ratio", 0.0)))
    vowel_term = min(1.0, float(metrics.get("vowel_ratio", 0.0)))
    symbol_term = max(0.0, 1.0 - min(1.0, float(metrics.get("symbol_ratio", 0.0)) * 3.0))
    short_token_term = max(0.0, 1.0 - min(1.0, float(metrics.get("short_token_ratio", 1.0))))
    numeric_term = min(1.0, float(metrics.get("numeric_ratio", 0.0)) * 1.5)

    quality = (
        (0.26 * alnum_term)
        + (0.20 * token_term)
        + (0.16 * lexical_term)
        + (0.14 * vowel_term)
        + (0.10 * symbol_term)
        + (0.08 * short_token_term)
        + (0.06 * numeric_term)
    )
    return round(max(0.0, min(1.0, quality)), 4)


def extract_source_text_by_page(
    source_pdf_path: Path,
    min_chars_for_native: int,
    ocr_fallback_enabled: bool,
    ocr_dpi: int,
    ocr_psm_candidates: list[int],
    ocr_rotation_candidates: list[int],
    source_reference_min_quality: float,
    source_ocr_min_alignment_for_reliable: float,
    parser_hint_text_by_page: dict[int, str] | None = None,
    parser_alignment_weight: float = 0.0,
    ocr_workers: int = 0,
    ocr_worker_backend: str = "thread",
    ocr_max_inflight_factor: int = 2,
    ocr_candidate_cascade_enabled: bool = False,
    ocr_cascade_quality_threshold: float = 0.55,
    ocr_cascade_alignment_threshold: float = 0.16,
    ocr_cascade_min_chars: int = 100,
    ocr_adaptive_dpi_enabled: bool = False,
    ocr_dpi_primary: int = 0,
    ocr_dpi_fallback: int = 0,
    ocr_timeout_seconds: float = 0.0,
) -> tuple[dict[int, str], dict[int, dict], dict]:
    page_map: dict[int, str] = {}
    source_meta_map: dict[int, dict] = {}
    page_state_by_number: dict[int, dict[str, Any]] = {}
    extraction_started = time.perf_counter()

    configured_workers = int(ocr_workers)
    if configured_workers <= 0:
        configured_workers = int(os.cpu_count() or 1)
    configured_workers = max(1, min(8, configured_workers))
    worker_backend_requested = str(ocr_worker_backend or "thread").strip().lower()
    if worker_backend_requested not in {"thread", "process"}:
        worker_backend_requested = "thread"
    max_inflight_factor = max(1, int(ocr_max_inflight_factor or 1))

    primary_dpi = int(ocr_dpi_primary or ocr_dpi or 240)
    fallback_dpi = int(ocr_dpi_fallback or ocr_dpi or primary_dpi)
    if primary_dpi < 72:
        primary_dpi = 72
    if fallback_dpi < 72:
        fallback_dpi = 72
    adaptive_dpi_enabled = bool(ocr_adaptive_dpi_enabled and fallback_dpi != primary_dpi)

    psm_candidates = [int(value) for value in (ocr_psm_candidates or [3, 4, 6, 11])]
    rotation_candidates = [int(value) for value in (ocr_rotation_candidates or [0])]

    open_started = time.perf_counter()
    doc = fitz.open(source_pdf_path)
    open_elapsed = round(time.perf_counter() - open_started, 4)
    scan_started = time.perf_counter()

    primary_jobs: list[dict[str, Any]] = []
    fallback_jobs: list[dict[str, Any]] = []
    primary_results_by_page: dict[int, dict] = {}
    fallback_results_by_page: dict[int, dict] = {}
    primary_errors: list[str] = []
    fallback_errors: list[str] = []
    worker_backend_effective = worker_backend_requested
    cascade_early_stop_count = 0
    fallback_dpi_page_count = 0
    ocr_render_error_count = 0
    primary_eval_elapsed = 0.0
    fallback_eval_elapsed = 0.0
    primary_eval_seconds_sum = 0.0
    fallback_eval_seconds_sum = 0.0
    fallback_prepare_started = 0.0
    fallback_prepare_elapsed = 0.0

    try:
        for index in range(len(doc)):
            page_number = index + 1
            page = doc[index]
            parser_hint_text = str((parser_hint_text_by_page or {}).get(page_number) or "")
            parser_hint_norm = normalize_text(parser_hint_text)
            native_text = page.get_text("text") or ""
            native_norm = normalize_text(native_text)
            native_metrics = text_quality_metrics(native_text)
            native_quality = score_source_reference_quality(native_metrics)
            selected_text = native_text
            selected_method = "native"
            selected_psm = None
            selected_rotation = None
            selected_quality = native_quality
            selected_alignment = SequenceMatcher(None, native_norm, parser_hint_norm).ratio() if parser_hint_norm else 0.0
            selected_selection_score = selected_quality + (float(parser_alignment_weight) * selected_alignment)
            should_attempt_ocr = bool(ocr_fallback_enabled and len(native_norm) < min_chars_for_native)
            ocr_candidate_count = (len(rotation_candidates) * len(psm_candidates)) if should_attempt_ocr else 0

            page_state_by_number[page_number] = {
                "parser_hint_norm": parser_hint_norm,
                "native_quality": native_quality,
                "native_norm_len": len(native_norm),
                "selected_text": selected_text,
                "selected_method": selected_method,
                "selected_psm": selected_psm,
                "selected_rotation": selected_rotation,
                "selected_quality": selected_quality,
                "selected_alignment": selected_alignment,
                "selected_selection_score": selected_selection_score,
                "ocr_candidate_count": ocr_candidate_count,
                "should_attempt_ocr": should_attempt_ocr,
            }

            if not should_attempt_ocr or ocr_candidate_count <= 0:
                continue
            if pytesseract is None or Image is None:
                continue
            try:
                samples, width, height = _render_page_grayscale_bytes(page, primary_dpi)
            except Exception:
                ocr_render_error_count += 1
                continue
            primary_jobs.append(
                {
                    "page_number": page_number,
                    "samples": samples,
                    "width": width,
                    "height": height,
                    "ocr_psm_candidates": psm_candidates,
                    "ocr_rotation_candidates": rotation_candidates,
                    "parser_hint_norm": parser_hint_norm,
                    "parser_alignment_weight": float(parser_alignment_weight),
                    "native_norm_len": len(native_norm),
                    "cascade_enabled": bool(ocr_candidate_cascade_enabled),
                    "cascade_quality_threshold": float(ocr_cascade_quality_threshold),
                    "cascade_alignment_threshold": float(ocr_cascade_alignment_threshold),
                    "cascade_min_chars": int(ocr_cascade_min_chars),
                    "ocr_dpi_used": int(primary_dpi),
                    "ocr_timeout_seconds": float(ocr_timeout_seconds),
                }
            )

        if primary_jobs and pytesseract is not None and Image is not None:
            primary_exec = _run_ocr_jobs(
                jobs=primary_jobs,
                max_workers=configured_workers,
                backend=worker_backend_requested,
                max_inflight_factor=max_inflight_factor,
            )
            primary_results_by_page = dict(primary_exec.get("results_by_page") or {})
            primary_errors = list(primary_exec.get("errors") or [])
            primary_eval_elapsed = float(primary_exec.get("elapsed_seconds") or 0.0)
            worker_backend_effective = str(primary_exec.get("backend_effective") or worker_backend_effective)

        if adaptive_dpi_enabled and primary_results_by_page:
            fallback_prepare_started = time.perf_counter()
            fallback_quality_floor = max(float(source_reference_min_quality), float(ocr_cascade_quality_threshold) + 0.02)
            fallback_alignment_floor = max(float(source_ocr_min_alignment_for_reliable), float(ocr_cascade_alignment_threshold) + 0.02)
            for page_number in sorted(primary_results_by_page):
                state = page_state_by_number.get(page_number) or {}
                parser_hint_norm = str(state.get("parser_hint_norm") or "")
                native_norm_len = int(state.get("native_norm_len") or 0)
                primary_result = primary_results_by_page.get(page_number) or {}
                primary_candidate = primary_result.get("best_candidate")

                needs_fallback = primary_candidate is None
                if primary_candidate is not None:
                    candidate_norm_len = int(primary_candidate.get("normalized_len") or 0)
                    candidate_quality = float(primary_candidate.get("quality") or 0.0)
                    candidate_alignment = float(primary_candidate.get("parser_alignment") or 0.0)
                    if candidate_norm_len <= (native_norm_len + 16):
                        needs_fallback = True
                    elif candidate_quality < fallback_quality_floor:
                        needs_fallback = True
                    elif parser_hint_norm and candidate_alignment < fallback_alignment_floor:
                        needs_fallback = True
                    elif bool(ocr_candidate_cascade_enabled) and not bool(primary_result.get("early_stop")):
                        needs_fallback = True
                    else:
                        needs_fallback = False

                if not needs_fallback:
                    continue
                try:
                    page = doc[page_number - 1]
                    samples, width, height = _render_page_grayscale_bytes(page, fallback_dpi)
                except Exception:
                    ocr_render_error_count += 1
                    continue
                fallback_jobs.append(
                    {
                        "page_number": page_number,
                        "samples": samples,
                        "width": width,
                        "height": height,
                        "ocr_psm_candidates": psm_candidates,
                        "ocr_rotation_candidates": rotation_candidates,
                        "parser_hint_norm": parser_hint_norm,
                        "parser_alignment_weight": float(parser_alignment_weight),
                        "native_norm_len": native_norm_len,
                        "cascade_enabled": bool(ocr_candidate_cascade_enabled),
                        "cascade_quality_threshold": float(ocr_cascade_quality_threshold),
                        "cascade_alignment_threshold": float(ocr_cascade_alignment_threshold),
                        "cascade_min_chars": int(ocr_cascade_min_chars),
                        "ocr_dpi_used": int(fallback_dpi),
                        "ocr_timeout_seconds": float(ocr_timeout_seconds),
                    }
                )
            fallback_prepare_elapsed = round(time.perf_counter() - fallback_prepare_started, 4)
            fallback_dpi_page_count = len(fallback_jobs)
            if fallback_jobs:
                fallback_exec = _run_ocr_jobs(
                    jobs=fallback_jobs,
                    max_workers=configured_workers,
                    backend=worker_backend_effective,
                    max_inflight_factor=max_inflight_factor,
                )
                fallback_results_by_page = dict(fallback_exec.get("results_by_page") or {})
                fallback_errors = list(fallback_exec.get("errors") or [])
                fallback_eval_elapsed = float(fallback_exec.get("elapsed_seconds") or 0.0)
                if worker_backend_effective == "process":
                    worker_backend_effective = str(fallback_exec.get("backend_effective") or worker_backend_effective)
    finally:
        doc.close()

    scan_elapsed = round(time.perf_counter() - scan_started, 4)
    ocr_elapsed = round(primary_eval_elapsed + fallback_eval_elapsed, 4)
    ocr_candidate_page_count = len(primary_jobs)
    ocr_evaluated_page_count = len(primary_results_by_page)
    ocr_worker_count = 0
    if ocr_candidate_page_count > 0 and pytesseract is not None and Image is not None:
        ocr_worker_count = configured_workers

    finalize_started = time.perf_counter()
    ocr_candidate_total = 0
    ocr_selected_page_count = 0
    primary_candidate_total = 0
    fallback_candidate_total = 0
    fallback_preferred_page_count = 0
    for page_number in sorted(page_state_by_number):
        state = page_state_by_number[page_number]
        parser_hint_norm = str(state.get("parser_hint_norm") or "")
        selected_text = str(state.get("selected_text") or "")
        selected_method = str(state.get("selected_method") or "native")
        selected_psm = state.get("selected_psm")
        selected_rotation = state.get("selected_rotation")
        selected_quality = float(state.get("selected_quality") or 0.0)
        selected_alignment = float(state.get("selected_alignment") or 0.0)
        selected_selection_score = float(state.get("selected_selection_score") or 0.0)
        native_quality = float(state.get("native_quality") or 0.0)
        native_norm_len = int(state.get("native_norm_len") or 0)
        candidate_count = int(state.get("ocr_candidate_count") or 0)

        primary_result = primary_results_by_page.get(page_number) or {}
        fallback_result = fallback_results_by_page.get(page_number) or {}
        best_candidate = None

        primary_count = int(primary_result.get("candidate_count") or 0)
        fallback_count = int(fallback_result.get("candidate_count") or 0)
        primary_candidate_total += primary_count
        fallback_candidate_total += fallback_count
        candidate_count = primary_count + fallback_count if (primary_count + fallback_count) > 0 else candidate_count
        ocr_candidate_total += candidate_count

        primary_candidate = primary_result.get("best_candidate")
        fallback_candidate = fallback_result.get("best_candidate")
        if fallback_candidate is not None:
            best_candidate = fallback_candidate
            fallback_preferred_page_count += 1
        elif primary_candidate is not None:
            best_candidate = primary_candidate
        if primary_result.get("early_stop"):
            cascade_early_stop_count += 1
        if fallback_result.get("early_stop"):
            cascade_early_stop_count += 1
        primary_eval_seconds_sum += float(primary_result.get("elapsed_seconds") or 0.0)
        fallback_eval_seconds_sum += float(fallback_result.get("elapsed_seconds") or 0.0)

        if best_candidate and int(best_candidate.get("normalized_len") or 0) > native_norm_len:
            selected_text = str(best_candidate.get("text") or "")
            selected_psm = best_candidate.get("psm")
            selected_rotation = best_candidate.get("rotation")
            selected_quality = float(best_candidate.get("quality") or 0.0)
            selected_alignment = float(best_candidate.get("parser_alignment") or 0.0)
            selected_selection_score = float(best_candidate.get("selection_score") or 0.0)
            if selected_quality >= float(source_reference_min_quality):
                selected_method = "ocr_fallback"
            else:
                selected_method = "ocr_fallback_low_quality"
            ocr_selected_page_count += 1

        is_reliable = bool(selected_quality >= float(source_reference_min_quality))
        if selected_method.startswith("ocr_fallback") and parser_hint_norm:
            if selected_alignment < float(source_ocr_min_alignment_for_reliable):
                is_reliable = False
                if selected_method == "ocr_fallback":
                    selected_method = "ocr_fallback_low_alignment"

        page_map[page_number] = selected_text
        source_meta_map[page_number] = {
            "method": selected_method,
            "reference_quality": selected_quality,
            "is_reliable": is_reliable,
            "parser_alignment_to_candidate": round(selected_alignment, 4),
            "selection_score": round(selected_selection_score, 4),
            "native_quality": native_quality,
            "native_normalized_len": native_norm_len,
            "selected_psm": selected_psm,
            "selected_rotation": selected_rotation,
            "selected_dpi": best_candidate.get("dpi") if best_candidate else None,
            "ocr_candidate_count": candidate_count,
        }

    finalize_elapsed = round(time.perf_counter() - finalize_started, 4)
    extraction_stats = {
        "page_count": len(page_state_by_number),
        "ocr_candidate_page_count": ocr_candidate_page_count,
        "ocr_evaluated_page_count": ocr_evaluated_page_count,
        "ocr_worker_count": ocr_worker_count,
        "ocr_worker_backend_requested": worker_backend_requested,
        "ocr_worker_backend_effective": worker_backend_effective,
        "ocr_timeout_seconds": float(ocr_timeout_seconds),
        "ocr_candidate_total": ocr_candidate_total,
        "ocr_primary_candidate_total": primary_candidate_total,
        "ocr_fallback_candidate_total": fallback_candidate_total,
        "ocr_fallback_dpi_page_count": fallback_dpi_page_count,
        "ocr_fallback_preferred_page_count": fallback_preferred_page_count,
        "ocr_selected_page_count": ocr_selected_page_count,
        "ocr_elapsed_seconds_total": round(primary_eval_seconds_sum + fallback_eval_seconds_sum, 4),
        "ocr_cascade_early_stop_count": cascade_early_stop_count,
        "ocr_render_error_count": ocr_render_error_count,
        "ocr_execution_error_count": len(primary_errors) + len(fallback_errors),
        "ocr_execution_errors": (primary_errors + fallback_errors)[:20],
        "ocr_primary_dpi": primary_dpi,
        "ocr_fallback_dpi": fallback_dpi if adaptive_dpi_enabled else None,
        "ocr_adaptive_dpi_enabled": adaptive_dpi_enabled,
        "ocr_candidate_cascade_enabled": bool(ocr_candidate_cascade_enabled),
        "timings_seconds": {
            "open_pdf_seconds": open_elapsed,
            "prepare_pages_seconds": scan_elapsed,
            "primary_ocr_evaluation_seconds": round(primary_eval_elapsed, 4),
            "fallback_prepare_seconds": fallback_prepare_elapsed,
            "fallback_ocr_evaluation_seconds": round(fallback_eval_elapsed, 4),
            "ocr_evaluation_seconds": ocr_elapsed,
            "finalize_seconds": finalize_elapsed,
            "total_seconds": round(time.perf_counter() - extraction_started, 4),
        },
    }
    return page_map, source_meta_map, extraction_stats


def score_page(parser_text: str, source_text: str, layout_type: str | None = None) -> tuple[float, float, float, float, float, float]:
    parser_norm = normalize_text(parser_text)
    source_norm = normalize_text(source_text)
    if not parser_norm and not source_norm:
        return 1.0, 1.0, 1.0, 1.0, 1.0, 1.0
    if not source_norm:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    if not parser_norm:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0

    char_similarity = SequenceMatcher(None, parser_norm, source_norm).ratio()
    parser_tokens = token_set(parser_norm)
    source_tokens = token_set(source_norm)
    overlap_tokens = parser_tokens.intersection(source_tokens)

    token_recall = (len(overlap_tokens) / len(source_tokens)) if source_tokens else 0.0
    token_precision = (len(overlap_tokens) / len(parser_tokens)) if parser_tokens else 0.0
    token_f1 = (2.0 * token_precision * token_recall / (token_precision + token_recall)) if (token_precision + token_recall) > 0 else 0.0

    parser_numeric_tokens = {tok for tok in parser_tokens if any(ch.isdigit() for ch in tok)}
    source_numeric_tokens = {tok for tok in source_tokens if any(ch.isdigit() for ch in tok)}
    numeric_overlap = parser_numeric_tokens.intersection(source_numeric_tokens)
    numeric_token_recall = (len(numeric_overlap) / len(source_numeric_tokens)) if source_numeric_tokens else token_recall

    layout = str(layout_type or "").strip().lower()
    if layout in {"table", "mixed", "form"}:
        composite = (0.20 * char_similarity) + (0.55 * token_f1) + (0.25 * numeric_token_recall)
    else:
        composite = (0.45 * char_similarity) + (0.45 * token_f1) + (0.10 * numeric_token_recall)
    return composite, char_similarity, token_recall, token_precision, token_f1, numeric_token_recall


def find_duplicate_groups(page_rows: list[dict], min_chars: int) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for page in page_rows:
        text = str(page.get("parser_text_norm") or "")
        if len(text) < min_chars:
            continue
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        buckets.setdefault(text_hash, []).append(page)

    duplicate_groups = []
    for text_hash, members in buckets.items():
        if len(members) < 2:
            continue
        duplicate_groups.append(
            {
                "text_hash": text_hash,
                "page_count": len(members),
                "source_page_numbers": [member.get("source_page_number") for member in members],
                "page_ids": [member.get("page_id") for member in members],
                "text_preview": str(members[0].get("parser_text_norm") or "")[:240],
            }
        )
    duplicate_groups.sort(key=lambda item: item["page_count"], reverse=True)
    return duplicate_groups


def write_markdown_report(report_path: Path, run_summary: dict, page_rows: list[dict]) -> None:
    lines = [
        f"# QA Report {run_summary['qa_run_id']}",
        "",
        f"- Generated: {datetime.datetime.now().isoformat()}",
        f"- Parser run: `{run_summary.get('parser_run_id')}`",
        f"- Status: **{str(run_summary.get('status') or '').upper()}**",
        f"- Comparable pages: {run_summary.get('comparable_page_count', 0)}",
        f"- Pass pages: {run_summary.get('pass_page_count', 0)}",
        f"- Warn pages: {run_summary.get('warn_page_count', 0)}",
        f"- Fail pages: {run_summary.get('fail_page_count', 0)}",
        f"- Unverifiable pages: {run_summary.get('unverifiable_page_count', 0)}",
        f"- Pass ratio: {run_summary.get('pass_ratio')}",
        f"- Warn-or-better ratio: {run_summary.get('warn_or_better_ratio')}",
        f"- Accepted ratio: {run_summary.get('accepted_ratio')}",
        f"- Fail ratio: {run_summary.get('fail_ratio')}",
        f"- Duplicate groups: {run_summary.get('duplicate_group_count', 0)}",
        "",
        "## Failing Pages",
        "",
    ]

    failing_rows = [row for row in page_rows if row.get("qa_status") == "fail"]
    if not failing_rows:
        lines.append("- none")
    else:
        for row in failing_rows[:200]:
            lines.append(
                f"- page {row.get('source_page_number')} `{row.get('page_id')}`: "
                f"score={row.get('accuracy_score')} char={row.get('char_similarity')} token={row.get('token_recall')}"
            )

    lines.extend(["", "## Artifact Checks", ""])
    artifact_checks = run_summary.get("artifact_checks", {})
    for key in sorted(artifact_checks):
        value = artifact_checks[key]
        lines.append(f"- {key}: {value}")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _update_parent_run_files(
    descriptor: QAInputDescriptor,
    run_summary: dict,
    qa_output_root: Path,
    logger: PipelineLogger,
) -> None:
    relative_qa_machine_root = Path("qa") / run_summary["qa_run_id"] / "machine_readable"
    parser_machine_root = descriptor.parser_machine_readable_folder

    parent_run_json_path = parser_machine_root / "run.json"
    parent_handoff_path = parser_machine_root / "handoff.json"
    root_handoff_path = descriptor.parser_output_root / "handoff.json"

    qa_summary_for_parent = {
        "qa_run_id": run_summary["qa_run_id"],
        "status": run_summary["status"],
        "status_reason": run_summary.get("status_reason"),
        "assessed_at": run_summary["assessed_at"],
        "source_pdf_hash_match": run_summary["source_pdf_hash_match"],
        "page_count": run_summary["page_count"],
        "comparable_page_count": run_summary["comparable_page_count"],
        "pass_page_count": run_summary["pass_page_count"],
        "warn_page_count": run_summary["warn_page_count"],
        "fail_page_count": run_summary["fail_page_count"],
        "unverifiable_page_count": run_summary.get("unverifiable_page_count", 0),
        "pass_ratio": run_summary["pass_ratio"],
        "warn_or_better_ratio": run_summary["warn_or_better_ratio"],
        "accepted_ratio": run_summary.get("accepted_ratio"),
        "fail_ratio": run_summary.get("fail_ratio"),
        "duplicate_group_count": run_summary["duplicate_group_count"],
        "duplicate_groups": run_summary["duplicate_groups"][:50],
        "artifact_checks": run_summary["artifact_checks"],
        "missing_artifacts": run_summary["missing_artifacts"],
        "qa_machine_readable_path": relative_qa_machine_root.as_posix(),
    }

    for target in [parent_run_json_path, parent_handoff_path, root_handoff_path]:
        if not target.exists():
            continue
        payload = load_json(target)
        payload["qa_assessment"] = qa_summary_for_parent
        write_json(target, payload)

    qa_target = descriptor.parser_output_root / "qa" / run_summary["qa_run_id"]
    if qa_target.exists():
        shutil.rmtree(qa_target)
    shutil.copytree(qa_output_root, qa_target)
    logger.info("QA_COPYBACK_COMPLETE", "SUCCESS", run_id=run_summary["qa_run_id"], message=str(qa_target))


def run_qa_assessment(
    base_dir: Path,
    qa_run_id: str,
    descriptor: QAInputDescriptor,
    thresholds: dict,
    logger: PipelineLogger,
) -> dict:
    settings = thresholds.get("qa", {})
    page_pass_score = float(settings.get("page_pass_score", 0.8))
    page_warn_score = float(settings.get("page_warn_score", 0.65))
    run_pass_ratio = float(settings.get("run_pass_ratio", 0.95))
    run_warn_ratio = float(settings.get("run_warn_ratio", 0.9))
    run_accept_ratio = float(settings.get("run_accept_ratio", 0.95))
    run_max_fail_ratio = float(settings.get("run_max_fail_ratio", 0.05))
    run_min_comparable_pages = int(settings.get("run_min_comparable_pages", 60))
    source_text_min_chars = int(settings.get("source_text_min_chars_for_compare", 80))
    source_ocr_fallback_enabled = bool(settings.get("source_ocr_fallback_enabled", True))
    source_ocr_dpi = int(settings.get("source_ocr_dpi", 240))
    source_ocr_dpi_primary = int(settings.get("source_ocr_dpi_primary", 200))
    source_ocr_dpi_fallback = int(settings.get("source_ocr_dpi_fallback", source_ocr_dpi))
    source_ocr_adaptive_dpi_enabled = bool(settings.get("source_ocr_adaptive_dpi_enabled", True))
    source_ocr_psm_candidates = [int(value) for value in (settings.get("source_ocr_psm_candidates") or [6, 3, 4, 11])]
    source_ocr_rotation_candidates = [int(value) for value in (settings.get("source_ocr_rotation_candidates") or [0, 90, 180, 270])]
    source_reference_min_quality = float(settings.get("source_reference_min_quality", 0.35))
    source_ocr_parser_alignment_weight = float(settings.get("source_ocr_parser_alignment_weight", 0.25))
    source_ocr_min_alignment_for_reliable = float(settings.get("source_ocr_min_alignment_for_reliable", 0.08))
    source_ocr_workers = int(settings.get("source_ocr_workers", 0))
    source_ocr_worker_backend = str(settings.get("source_ocr_worker_backend", "process")).strip().lower()
    source_ocr_max_inflight_factor = int(settings.get("source_ocr_max_inflight_factor", 2))
    source_ocr_candidate_cascade_enabled = bool(settings.get("source_ocr_candidate_cascade_enabled", True))
    source_ocr_cascade_quality_threshold = float(settings.get("source_ocr_cascade_quality_threshold", 0.6))
    source_ocr_cascade_alignment_threshold = float(settings.get("source_ocr_cascade_alignment_threshold", 0.2))
    source_ocr_cascade_min_chars = int(settings.get("source_ocr_cascade_min_chars", 120))
    source_ocr_timeout_seconds = float(settings.get("source_ocr_timeout_seconds", 25.0))
    duplicate_min_chars = int(settings.get("duplicate_min_chars", 80))
    assessment_started = time.perf_counter()
    stage_timings: dict[str, float] = {}

    parser_machine_root = descriptor.parser_machine_readable_folder
    parser_run_json_path = parser_machine_root / "run.json"
    parser_pages_dir = parser_machine_root / "pages"
    parser_handoff_path = parser_machine_root / "handoff.json"

    stage_started = time.perf_counter()
    parser_run_payload = load_json(parser_run_json_path)
    parser_expected_page_count = safe_int(parser_run_payload.get("page_count"), 0)
    parser_page_files = sorted(parser_pages_dir.glob("*.json"))
    parser_payload_by_path: dict[Path, dict] = {}
    parser_hint_text_by_page: dict[int, str] = {}
    for page_path in parser_page_files:
        payload = load_json(page_path)
        parser_payload_by_path[page_path] = payload
        info = payload.get("page", {})
        source_page_number = safe_int(info.get("source_page_number"), 0)
        if source_page_number <= 0:
            continue
        parser_text = str((payload.get("text") or {}).get("content") or "")
        if parser_text and source_page_number not in parser_hint_text_by_page:
            parser_hint_text_by_page[source_page_number] = parser_text
    stage_timings["load_parser_inputs_seconds"] = round(time.perf_counter() - stage_started, 4)

    source_text_map: dict[int, str] = {}
    source_meta_map: dict[int, dict] = {}
    source_extraction_stats: dict[str, Any] = {}
    source_pdf_hash_actual = None
    source_pdf_hash_match = None
    source_available = False
    source_error = None
    stage_started = time.perf_counter()
    if descriptor.source_pdf_path and descriptor.source_pdf_path.exists():
        try:
            source_available = True
            source_text_map, source_meta_map, source_extraction_stats = extract_source_text_by_page(
                descriptor.source_pdf_path,
                min_chars_for_native=source_text_min_chars,
                ocr_fallback_enabled=source_ocr_fallback_enabled,
                ocr_dpi=source_ocr_dpi,
                ocr_psm_candidates=source_ocr_psm_candidates,
                ocr_rotation_candidates=source_ocr_rotation_candidates,
                source_reference_min_quality=source_reference_min_quality,
                source_ocr_min_alignment_for_reliable=source_ocr_min_alignment_for_reliable,
                parser_hint_text_by_page=parser_hint_text_by_page,
                parser_alignment_weight=source_ocr_parser_alignment_weight,
                ocr_workers=source_ocr_workers,
                ocr_worker_backend=source_ocr_worker_backend,
                ocr_max_inflight_factor=source_ocr_max_inflight_factor,
                ocr_candidate_cascade_enabled=source_ocr_candidate_cascade_enabled,
                ocr_cascade_quality_threshold=source_ocr_cascade_quality_threshold,
                ocr_cascade_alignment_threshold=source_ocr_cascade_alignment_threshold,
                ocr_cascade_min_chars=source_ocr_cascade_min_chars,
                ocr_adaptive_dpi_enabled=source_ocr_adaptive_dpi_enabled,
                ocr_dpi_primary=source_ocr_dpi_primary,
                ocr_dpi_fallback=source_ocr_dpi_fallback,
                ocr_timeout_seconds=source_ocr_timeout_seconds,
            )
            source_pdf_hash_actual = sha256_file(descriptor.source_pdf_path)
            if descriptor.source_pdf_hash:
                source_pdf_hash_match = source_pdf_hash_actual.lower() == str(descriptor.source_pdf_hash).lower()
        except Exception as exc:
            source_error = str(exc)
            source_available = False
            source_extraction_stats = {"error": source_error}
    stage_timings["extract_source_seconds"] = round(time.perf_counter() - stage_started, 4)

    page_rows: list[dict] = []
    qa_page_payloads: list[dict] = []
    parser_updated_pages: list[tuple[Path, dict]] = []
    source_method_counter: Counter[str] = Counter()
    source_short_text_count = 0
    source_reference_unreliable_count = 0
    stage_started = time.perf_counter()
    for page_path in parser_page_files:
        page_payload = parser_payload_by_path.get(page_path) or load_json(page_path)
        page_info = page_payload.get("page", {})
        page_id = str(page_info.get("page_id") or page_path.stem)
        source_page_number = safe_int(page_info.get("source_page_number"), 0)
        layout_type = str(page_info.get("layout_type") or page_info.get("page_layout") or "")
        document_machine_code = page_info.get("document_machine_code") or parser_run_payload.get("document_machine_code")
        parser_text = str((page_payload.get("text") or {}).get("content") or "")
        parser_text_norm = normalize_text(parser_text)
        source_text = source_text_map.get(source_page_number, "")
        source_meta = source_meta_map.get(source_page_number, {})
        source_text_method = str(source_meta.get("method") or "unknown")
        source_reference_quality = source_meta.get("reference_quality")
        source_reference_reliable = bool(source_meta.get("is_reliable", False))
        source_selection_score = source_meta.get("selection_score")
        source_parser_alignment = source_meta.get("parser_alignment_to_candidate")
        source_selected_psm = source_meta.get("selected_psm")
        source_selected_rotation = source_meta.get("selected_rotation")
        source_selected_dpi = source_meta.get("selected_dpi")
        source_method_counter[source_text_method] += 1
        source_text_norm = normalize_text(source_text)
        source_norm_len = len(source_text_norm)

        if source_available and source_norm_len < source_text_min_chars:
            source_short_text_count += 1
        if source_available and source_norm_len >= source_text_min_chars and not source_reference_reliable:
            source_reference_unreliable_count += 1

        source_comparable = source_available and source_norm_len >= source_text_min_chars and source_reference_reliable
        if source_comparable:
            composite, char_similarity, token_recall, token_precision, token_f1, numeric_token_recall = score_page(
                parser_text,
                source_text,
                layout_type=layout_type,
            )
            accuracy_score = round(composite, 4)
            char_similarity = round(char_similarity, 4)
            token_recall = round(token_recall, 4)
            token_precision = round(token_precision, 4)
            token_f1 = round(token_f1, 4)
            numeric_token_recall = round(numeric_token_recall, 4)
            if accuracy_score >= page_pass_score:
                qa_status = "pass"
                qa_detail_reason = "score_pass"
            elif accuracy_score >= page_warn_score:
                qa_status = "warn"
                qa_detail_reason = "score_warn"
            else:
                qa_status = "fail"
                qa_detail_reason = "score_fail"
        else:
            accuracy_score = None
            char_similarity = None
            token_recall = None
            token_precision = None
            token_f1 = None
            numeric_token_recall = None
            if not source_available:
                qa_status = "source_unavailable"
                qa_detail_reason = "source_unavailable"
            elif source_norm_len < source_text_min_chars:
                qa_status = "unverifiable"
                qa_detail_reason = "source_text_too_short"
            elif not source_reference_reliable:
                qa_status = "unverifiable"
                qa_detail_reason = "source_reference_low_quality"
            else:
                qa_status = "unverifiable"
                qa_detail_reason = "source_unverifiable"

        qa_page_machine_code = build_qa_page_machine_code(document_machine_code, qa_run_id, max(source_page_number, 0))
        qa_block = {
            "qa_run_id": qa_run_id,
            "qa_page_machine_code": qa_page_machine_code,
            "qa_status": qa_status,
            "qa_detail_reason": qa_detail_reason,
            "accuracy_score": accuracy_score,
            "char_similarity": char_similarity,
            "token_recall": token_recall,
            "token_precision": token_precision,
            "token_f1": token_f1,
            "numeric_token_recall": numeric_token_recall,
            "source_pdf_path": str(descriptor.source_pdf_path) if descriptor.source_pdf_path else None,
            "source_page_number": source_page_number,
            "source_comparable": source_comparable,
            "source_text_method": source_text_method,
            "source_reference_quality": source_reference_quality,
            "source_reference_reliable": source_reference_reliable,
            "source_selection_score": source_selection_score,
            "source_parser_alignment_to_candidate": source_parser_alignment,
            "source_selected_psm": source_selected_psm,
            "source_selected_rotation": source_selected_rotation,
            "source_selected_dpi": source_selected_dpi,
            "assessed_at": datetime.datetime.now().isoformat(),
        }

        page_payload.setdefault("page", {})
        page_payload["page"]["qa_page_machine_code"] = qa_page_machine_code
        page_payload["qa"] = qa_block
        parser_updated_pages.append((page_path, page_payload))

        page_row = {
            "page_id": page_id,
            "source_page_number": source_page_number,
            "qa_page_machine_code": qa_page_machine_code,
            "qa_status": qa_status,
            "qa_detail_reason": qa_detail_reason,
            "accuracy_score": accuracy_score,
            "char_similarity": char_similarity,
            "token_recall": token_recall,
            "token_precision": token_precision,
            "token_f1": token_f1,
            "numeric_token_recall": numeric_token_recall,
            "parser_text_norm": parser_text_norm,
            "parser_char_count": len(parser_text),
            "source_char_count": len(source_text),
            "source_comparable": source_comparable,
            "source_text_method": source_text_method,
            "source_reference_quality": source_reference_quality,
            "source_reference_reliable": source_reference_reliable,
            "source_selection_score": source_selection_score,
            "source_parser_alignment_to_candidate": source_parser_alignment,
            "source_selected_dpi": source_selected_dpi,
        }
        page_rows.append(page_row)

        qa_page_payloads.append(
            {
                "schema_version": "catalyst_qa_page_export.v1",
                "qa_run_id": qa_run_id,
                "parser_run_id": descriptor.parser_run_id,
                "page_id": page_id,
                "source_page_number": source_page_number,
                "qa_page_machine_code": qa_page_machine_code,
                "qa_status": qa_status,
                "qa_detail_reason": qa_detail_reason,
                "accuracy_score": accuracy_score,
                "char_similarity": char_similarity,
                "token_recall": token_recall,
                "token_precision": token_precision,
                "token_f1": token_f1,
                "numeric_token_recall": numeric_token_recall,
                "source_comparable": source_comparable,
                "source_text_method": source_text_method,
                "source_reference_quality": source_reference_quality,
                "source_reference_reliable": source_reference_reliable,
                "source_selection_score": source_selection_score,
                "source_parser_alignment_to_candidate": source_parser_alignment,
                "source_selected_psm": source_selected_psm,
                "source_selected_rotation": source_selected_rotation,
                "source_selected_dpi": source_selected_dpi,
                "source_pdf_path": str(descriptor.source_pdf_path) if descriptor.source_pdf_path else None,
            }
        )
    stage_timings["score_pages_seconds"] = round(time.perf_counter() - stage_started, 4)

    stage_started = time.perf_counter()
    for page_path, payload in parser_updated_pages:
        write_json(page_path, payload)
    with open(parser_machine_root / "pages.jsonl", "w", encoding="utf-8") as f:
        for _, payload in parser_updated_pages:
            f.write(json.dumps(payload) + "\n")
    stage_timings["write_parser_updates_seconds"] = round(time.perf_counter() - stage_started, 4)

    stage_started = time.perf_counter()
    source_page_numbers = {safe_int(row.get("source_page_number"), 0) for row in page_rows if safe_int(row.get("source_page_number"), 0) > 0}
    expected_page_numbers = set(range(1, parser_expected_page_count + 1)) if parser_expected_page_count > 0 else set()
    missing_pages = sorted(expected_page_numbers - source_page_numbers)
    duplicate_groups = find_duplicate_groups(page_rows, duplicate_min_chars)

    comparable_rows = [row for row in page_rows if row.get("source_comparable")]
    pass_rows = [row for row in comparable_rows if row.get("qa_status") == "pass"]
    warn_rows = [row for row in comparable_rows if row.get("qa_status") == "warn"]
    fail_rows = [row for row in comparable_rows if row.get("qa_status") == "fail"]
    unverifiable_rows = [row for row in page_rows if row.get("qa_status") == "unverifiable"]
    source_unavailable_rows = [row for row in page_rows if row.get("qa_status") == "source_unavailable"]
    comparable_count = len(comparable_rows)
    pass_ratio = round(len(pass_rows) / comparable_count, 4) if comparable_count else 0.0
    warn_or_better_ratio = round((len(pass_rows) + len(warn_rows)) / comparable_count, 4) if comparable_count else 0.0
    accepted_ratio = warn_or_better_ratio
    fail_ratio = round(len(fail_rows) / comparable_count, 4) if comparable_count else 1.0

    score_values = [float(row["accuracy_score"]) for row in comparable_rows if row.get("accuracy_score") is not None]
    score_stats = {
        "min_accuracy_score": round(min(score_values), 4) if score_values else None,
        "max_accuracy_score": round(max(score_values), 4) if score_values else None,
        "avg_accuracy_score": round(sum(score_values) / len(score_values), 4) if score_values else None,
    }
    source_text_method_counts = dict(source_method_counter)

    artifact_checks = {
        "parser_output_root_exists": descriptor.parser_output_root.exists(),
        "parser_machine_readable_exists": parser_machine_root.exists(),
        "parser_run_json_exists": parser_run_json_path.exists(),
        "parser_handoff_exists": parser_handoff_path.exists(),
        "parser_manifest_run_exists": (descriptor.parser_root / "manifests" / "runs" / f"{descriptor.parser_run_id}.json").exists(),
        "parser_logs_run_exists": (descriptor.parser_root / "logs" / "runs" / descriptor.parser_run_id).exists(),
    }
    missing_artifacts = sorted(key for key, present in artifact_checks.items() if not present)
    stage_timings["aggregate_metrics_seconds"] = round(time.perf_counter() - stage_started, 4)

    status_reason = "quality_failed"
    if not source_available:
        qa_status = "fail"
        status_reason = "source_unavailable"
    elif source_pdf_hash_match is False:
        qa_status = "fail"
        status_reason = "source_hash_mismatch"
    elif missing_pages:
        qa_status = "fail"
        status_reason = "page_coverage_gap"
    elif comparable_count < run_min_comparable_pages:
        qa_status = "warn"
        status_reason = "insufficient_comparable_pages"
    elif comparable_count == 0:
        qa_status = "warn"
        status_reason = "no_comparable_source_text"
    elif accepted_ratio >= run_accept_ratio and fail_ratio <= run_max_fail_ratio:
        qa_status = "pass"
        status_reason = "accepted_ratio_met"
    elif pass_ratio >= run_pass_ratio and not missing_artifacts:
        qa_status = "pass"
        status_reason = "pass_ratio_met"
    elif warn_or_better_ratio >= run_warn_ratio:
        qa_status = "warn"
        status_reason = "warn_ratio_met"
    else:
        qa_status = "fail"
        status_reason = "ratio_below_threshold"

    assessed_at = datetime.datetime.now().isoformat()
    run_summary = {
        "schema_version": "catalyst_qa_run_export.v1",
        "qa_run_id": qa_run_id,
        "parser_run_id": descriptor.parser_run_id,
        "status": qa_status,
        "status_reason": status_reason,
        "assessed_at": assessed_at,
        "parser_output_root": str(descriptor.parser_output_root),
        "parser_machine_readable_folder": str(descriptor.parser_machine_readable_folder),
        "source_pdf_path": str(descriptor.source_pdf_path) if descriptor.source_pdf_path else None,
        "source_available": source_available,
        "source_error": source_error,
        "source_pdf_hash_expected": descriptor.source_pdf_hash,
        "source_pdf_hash_actual": source_pdf_hash_actual,
        "source_pdf_hash_match": source_pdf_hash_match,
        "page_count": len(page_rows),
        "expected_page_count": parser_expected_page_count,
        "missing_pages": missing_pages,
        "comparable_page_count": comparable_count,
        "pass_page_count": len(pass_rows),
        "warn_page_count": len(warn_rows),
        "fail_page_count": len(fail_rows),
        "unverifiable_page_count": len(unverifiable_rows),
        "source_unavailable_page_count": len(source_unavailable_rows),
        "pass_ratio": pass_ratio,
        "warn_or_better_ratio": warn_or_better_ratio,
        "accepted_ratio": accepted_ratio,
        "fail_ratio": fail_ratio,
        "duplicate_group_count": len(duplicate_groups),
        "duplicate_groups": duplicate_groups,
        "score_stats": score_stats,
        "source_text_method_counts": source_text_method_counts,
        "source_short_text_page_count": source_short_text_count,
        "source_reference_unreliable_page_count": source_reference_unreliable_count,
        "source_ocr_fallback_enabled": source_ocr_fallback_enabled,
        "source_ocr_dpi": source_ocr_dpi,
        "source_ocr_dpi_primary": source_ocr_dpi_primary,
        "source_ocr_dpi_fallback": source_ocr_dpi_fallback,
        "source_ocr_adaptive_dpi_enabled": source_ocr_adaptive_dpi_enabled,
        "source_ocr_psm_candidates": source_ocr_psm_candidates,
        "source_ocr_rotation_candidates": source_ocr_rotation_candidates,
        "source_ocr_workers": source_ocr_workers,
        "source_ocr_worker_backend": source_ocr_worker_backend,
        "source_ocr_max_inflight_factor": source_ocr_max_inflight_factor,
        "source_ocr_candidate_cascade_enabled": source_ocr_candidate_cascade_enabled,
        "source_ocr_cascade_quality_threshold": source_ocr_cascade_quality_threshold,
        "source_ocr_cascade_alignment_threshold": source_ocr_cascade_alignment_threshold,
        "source_ocr_cascade_min_chars": source_ocr_cascade_min_chars,
        "source_ocr_timeout_seconds": source_ocr_timeout_seconds,
        "source_reference_min_quality": source_reference_min_quality,
        "source_ocr_parser_alignment_weight": source_ocr_parser_alignment_weight,
        "source_ocr_min_alignment_for_reliable": source_ocr_min_alignment_for_reliable,
        "source_extraction_stats": source_extraction_stats,
        "run_accept_ratio": run_accept_ratio,
        "run_max_fail_ratio": run_max_fail_ratio,
        "run_min_comparable_pages": run_min_comparable_pages,
        "artifact_checks": artifact_checks,
        "missing_artifacts": missing_artifacts,
        "runtime_metrics_from_parser": parser_run_payload.get("runtime_metrics"),
    }

    machine_root = base_dir / "work" / "runs" / qa_run_id / "machine_readable"
    pages_root = machine_root / "pages"
    stage_started = time.perf_counter()
    pages_root.mkdir(parents=True, exist_ok=True)
    for page_payload in qa_page_payloads:
        write_json(pages_root / f"{page_payload['page_id']}.json", page_payload)
    with open(machine_root / "pages.jsonl", "w", encoding="utf-8") as f:
        for payload in qa_page_payloads:
            f.write(json.dumps(payload) + "\n")

    handoff_payload = {
        "schema_version": "catalyst_qa_handoff.v1",
        "qa_run_id": qa_run_id,
        "parser_run_id": descriptor.parser_run_id,
        "status": qa_status,
        "status_reason": status_reason,
        "assessed_at": assessed_at,
        "page_count": len(page_rows),
        "comparable_page_count": comparable_count,
        "pass_page_count": len(pass_rows),
        "warn_page_count": len(warn_rows),
        "fail_page_count": len(fail_rows),
        "unverifiable_page_count": len(unverifiable_rows),
        "pass_ratio": pass_ratio,
        "warn_or_better_ratio": warn_or_better_ratio,
        "accepted_ratio": accepted_ratio,
        "fail_ratio": fail_ratio,
        "source_pdf_hash_match": source_pdf_hash_match,
        "missing_pages": missing_pages,
        "duplicate_group_count": len(duplicate_groups),
    }
    write_json(machine_root / "handoff.json", handoff_payload)
    stage_timings["write_qa_machine_outputs_seconds"] = round(time.perf_counter() - stage_started, 4)

    report_path = base_dir / "reports" / f"{qa_run_id}.md"
    stage_started = time.perf_counter()
    write_markdown_report(report_path, run_summary, page_rows)

    qa_outbox_root = base_dir / "outbox" / f"{qa_run_id}_{descriptor.parser_output_root.name}"
    if qa_outbox_root.exists():
        shutil.rmtree(qa_outbox_root)
    qa_outbox_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(machine_root, qa_outbox_root / "machine_readable")
    shutil.copy2(report_path, qa_outbox_root / "QA_REPORT.md")
    write_json(qa_outbox_root / "handoff.json", handoff_payload)
    (qa_outbox_root / "SUCCESS.txt").write_text(f"QA run {qa_run_id} completed with status {qa_status}.\n", encoding="utf-8")
    stage_timings["publish_qa_outbox_seconds"] = round(time.perf_counter() - stage_started, 4)

    stage_started = time.perf_counter()
    _update_parent_run_files(
        descriptor=descriptor,
        run_summary=run_summary,
        qa_output_root=qa_outbox_root,
        logger=logger,
    )
    stage_timings["sync_parser_outputs_seconds"] = round(time.perf_counter() - stage_started, 4)
    stage_timings["total_assessment_seconds"] = round(time.perf_counter() - assessment_started, 4)
    run_summary["qa_stage_timings"] = stage_timings

    for output_root in [
        machine_root,
        qa_outbox_root / "machine_readable",
        descriptor.parser_output_root / "qa" / qa_run_id / "machine_readable",
    ]:
        if not output_root.exists():
            continue
        write_json(output_root / "qa_run.json", run_summary)
        write_json(output_root / "run.json", run_summary)

    result = {
        "qa_run_id": qa_run_id,
        "status": qa_status,
        "status_reason": status_reason,
        "output_root": str(qa_outbox_root),
        "machine_readable_folder": str(qa_outbox_root / "machine_readable"),
        "report_path": str(qa_outbox_root / "QA_REPORT.md"),
        "summary": run_summary,
    }
    return result
