from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import cv2
import fitz
import numpy as np
import pytesseract
from PIL import Image
from pytesseract import Output

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler


pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
WORD_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'&/.-]*")
COMMON_OCR_PUNCTUATION = set(".,;:!?()[]{}'\"-_/\\&%$#@+=*")
WORD_WITNESS_SCHEMA_VERSION = "catalyst_word_witness.v1"
try:
    PIL_RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
except AttributeError:  # Pillow < 10 fallback
    PIL_RESAMPLE_LANCZOS = Image.LANCZOS


def alnum_count(text: str) -> int:
    return sum(1 for ch in (text or "") if ch.isalnum())


def calculate_quality_score(text: str) -> float:
    if not text:
        return 0.0
    clean_text = re.sub(r"[^a-zA-Z0-9\s.,!?;:()'\"-]", "", text)
    return len(clean_text) / len(text)


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def read_text_if_exists(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def load_grayscale_image(path: Path | None) -> np.ndarray | None:
    if path is None or not path.exists():
        return None
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    return image


def resize_image_max_dim(image: np.ndarray, max_dim: int) -> np.ndarray:
    if max_dim <= 0:
        return image
    height, width = image.shape[:2]
    current_max = max(height, width)
    if current_max <= max_dim:
        return image
    scale = max_dim / float(current_max)
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)


def resize_pil_image_max_dim(image: Image.Image, max_dim: int) -> Image.Image:
    if max_dim <= 0:
        return image
    width, height = image.size
    current_max = max(width, height)
    if current_max <= max_dim:
        return image
    scale = max_dim / float(current_max)
    resized = (
        max(1, int(round(width * scale))),
        max(1, int(round(height * scale))),
    )
    return image.resize(resized, resample=PIL_RESAMPLE_LANCZOS)


def rotate_cardinal_image(image: np.ndarray, angle: int) -> np.ndarray:
    normalized = angle % 360
    if normalized == 0:
        return image.copy()
    if normalized == 90:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    if normalized == 180:
        return cv2.rotate(image, cv2.ROTATE_180)
    if normalized == 270:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    raise ValueError(f"Unsupported cardinal angle: {angle}")


def build_ocr_metrics(text: str) -> dict:
    raw_text = text or ""
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    tokens = WORD_TOKEN_PATTERN.findall(raw_text)
    lexical_tokens = [token for token in tokens if len(token) >= 4 and re.search(r"[A-Za-z]", token) and re.search(r"[AEIOUYaeiouy]", token)]
    numeric_tokens = [token for token in tokens if any(ch.isdigit() for ch in token)]
    short_tokens = [token for token in tokens if len(token) <= 2]
    printable_nonspace_chars = [ch for ch in raw_text if ch.isprintable() and not ch.isspace()]
    noise_char_count = sum(1 for ch in printable_nonspace_chars if not ch.isalnum() and ch not in COMMON_OCR_PUNCTUATION)
    alpha_chars = sum(1 for ch in raw_text if ch.isalpha())
    digit_chars = sum(1 for ch in raw_text if ch.isdigit())
    printable_count = len(printable_nonspace_chars)
    token_count = len(tokens)
    lexical_ratio = len(lexical_tokens) / token_count if token_count else 0.0
    short_word_ratio = len(short_tokens) / token_count if token_count else 0.0
    noise_ratio = noise_char_count / printable_count if printable_count else 0.0
    avg_word_length = (sum(len(token) for token in tokens) / token_count) if token_count else 0.0
    quality_score = 0.0
    quality_score += min(0.35, alnum_count(raw_text) / 320.0)
    quality_score += min(0.2, token_count / 40.0)
    quality_score += min(0.2, len(lexical_tokens) / 20.0)
    quality_score += max(0.0, 0.15 * (1.0 - min(1.0, noise_ratio)))
    quality_score += min(0.1, len(lines) / 20.0)
    return {
        "alnum_count": alnum_count(raw_text),
        "word_count": token_count,
        "lexical_word_count": len(lexical_tokens),
        "numeric_token_count": len(numeric_tokens),
        "line_count": len(lines),
        "avg_word_length": round(avg_word_length, 2),
        "short_word_ratio": round(short_word_ratio, 4),
        "alpha_char_count": alpha_chars,
        "digit_char_count": digit_chars,
        "noise_char_count": noise_char_count,
        "noise_ratio": round(noise_ratio, 4),
        "lexical_ratio": round(lexical_ratio, 4),
        "quality_score": round(min(0.99, quality_score), 4),
    }


def ocr_candidate_selection_score(metrics: dict, favor_numeric: bool = False) -> float:
    score = float(metrics["alnum_count"])
    score += float(metrics["word_count"]) * 4.0
    score += float(metrics["lexical_word_count"]) * 18.0
    score += float(metrics["numeric_token_count"]) * (2.0 if favor_numeric else 0.75)
    score += float(metrics["line_count"]) * 1.5
    score += float(metrics["avg_word_length"]) * 6.0
    score += float(metrics["lexical_ratio"]) * 120.0
    score += max(0.0, (1.0 - float(metrics["noise_ratio"]))) * 20.0
    score -= float(metrics["short_word_ratio"]) * 80.0
    score -= float(metrics["noise_char_count"]) * 4.0
    return round(score, 2)


def stabilize_ocr_candidate_choice(candidates: list[dict], best_candidate: dict) -> dict:
    if int(best_candidate["rotation"]) == 0:
        return best_candidate

    base_candidates = [candidate for candidate in candidates if int(candidate["rotation"]) == 0]
    if not base_candidates:
        return best_candidate

    readable_base = max(
        base_candidates,
        key=lambda item: (
            item["metrics"]["lexical_word_count"],
            item["metrics"]["lexical_ratio"],
            item["metrics"]["alnum_count"],
            item["selection_score"],
        ),
    )
    if (
        float(best_candidate["metrics"]["lexical_ratio"]) < 0.12
        and float(best_candidate["metrics"]["short_word_ratio"]) > 0.45
        and int(readable_base["metrics"]["lexical_word_count"]) >= 12
        and float(readable_base["metrics"]["lexical_ratio"]) >= 0.35
        and int(readable_base["metrics"]["alnum_count"]) >= int(best_candidate["metrics"]["alnum_count"] * 1.15)
    ):
        return readable_base
    return best_candidate


def summarize_ocr_candidate(candidate: dict) -> dict:
    return {
        "variant": candidate["variant"],
        "rotation": candidate["rotation"],
        "config": candidate["config"],
        "selection_score": candidate["selection_score"],
        "metrics": candidate["metrics"],
    }


def normalize_bbox(x: float, y: float, width: float, height: float) -> list[float]:
    return [
        round(float(x), 2),
        round(float(y), 2),
        round(float(width), 2),
        round(float(height), 2),
    ]


def build_line_entries(line_groups: dict[str, dict]) -> list[dict]:
    lines: list[dict] = []
    for line_id, line_payload in line_groups.items():
        words = sorted(line_payload["words"], key=lambda item: item["reading_order"])
        if not words:
            continue
        x0 = min(word["bbox"][0] for word in words)
        y0 = min(word["bbox"][1] for word in words)
        x1 = max(word["bbox"][0] + word["bbox"][2] for word in words)
        y1 = max(word["bbox"][1] + word["bbox"][3] for word in words)
        lines.append(
            {
                "line_id": line_id,
                "block_id": line_payload["block_id"],
                "text": " ".join(word["text"] for word in words).strip(),
                "bbox": normalize_bbox(x0, y0, x1 - x0, y1 - y0),
                "word_ids": [word["word_id"] for word in words],
                "reading_order": words[0]["reading_order"],
            }
        )
    lines.sort(key=lambda item: item["reading_order"])
    return lines


def build_text_from_lines(lines: list[dict]) -> str:
    return "\n".join(line["text"].strip() for line in lines if str(line.get("text") or "").strip()).strip()


def extract_ocr_words_from_data(data: dict) -> list[dict]:
    words: list[dict] = []
    total = len(data.get("text", []))
    for index in range(total):
        token = str(data["text"][index] or "").strip()
        if not token:
            continue
        left = safe_int(data["left"][index])
        top = safe_int(data["top"][index])
        width = safe_int(data["width"][index])
        height = safe_int(data["height"][index])
        if width <= 0 or height <= 0:
            continue
        block_num = safe_int(data["block_num"][index])
        par_num = safe_int(data["par_num"][index])
        line_num = safe_int(data["line_num"][index])
        word_num = safe_int(data["word_num"][index])
        block_id = f"b{block_num:04d}"
        line_id = f"{block_id}_p{par_num:04d}_l{line_num:04d}"
        word_id = f"{line_id}_w{word_num:04d}"
        confidence = safe_float(data["conf"][index], -1.0)
        words.append(
            {
                "word_id": word_id,
                "text": token,
                "bbox": normalize_bbox(left, top, width, height),
                "confidence": round(confidence, 2) if confidence >= 0.0 else None,
                "block_id": block_id,
                "line_id": line_id,
                "reading_order": len(words),
            }
        )
    return words


def build_tesseract_line_entries(words: list[dict]) -> list[dict]:
    line_groups: dict[str, dict] = {}
    for word in words:
        line_groups.setdefault(word["line_id"], {"block_id": word["block_id"], "words": []})
        line_groups[word["line_id"]]["words"].append(word)
    return build_line_entries(line_groups)


def split_words_into_segments(words: list[dict], page_width: float, extraction: dict | None = None) -> list[list[dict]]:
    if not words:
        return []

    settings = extraction or {}
    widths = sorted(float(word["bbox"][2]) for word in words)
    median_width = widths[len(widths) // 2] if widths else 0.0
    gap_threshold = max(
        float(settings.get("ocr_geometry_min_gap_pixels", 80.0)),
        median_width * float(settings.get("ocr_geometry_large_gap_ratio", 3.0)),
        float(page_width) * float(settings.get("ocr_geometry_page_gap_ratio", 0.08)),
    )

    segments: list[list[dict]] = [[words[0]]]
    for word in words[1:]:
        previous = segments[-1][-1]
        previous_right = float(previous["bbox"][0]) + float(previous["bbox"][2])
        gap = float(word["bbox"][0]) - previous_right
        if gap >= gap_threshold:
            segments.append([word])
        else:
            segments[-1].append(word)
    return segments


def build_geometry_line_entries(words: list[dict], page_width: float, extraction: dict | None = None) -> tuple[list[dict], list[dict]]:
    if not words:
        return [], []

    settings = extraction or {}
    row_tolerance_ratio = float(settings.get("ocr_geometry_row_tolerance_ratio", 0.7))
    sorted_words = sorted(
        words,
        key=lambda item: (
            round(float(item["bbox"][1]) + (float(item["bbox"][3]) / 2.0), 2),
            round(float(item["bbox"][0]), 2),
        ),
    )

    rows: list[dict] = []
    for word in sorted_words:
        x, y, width, height = [float(value) for value in word["bbox"]]
        center_y = y + (height / 2.0)
        best_row = None
        best_delta = None
        for row in rows:
            tolerance = max(float(row["avg_height"]), height) * row_tolerance_ratio
            if center_y < row["y0"] - tolerance or center_y > row["y1"] + tolerance:
                continue
            delta = abs(center_y - row["center_y"])
            if best_delta is None or delta < best_delta:
                best_row = row
                best_delta = delta
        if best_row is None:
            rows.append(
                {
                    "words": [word],
                    "y0": y,
                    "y1": y + height,
                    "center_y": center_y,
                    "avg_height": height,
                    "x0": x,
                }
            )
            continue
        existing_count = len(best_row["words"])
        best_row["words"].append(word)
        best_row["y0"] = min(best_row["y0"], y)
        best_row["y1"] = max(best_row["y1"], y + height)
        best_row["center_y"] = ((best_row["center_y"] * existing_count) + center_y) / float(existing_count + 1)
        best_row["avg_height"] = ((best_row["avg_height"] * existing_count) + height) / float(existing_count + 1)
        best_row["x0"] = min(best_row["x0"], x)

    rows.sort(key=lambda row: (round(float(row["center_y"]), 2), round(float(row["x0"]), 2)))

    rebuilt_words: list[dict] = []
    rebuilt_lines: list[dict] = []
    reading_order = 0

    for row_index, row in enumerate(rows, start=1):
        row_words = sorted(row["words"], key=lambda item: (float(item["bbox"][0]), float(item["bbox"][1])))
        segments = split_words_into_segments(row_words, page_width, settings)
        block_id = f"g{row_index:04d}"
        for segment_index, segment_words in enumerate(segments, start=1):
            if not segment_words:
                continue
            line_id = f"{block_id}_l{segment_index:04d}"
            line_word_ids: list[str] = []
            for word_index, word in enumerate(segment_words, start=1):
                updated_word = dict(word)
                updated_word["source_word_id"] = word.get("word_id")
                updated_word["source_line_id"] = word.get("line_id")
                updated_word["block_id"] = block_id
                updated_word["line_id"] = line_id
                updated_word["word_id"] = f"{line_id}_w{word_index:04d}"
                updated_word["reading_order"] = reading_order
                rebuilt_words.append(updated_word)
                line_word_ids.append(updated_word["word_id"])
                reading_order += 1

            x0 = min(float(item["bbox"][0]) for item in segment_words)
            y0 = min(float(item["bbox"][1]) for item in segment_words)
            x1 = max(float(item["bbox"][0]) + float(item["bbox"][2]) for item in segment_words)
            y1 = max(float(item["bbox"][1]) + float(item["bbox"][3]) for item in segment_words)
            rebuilt_lines.append(
                {
                    "line_id": line_id,
                    "block_id": block_id,
                    "text": " ".join(str(item["text"]).strip() for item in segment_words if str(item["text"]).strip()).strip(),
                    "bbox": normalize_bbox(x0, y0, x1 - x0, y1 - y0),
                    "word_ids": line_word_ids,
                    "reading_order": rebuilt_words[-len(segment_words)]["reading_order"],
                }
            )

    return rebuilt_words, rebuilt_lines


def build_ocr_word_witness_from_words(
    words: list[dict],
    page_size: list[float],
    source_variant: str,
    line_strategy: str = "tesseract",
    extraction: dict | None = None,
) -> dict:
    working_words = [dict(word) for word in words]
    if line_strategy == "y_overlap":
        working_words, lines = build_geometry_line_entries(working_words, float(page_size[0]), extraction)
    else:
        lines = build_tesseract_line_entries(working_words)

    return {
        "schema_version": WORD_WITNESS_SCHEMA_VERSION,
        "engine": "tesseract_v5",
        "coordinate_space": "normalized_image_pixels",
        "page_size": [int(page_size[0]), int(page_size[1])],
        "source_variant": source_variant,
        "line_strategy": line_strategy,
        "word_count": len(working_words),
        "line_count": len(lines),
        "words": working_words,
        "lines": lines,
    }


def build_empty_word_witness(engine: str, coordinate_space: str, page_size: list[float], source_variant: str) -> dict:
    return {
        "schema_version": WORD_WITNESS_SCHEMA_VERSION,
        "engine": engine,
        "coordinate_space": coordinate_space,
        "page_size": page_size,
        "source_variant": source_variant,
        "line_strategy": "empty",
        "word_count": 0,
        "line_count": 0,
        "words": [],
        "lines": [],
    }


def extract_native_word_witness(page_pdf_path: Path) -> dict:
    doc = fitz.open(page_pdf_path)
    try:
        page = doc[0]
        rect = page.rect
        page_size = [round(float(rect.width), 2), round(float(rect.height), 2)]
        raw_words = sorted(
            page.get_text("words"),
            key=lambda item: (
                safe_int(item[5]),
                safe_int(item[6]),
                safe_int(item[7]),
                safe_float(item[1]),
                safe_float(item[0]),
            ),
        )
        words: list[dict] = []
        line_groups: dict[str, dict] = {}
        for reading_order, item in enumerate(raw_words):
            x0, y0, x1, y1, text, block_no, line_no, word_no = item[:8]
            token = str(text or "").strip()
            if not token:
                continue
            block_id = f"b{safe_int(block_no):04d}"
            line_id = f"{block_id}_l{safe_int(line_no):04d}"
            word_id = f"{line_id}_w{safe_int(word_no):04d}"
            word_entry = {
                "word_id": word_id,
                "text": token,
                "bbox": normalize_bbox(x0, y0, float(x1) - float(x0), float(y1) - float(y0)),
                "confidence": 1.0,
                "block_id": block_id,
                "line_id": line_id,
                "reading_order": reading_order,
            }
            words.append(word_entry)
            line_groups.setdefault(line_id, {"block_id": block_id, "words": []})
            line_groups[line_id]["words"].append(word_entry)
        lines = build_line_entries(line_groups)
        return {
            "schema_version": WORD_WITNESS_SCHEMA_VERSION,
            "engine": "native_pymupdf",
            "coordinate_space": "pdf_points",
            "page_size": page_size,
            "source_variant": "native_text",
            "line_strategy": "native_pdf",
            "word_count": len(words),
            "line_count": len(lines),
            "words": words,
            "lines": lines,
        }
    finally:
        doc.close()


def extract_ocr_word_witness(
    image: Image.Image,
    config: str | None,
    source_variant: str,
    line_strategy: str = "tesseract",
    extraction: dict | None = None,
    output_page_size: list[float] | tuple[float, float] | None = None,
) -> dict:
    ocr_kwargs = {"output_type": Output.DICT}
    if config:
        ocr_kwargs["config"] = config
    data = pytesseract.image_to_data(image, **ocr_kwargs)
    words = extract_ocr_words_from_data(data)
    page_size = [float(image.size[0]), float(image.size[1])]
    if output_page_size and len(output_page_size) == 2:
        output_width = float(output_page_size[0])
        output_height = float(output_page_size[1])
        if output_width > 0 and output_height > 0 and (
            abs(output_width - page_size[0]) > 0.5 or abs(output_height - page_size[1]) > 0.5
        ):
            scale_x = output_width / page_size[0]
            scale_y = output_height / page_size[1]
            for word in words:
                bbox = word.get("bbox", [0.0, 0.0, 0.0, 0.0])
                if len(bbox) != 4:
                    continue
                x, y, width, height = [float(value) for value in bbox]
                word["bbox"] = normalize_bbox(x * scale_x, y * scale_y, width * scale_x, height * scale_y)
            page_size = [output_width, output_height]
    return build_ocr_word_witness_from_words(
        words,
        page_size=page_size,
        source_variant=source_variant,
        line_strategy=line_strategy,
        extraction=extraction,
    )


def make_ocr_candidate(
    image: Image.Image,
    variant: str,
    rotation: int,
    config: str | None,
    favor_numeric: bool,
    output_page_size: list[float] | tuple[float, float] | None = None,
) -> dict:
    witness = extract_ocr_word_witness(
        image,
        config,
        variant,
        line_strategy="tesseract",
        output_page_size=output_page_size,
    )
    text = build_text_from_lines(witness["lines"])
    metrics = build_ocr_metrics(text)
    return {
        "variant": variant,
        "rotation": rotation,
        "config": config or "",
        "text": text,
        "metrics": metrics,
        "selection_score": ocr_candidate_selection_score(metrics, favor_numeric=favor_numeric),
        "ocr_words": witness["words"],
        "page_size": witness["page_size"],
    }


def select_best_ocr_candidate(candidates: list[dict]) -> tuple[dict, float]:
    ranked = sorted(
        candidates,
        key=lambda item: (
            item["selection_score"],
            item["metrics"]["quality_score"],
            item["metrics"]["alnum_count"],
            item["metrics"]["lexical_word_count"],
        ),
        reverse=True,
    )
    best = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else None
    margin = best["selection_score"] - (runner_up["selection_score"] if runner_up else 0.0)
    return best, round(margin, 2)


def resolve_cardinal_orientation(
    image: np.ndarray,
    native_detected: bool,
    dark_ratio: float,
    extraction: dict,
) -> tuple[np.ndarray, dict]:
    pre_blank_floor = float(extraction.get("cardinal_orientation_blank_dark_ratio_floor", 0.001))
    if dark_ratio <= pre_blank_floor:
        return image.copy(), {
            "applied_angle": 0,
            "source": "blank_skip",
            "triggered": False,
            "candidate_skews": [],
            "ocr_candidates": [],
            "selection_margin": 0.0,
        }

    projection_max_dim = int(extraction.get("cardinal_orientation_projection_max_dim", 1400))
    projection_trigger_margin = float(extraction.get("cardinal_orientation_projection_trigger_margin", 10.0))
    use_ocr_confirmation = bool(extraction.get("cardinal_orientation_use_ocr_confirmation", True))
    base_projection_image = resize_image_max_dim(image, projection_max_dim)
    base_candidate = {
        "angle": 0,
        "abs_skew": round(abs(calculate_skew_angle_from_image(image)), 2),
        "projection_score": round(projection_profile_score(base_projection_image), 4),
    }
    candidates = [base_candidate]
    for angle in (90, 180, 270):
        rotated = rotate_cardinal_image(image, angle)
        reduced = resize_image_max_dim(rotated, projection_max_dim)
        candidates.append(
            {
                "angle": angle,
                "abs_skew": round(abs(calculate_skew_angle_from_image(rotated)), 2),
                "projection_score": round(projection_profile_score(reduced), 4),
            }
        )

    best_abs_skew = min(candidate["abs_skew"] for candidate in candidates)
    base_abs_skew = base_candidate["abs_skew"]
    base_projection_score = float(base_candidate["projection_score"])
    best_projection_score = max(float(candidate["projection_score"]) for candidate in candidates)
    trigger_skew_threshold = float(extraction.get("cardinal_orientation_trigger_skew_threshold", 6.0))
    skew_margin = float(extraction.get("cardinal_orientation_skew_margin", 2.5))
    score_margin = float(extraction.get("cardinal_orientation_score_margin", 18.0))
    low_quality_threshold = float(extraction.get("ocr_witness_low_quality_threshold", 0.22))
    skew_triggered = best_abs_skew + 0.5 < base_abs_skew or base_abs_skew >= trigger_skew_threshold
    projection_triggered = best_projection_score >= base_projection_score + projection_trigger_margin

    should_trigger = (
        not native_detected
        and dark_ratio > pre_blank_floor
        and (skew_triggered or projection_triggered)
    )
    if not should_trigger:
        return image.copy(), {
            "applied_angle": 0,
            "source": "base",
            "triggered": False,
            "candidate_skews": candidates,
            "ocr_candidates": [],
        }

    if not use_ocr_confirmation:
        fast_candidates = [candidate for candidate in candidates if int(candidate["angle"]) in (0, 90, 270)]
        base_fast = next((candidate for candidate in fast_candidates if int(candidate["angle"]) == 0), base_candidate)
        best_fast = max(
            fast_candidates,
            key=lambda item: (
                float(item["projection_score"]),
                -float(item["abs_skew"]),
            ),
        )
        projection_gain = float(best_fast["projection_score"]) - float(base_fast["projection_score"])
        skew_gain = float(base_fast["abs_skew"]) - float(best_fast["abs_skew"])
        apply_fast = (
            int(best_fast["angle"]) != 0
            and (projection_gain >= projection_trigger_margin or skew_gain >= skew_margin)
        )
        applied_angle = int(best_fast["angle"]) if apply_fast else 0
        return rotate_cardinal_image(image, applied_angle), {
            "applied_angle": applied_angle,
            "source": "projection_skew_fastpath" if apply_fast else "base",
            "triggered": True,
            "candidate_skews": candidates,
            "ocr_candidates": [],
            "selection_margin": round(max(projection_gain, skew_gain), 2),
        }

    ocr_angles = {0}
    ocr_angles.update(candidate["angle"] for candidate in candidates if candidate["abs_skew"] <= best_abs_skew + skew_margin)
    if projection_triggered:
        ocr_angles.update(
            candidate["angle"]
            for candidate in candidates
            if float(candidate["projection_score"]) >= best_projection_score - (projection_trigger_margin / 2.0)
        )
    reduced_max_dim = int(extraction.get("cardinal_orientation_ocr_max_dim", 1400))
    ocr_candidates: list[dict] = []
    for angle in sorted(ocr_angles):
        oriented = rotate_cardinal_image(image, angle)
        reduced = resize_image_max_dim(oriented, reduced_max_dim)
        reduced_pil = Image.fromarray(reduced)
        candidate = make_ocr_candidate(
            reduced_pil,
            variant=f"cardinal_{angle}_psm6",
            rotation=angle,
            config="--psm 6",
            favor_numeric=False,
        )
        ocr_candidates.append(candidate)

    best_candidate, selection_margin = select_best_ocr_candidate(ocr_candidates)
    base_metrics = next((candidate["metrics"] for candidate in ocr_candidates if candidate["rotation"] == 0), build_ocr_metrics(""))
    choose_best = (
        best_candidate["rotation"] != 0
        and (
            best_candidate["selection_score"] >= next(
                (candidate["selection_score"] for candidate in ocr_candidates if candidate["rotation"] == 0),
                0.0,
            )
            + score_margin
            or float(base_metrics["quality_score"]) < low_quality_threshold
        )
    )
    applied_angle = int(best_candidate["rotation"]) if choose_best else 0
    return rotate_cardinal_image(image, applied_angle), {
        "applied_angle": applied_angle,
        "source": "ocr_quality" if choose_best else "base",
        "triggered": True,
        "candidate_skews": candidates,
        "ocr_candidates": [summarize_ocr_candidate(candidate) for candidate in ocr_candidates],
        "selection_margin": selection_margin,
    }


def render_page_pdf(page_pdf_path: Path, render_dpi: int) -> tuple[str, np.ndarray]:
    doc = fitz.open(page_pdf_path)
    try:
        page = doc[0]
        native_text = page.get_text("text").strip()
        zoom = render_dpi / 72
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
        image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
        return native_text, image
    finally:
        doc.close()


def calculate_skew_angle_from_image(image: np.ndarray) -> float:
    _, thresh = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    coords = np.column_stack(np.where(thresh > 0))
    if coords.size == 0:
        return 0.0
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
    if abs(angle) > 45:
        if angle > 0:
            angle = angle - 90
        else:
            angle = angle + 90
    return float(angle)


def rotate_image(image: np.ndarray, angle: float) -> np.ndarray:
    if abs(angle) < 0.1:
        return image.copy()
    (height, width) = image.shape[:2]
    center = (width / 2.0, height / 2.0)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    cos = abs(matrix[0, 0])
    sin = abs(matrix[0, 1])
    bound_width = int(round((height * sin) + (width * cos)))
    bound_height = int(round((height * cos) + (width * sin)))
    matrix[0, 2] += (bound_width / 2.0) - center[0]
    matrix[1, 2] += (bound_height / 2.0) - center[1]
    return cv2.warpAffine(
        image,
        matrix,
        (bound_width, bound_height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,
    )


def projection_profile_score(image: np.ndarray) -> float:
    _, thresh = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    foreground = float(cv2.countNonZero(thresh))
    if foreground <= 0.0:
        return 0.0

    row_density = np.count_nonzero(thresh, axis=1).astype(np.float32) / max(1.0, float(thresh.shape[1]))
    col_density = np.count_nonzero(thresh, axis=0).astype(np.float32) / max(1.0, float(thresh.shape[0]))
    if row_density.size < 3 or col_density.size < 3:
        return 0.0

    row_diff = np.diff(row_density)
    col_diff = np.diff(col_density)
    row_variance = float(np.var(row_density))
    col_variance = float(np.var(col_density))
    row_transition = float(np.mean(np.abs(row_diff)))
    col_transition = float(np.mean(np.abs(col_diff)))
    row_peak = max(0.0, float(np.percentile(row_density, 95) - np.percentile(row_density, 50)))

    score = (row_variance * 1500.0) + (row_transition * 900.0) + (row_peak * 700.0)
    score -= (col_variance * 500.0) + (col_transition * 250.0)
    return round(score, 4)


def generate_angle_range(center: float, window: float, step: float, max_abs_angle: float) -> list[float]:
    if step <= 0:
        return [round(max(-max_abs_angle, min(max_abs_angle, center)), 4)]

    angles = {0.0}
    current = center - window
    while current <= center + window + (step / 2.0):
        clamped = max(-max_abs_angle, min(max_abs_angle, current))
        angles.add(round(clamped, 4))
        current += step
    angles.add(round(max(-max_abs_angle, min(max_abs_angle, center)), 4))
    return sorted(angles)


def evaluate_rotation_candidates(image: np.ndarray, angles: list[float]) -> list[dict]:
    candidates: list[dict] = []
    for angle in angles:
        rotated = rotate_image(image, angle)
        candidates.append(
            {
                "angle": round(float(angle), 4),
                "projection_score": projection_profile_score(rotated),
            }
        )
    return sorted(candidates, key=lambda item: (item["projection_score"], -abs(item["angle"])), reverse=True)


def refine_skew_angle(image: np.ndarray, coarse_angle: float, dark_ratio: float, extraction: dict) -> dict:
    max_abs_angle = float(extraction.get("geometry_refine_max_angle_degrees", 18.0))
    scoring_max_dim = int(extraction.get("geometry_refine_scoring_max_dim", 1600))
    coarse_window = float(extraction.get("geometry_refine_coarse_window_degrees", 6.0))
    coarse_step = float(extraction.get("geometry_refine_coarse_step_degrees", 1.0))
    fine_window = float(extraction.get("geometry_refine_fine_window_degrees", 1.25))
    fine_step = float(extraction.get("geometry_refine_fine_step_degrees", 0.25))
    min_score_improvement = float(extraction.get("geometry_refine_min_score_improvement", 0.5))
    skip_dark_ratio = float(extraction.get("geometry_refine_blank_dark_ratio_floor", 0.001))
    skip_abs_angle = float(extraction.get("geometry_refine_skip_below_abs_angle", 0.4))

    if dark_ratio <= skip_dark_ratio:
        return {
            "applied_angle": 0.0,
            "coarse_angle": round(float(coarse_angle), 4),
            "base_score": 0.0,
            "best_score": 0.0,
            "coarse_candidates": [],
            "fine_candidates": [],
            "source": "blank_skip",
        }

    if abs(float(coarse_angle)) < skip_abs_angle:
        return {
            "applied_angle": 0.0,
            "coarse_angle": round(float(coarse_angle), 4),
            "base_score": 0.0,
            "best_score": 0.0,
            "coarse_candidates": [],
            "fine_candidates": [],
            "source": "coarse_skip",
        }

    reduced = resize_image_max_dim(image, scoring_max_dim)
    base_score = projection_profile_score(reduced)
    coarse_angles = generate_angle_range(float(coarse_angle), coarse_window, coarse_step, max_abs_angle)
    coarse_candidates = evaluate_rotation_candidates(reduced, coarse_angles)
    coarse_best = coarse_candidates[0] if coarse_candidates else {"angle": 0.0, "projection_score": base_score}

    fine_angles = generate_angle_range(float(coarse_best["angle"]), fine_window, fine_step, max_abs_angle)
    fine_candidates = evaluate_rotation_candidates(reduced, fine_angles)
    fine_best = fine_candidates[0] if fine_candidates else coarse_best

    should_apply = (
        abs(float(fine_best["angle"])) >= 0.1
        and float(fine_best["projection_score"]) >= float(base_score) + min_score_improvement
    )
    applied_angle = float(fine_best["angle"]) if should_apply else 0.0
    return {
        "applied_angle": round(applied_angle, 4),
        "coarse_angle": round(float(coarse_angle), 4),
        "base_score": round(float(base_score), 4),
        "best_score": round(float(fine_best["projection_score"]), 4),
        "coarse_candidates": coarse_candidates[:24],
        "fine_candidates": fine_candidates[:24],
        "source": "projection_profile" if should_apply else "coarse_only",
    }


def normalize_page_geometry(image: np.ndarray, native_detected: bool, dark_ratio: float, extraction: dict) -> tuple[np.ndarray, dict]:
    oriented_image, cardinal_orientation = resolve_cardinal_orientation(
        image,
        native_detected=native_detected,
        dark_ratio=dark_ratio,
        extraction=extraction,
    )
    coarse_skew_angle = calculate_skew_angle_from_image(oriented_image)
    refinement = refine_skew_angle(oriented_image, coarse_skew_angle, dark_ratio, extraction)
    normalized_image = rotate_image(oriented_image, float(refinement.get("applied_angle", 0.0)))

    pass_summaries = [
        {
            "pass_index": 1,
            "applied_angle": refinement.get("applied_angle", 0.0),
            "source": refinement.get("source"),
            "base_score": refinement.get("base_score", 0.0),
            "best_score": refinement.get("best_score", 0.0),
        }
    ]

    residual_probe_angle = calculate_skew_angle_from_image(normalized_image)
    max_passes = max(1, int(extraction.get("geometry_refine_max_passes", 2)))
    residual_threshold = float(extraction.get("geometry_refine_residual_threshold", 0.9))
    total_applied_angle = float(refinement.get("applied_angle", 0.0))
    current_image = normalized_image

    for pass_index in range(2, max_passes + 1):
        if abs(float(residual_probe_angle)) < residual_threshold:
            break
        residual_refinement = refine_skew_angle(current_image, residual_probe_angle, dark_ratio, extraction)
        residual_angle = float(residual_refinement.get("applied_angle", 0.0))
        pass_summaries.append(
            {
                "pass_index": pass_index,
                "applied_angle": residual_angle,
                "source": residual_refinement.get("source"),
                "base_score": residual_refinement.get("base_score", 0.0),
                "best_score": residual_refinement.get("best_score", 0.0),
            }
        )
        if abs(residual_angle) < 0.1:
            break
        current_image = rotate_image(current_image, residual_angle)
        total_applied_angle += residual_angle
        residual_probe_angle = calculate_skew_angle_from_image(current_image)

    final_residual_probe_angle = calculate_skew_angle_from_image(current_image)

    return current_image, {
        "cardinal_orientation": cardinal_orientation,
        "coarse_skew_angle": round(float(coarse_skew_angle), 4),
        "applied_skew_angle": round(float(total_applied_angle), 4),
        "residual_skew_angle": round(float(final_residual_probe_angle), 4),
        "residual_skew_probe_angle": round(float(final_residual_probe_angle), 4),
        "refinement": refinement,
        "pass_summaries": pass_summaries,
    }


def detect_handwriting_confidence(image: np.ndarray, threshold_image: np.ndarray | None = None) -> float:
    edges = cv2.Canny(image, 100, 200)
    edge_density = cv2.countNonZero(edges) / (image.shape[0] * image.shape[1])
    if threshold_image is None:
        _, thresh = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    else:
        thresh = threshold_image
    cnts, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return 0.0
    avg_area = sum(cv2.contourArea(c) for c in cnts) / len(cnts)
    confidence = 0.0
    if edge_density > 0.05:
        confidence += 0.4
    if avg_area < 500:
        confidence += 0.4
    return min(1.0, confidence)


def detect_text_regions(image: np.ndarray, threshold_image: np.ndarray | None = None) -> list[dict]:
    if threshold_image is None:
        _, thresh = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    else:
        thresh = threshold_image
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 5))
    dilate = cv2.dilate(thresh, kernel, iterations=3)
    cnts = cv2.findContours(dilate, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cnts = cnts[0] if len(cnts) == 2 else cnts[1]
    regions = []
    for contour in cnts:
        x, y, w, h = cv2.boundingRect(contour)
        if w > 20 and h > 10:
            regions.append({"type": "text_block", "bbox": [x, y, w, h], "confidence": 1.0})
    return regions


def detect_table_regions(image: np.ndarray, threshold_image: np.ndarray | None = None) -> list[dict]:
    if threshold_image is None:
        _, thresh = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    else:
        thresh = threshold_image
    horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
    horiz_lines = cv2.erode(thresh, horiz_kernel, iterations=1)
    horiz_lines = cv2.dilate(horiz_lines, horiz_kernel, iterations=1)
    vert_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40))
    vert_lines = cv2.erode(thresh, vert_kernel, iterations=1)
    vert_lines = cv2.dilate(vert_lines, vert_kernel, iterations=1)
    grid_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (10, 10))
    grid_structure = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, grid_kernel)
    table_mask = cv2.add(horiz_lines, vert_lines)
    table_mask = cv2.add(table_mask, grid_structure)
    cnts = cv2.findContours(table_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cnts = cnts[0] if len(cnts) == 2 else cnts[1]
    regions = []
    for contour in cnts:
        x, y, w, h = cv2.boundingRect(contour)
        area = w * h
        if (w > 100 and h > 50) or (area > 5000 and w > 50):
            regions.append({"type": "table_region", "bbox": [x, y, w, h], "confidence": 0.8})
    return regions


def ocr_image(image: Image.Image, config: str | None = None) -> str:
    if config:
        return pytesseract.image_to_string(image, config=config)
    return pytesseract.image_to_string(image)


def should_try_rotation(table_regions: int, text_regions: int, skew_angle: float, base_metrics: dict, extraction: dict) -> bool:
    base_alnum = int(base_metrics["alnum_count"])
    low_text_threshold = int(extraction.get("ocr_retry_alnum_threshold", 24))
    skew_retry_alnum_threshold = int(extraction.get("ocr_retry_skew_alnum_threshold", 320))
    quality_threshold = float(extraction.get("ocr_retry_quality_threshold", 0.24))
    word_threshold = int(extraction.get("ocr_retry_word_threshold", 4))
    sparse_region_threshold = int(extraction.get("ocr_retry_sparse_region_threshold", 12))
    high_skew = abs(float(skew_angle)) >= float(extraction.get("ocr_retry_skew_threshold", 8.0))
    fragmented_layout = (table_regions + text_regions) >= sparse_region_threshold
    
    # Early Exit: If the base OCR is sparse but very high quality and low skew,
    # assume the sparsity is real (e.g. a clean separator or cover) and skip retries.
    if (
        not high_skew 
        and float(base_metrics["quality_score"]) >= float(extraction.get("ocr_retry_early_exit_quality_threshold", 0.82))
        and base_alnum <= skew_retry_alnum_threshold
    ):
        return False

    weak_base = (
        base_alnum <= low_text_threshold
        or float(base_metrics["quality_score"]) < quality_threshold
        or int(base_metrics["word_count"]) <= word_threshold
    )

    if high_skew and base_alnum <= skew_retry_alnum_threshold:
        return True

    return weak_base and (
        table_regions >= int(extraction.get("ocr_retry_table_threshold", 3))
        or fragmented_layout
        or high_skew
    )


def select_route(native_detected: bool, native_quality: float, region_ids: list[str], handwriting_detected: bool, extraction: dict) -> tuple[str, dict]:
    quality_min = float(extraction.get("native_text_quality_threshold", 0.85))
    has_table_structure = any("_TAB_" in region_id for region_id in region_ids)
    route = "manual_review_required"
    if handwriting_detected:
        route = "ocr_handwriting_page"
    elif native_detected and native_quality >= quality_min:
        route = "native_text_only"
        if has_table_structure:
            route = "native_text_plus_layout"
    else:
        route = "ocr_text_page"
        if has_table_structure:
            route = "ocr_mixed_layout_page"
    return route, {
        "has_native": native_detected,
        "native_quality": native_quality,
        "has_table_structure": has_table_structure,
    }


def should_rescue_handwriting_route(route_type: str, table_regions: int, text_regions: int, settings: dict) -> bool:
    if route_type != "ocr_handwriting_page":
        return False
    return (table_regions + text_regions) >= int(settings.get("handwriting_dense_region_threshold", 40)) and text_regions >= int(
        settings.get("handwriting_dense_text_region_threshold", 20)
    )


def compute_dark_ratio(image: np.ndarray) -> float:
    _, thresh = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return float(cv2.countNonZero(thresh)) / float(image.shape[0] * image.shape[1])


def should_skip_ocr_as_blank(native_detected: bool, route_type: str, handwriting_detected: bool, region_count: int, dark_ratio: float, settings: dict) -> bool:
    if native_detected or route_type in {"native_text_only", "native_text_plus_layout"} or handwriting_detected:
        return False
    return region_count <= int(settings.get("pre_ocr_blank_max_regions", 24)) and dark_ratio <= float(
        settings.get("pre_ocr_blank_dark_ratio_threshold", 0.0002)
    )


def assess_ocr_witness(metrics: dict, selection_margin: float, candidate_count: int, table_like: bool, extraction: dict) -> dict:
    reasons: list[str] = []
    if int(metrics["alnum_count"]) <= int(extraction.get("ocr_witness_low_alnum_threshold", 24)):
        reasons.append("low_alnum")
    if float(metrics["quality_score"]) < float(extraction.get("ocr_witness_low_quality_threshold", 0.22)):
        reasons.append("low_quality")
    if (
        int(metrics["word_count"]) >= int(extraction.get("ocr_witness_dense_word_floor", 40))
        and float(metrics["lexical_ratio"]) < float(extraction.get("ocr_witness_low_lexical_ratio_threshold", 0.05))
        and float(metrics["short_word_ratio"]) > float(extraction.get("ocr_witness_high_short_word_ratio_threshold", 0.85))
    ):
        reasons.append("dense_gibberish_signal")

    low_word_count = int(extraction.get("ocr_witness_low_word_count", 4))
    low_numeric_count = int(extraction.get("ocr_witness_low_numeric_token_count", 2))
    if table_like:
        if int(metrics["word_count"]) < low_word_count and int(metrics["numeric_token_count"]) < low_numeric_count:
            reasons.append("low_table_token_evidence")
    else:
        if int(metrics["word_count"]) < low_word_count:
            reasons.append("low_word_count")
        if int(metrics["lexical_word_count"]) < max(2, low_word_count - 1):
            reasons.append("low_lexical_evidence")

    if candidate_count > 1 and selection_margin < float(extraction.get("ocr_witness_unstable_margin", 12.0)):
        reasons.append("unstable_retry_winner")

    return {
        "state": "weak" if reasons else "strong",
        "reasons": reasons,
    }


def candidate_uses_sparse_psm(candidate: dict) -> bool:
    config = str(candidate.get("config") or "")
    variant = str(candidate.get("variant") or "").lower()
    return "--psm 11" in config or "--psm 12" in config or "psm11" in variant or "psm12" in variant


def select_ocr_line_strategy(
    route_type: str,
    table_region_count: int,
    text_region_count: int,
    skew_angle: float,
    cardinal_rotation_applied: int,
    candidate: dict,
    witness_state: str,
    extraction: dict,
) -> str:
    fragmented_region_threshold = int(extraction.get("ocr_geometry_fragmented_region_threshold", 12))
    rotated_skew_threshold = float(extraction.get("ocr_geometry_rotated_skew_threshold", 10.0))
    low_quality_threshold = float(extraction.get("ocr_geometry_low_quality_threshold", 0.3))
    low_word_threshold = int(extraction.get("ocr_geometry_low_word_threshold", 18))
    metrics = candidate.get("metrics", {})
    fragmented_layout = (table_region_count + text_region_count) >= fragmented_region_threshold
    low_signal = (
        float(metrics.get("quality_score", 0.0)) <= low_quality_threshold
        or int(metrics.get("word_count", 0)) <= low_word_threshold
        or int(metrics.get("lexical_word_count", 0)) <= max(4, low_word_threshold // 4)
    )
    structured_route = route_type in {"ocr_mixed_layout_page", "ocr_handwriting_page"}
    rotated_cardinal = abs(int(cardinal_rotation_applied or 0)) % 180 == 90

    if candidate_uses_sparse_psm(candidate):
        return "y_overlap"
    if rotated_cardinal and structured_route and table_region_count > 0:
        return "y_overlap"
    if witness_state == "weak" and (structured_route or fragmented_layout or table_region_count > 0):
        return "y_overlap"
    if int(candidate.get("rotation") or 0) != 0 and abs(float(skew_angle)) >= rotated_skew_threshold and (
        table_region_count > 0 or low_signal
    ):
        return "y_overlap"
    if structured_route and fragmented_layout and low_signal:
        return "y_overlap"
    return "tesseract"


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def process_geometry_page(base_dir: Path, run_id: str, page_id: str, page_manifest: dict, thresholds: dict) -> dict:
    extraction = thresholds.get("extraction", {})
    render_dpi = int(extraction.get("render_dpi", 300))

    work_dir = base_dir / "work" / "runs" / run_id
    rendered_dir = work_dir / "pages_rendered"
    normalized_dir = work_dir / "pages_normalized"
    native_dir = work_dir / "native_text"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    normalized_dir.mkdir(parents=True, exist_ok=True)
    native_dir.mkdir(parents=True, exist_ok=True)

    raw_pdf_path = base_dir / page_manifest["raw_pdf_path"]
    timings: dict[str, float] = {"render_and_native": 0.0, "detect_skew": 0.0, "correct_skew": 0.0}

    started = time.perf_counter()
    native_text, rendered_image = render_page_pdf(raw_pdf_path, render_dpi)
    timings["render_and_native"] = time.perf_counter() - started
    native_detected = bool(native_text)
    native_quality = calculate_quality_score(native_text) if native_detected else 0.0

    rendered_filename = f"{page_id}.png"
    rendered_path = rendered_dir / rendered_filename
    cv2.imwrite(str(rendered_path), rendered_image)

    native_text_relative = None
    if native_detected:
        native_text_relative = Path("work") / "runs" / run_id / "native_text" / f"{page_id}.txt"
        save_text(base_dir / native_text_relative, native_text)

    raw_dark_ratio = compute_dark_ratio(rendered_image)
    started = time.perf_counter()
    normalized_image, geometry_report = normalize_page_geometry(
        rendered_image,
        native_detected=native_detected,
        dark_ratio=raw_dark_ratio,
        extraction=extraction,
    )
    geometry_seconds = time.perf_counter() - started
    timings["detect_skew"] = geometry_seconds
    timings["correct_skew"] = 0.0

    normalized_filename = f"{page_id}.png"
    normalized_path = normalized_dir / normalized_filename
    cv2.imwrite(str(normalized_path), normalized_image)

    cardinal_orientation = geometry_report.get("cardinal_orientation", {})
    manifest_update = {
        "native_text_detected": native_detected,
        "native_text_quality_score": native_quality,
        "rendered_image_path": str((Path("work") / "runs" / run_id / "pages_rendered" / rendered_filename).as_posix()),
        "normalized_image_path": str((Path("work") / "runs" / run_id / "pages_normalized" / normalized_filename).as_posix()),
        "raw_dark_ratio": round(raw_dark_ratio, 6),
        "detected_skew_angle": geometry_report.get("applied_skew_angle", 0.0),
        "coarse_skew_angle": geometry_report.get("coarse_skew_angle", 0.0),
        "residual_skew_angle": geometry_report.get("residual_skew_angle", 0.0),
        "residual_skew_probe_angle": geometry_report.get("residual_skew_probe_angle", 0.0),
        "cardinal_rotation_applied": cardinal_orientation.get("applied_angle", 0),
        "cardinal_orientation_source": cardinal_orientation.get("source", "base"),
        "cardinal_orientation_triggered": cardinal_orientation.get("triggered", False),
        "cardinal_orientation_candidates": cardinal_orientation.get("candidate_skews", []),
        "cardinal_orientation_ocr_candidates": cardinal_orientation.get("ocr_candidates", []),
        "cardinal_orientation_selection_margin": cardinal_orientation.get("selection_margin", 0.0),
        "geometry_normalization_state": "normalized",
        "geometry_normalization_source": geometry_report.get("refinement", {}).get("source"),
        "geometry_normalization_pass_count": len(geometry_report.get("pass_summaries", [])),
        "geometry_normalization_pass_summaries": geometry_report.get("pass_summaries", []),
        "geometry_normalization_projection_base_score": geometry_report.get("refinement", {}).get("base_score", 0.0),
        "geometry_normalization_projection_best_score": geometry_report.get("refinement", {}).get("best_score", 0.0),
        "geometry_normalization_coarse_candidates": geometry_report.get("refinement", {}).get("coarse_candidates", []),
        "geometry_normalization_fine_candidates": geometry_report.get("refinement", {}).get("fine_candidates", []),
        "current_state": "geometry_normalized",
    }
    if native_text_relative:
        manifest_update["native_text_path"] = str(native_text_relative.as_posix())

    return {
        "page_id": page_id,
        "page_number": page_manifest["source_page_number"],
        "manifest_update": manifest_update,
        "timings": {key: round(value, 4) for key, value in timings.items()},
    }


def process_page(
    base_dir: Path,
    run_id: str,
    page_id: str,
    page_manifest: dict,
    thresholds: dict,
    region_manifest_dir: Path | None,
) -> dict:
    extraction = thresholds.get("extraction", {})
    layout = thresholds.get("layout", {})
    state_machine = thresholds.get("state_machine", {})
    render_dpi = int(extraction.get("render_dpi", 300))
    handwriting_threshold = float(layout.get("handwriting_suspicion_threshold", 0.5))

    work_dir = base_dir / "work" / "runs" / run_id
    rendered_dir = work_dir / "pages_rendered"
    normalized_dir = work_dir / "pages_normalized"
    native_dir = work_dir / "native_text"
    ocr_dir = work_dir / "ocr_text"
    native_word_dir = work_dir / "native_word_witness"
    word_witness_dir = work_dir / "word_witness"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    normalized_dir.mkdir(parents=True, exist_ok=True)
    native_dir.mkdir(parents=True, exist_ok=True)
    ocr_dir.mkdir(parents=True, exist_ok=True)
    native_word_dir.mkdir(parents=True, exist_ok=True)
    word_witness_dir.mkdir(parents=True, exist_ok=True)

    timings: dict[str, float] = {}
    page_number = page_manifest["source_page_number"]
    raw_pdf_path = base_dir / page_manifest["raw_pdf_path"]
    native_word_witness_relative = None
    word_witness_relative = Path("work") / "runs" / run_id / "word_witness" / f"{page_id}.json"
    native_word_witness = None
    word_witness = None
    timings = {
        "render_and_native": 0.0,
        "detect_skew": 0.0,
        "correct_skew": 0.0,
    }
    geometry_manifest_update: dict = {}

    native_text_relative_raw = str(page_manifest.get("native_text_path") or "").strip()
    native_text_relative = Path(native_text_relative_raw) if native_text_relative_raw else None
    native_text = read_text_if_exists(base_dir / native_text_relative) if native_text_relative else ""
    native_detected = bool(page_manifest.get("native_text_detected", False) or native_text)
    native_quality = float(
        page_manifest.get("native_text_quality_score")
        if page_manifest.get("native_text_quality_score") is not None
        else (calculate_quality_score(native_text) if native_text else 0.0)
    )

    normalized_relative_raw = str(page_manifest.get("normalized_image_path") or "").strip()
    normalized_relative = Path(normalized_relative_raw) if normalized_relative_raw else None
    normalized_image = load_grayscale_image(base_dir / normalized_relative) if normalized_relative else None

    if normalized_image is None:
        geometry_result = process_geometry_page(base_dir, run_id, page_id, page_manifest, thresholds)
        geometry_manifest_update = geometry_result["manifest_update"]
        for stage_name, duration in geometry_result["timings"].items():
            timings[stage_name] = timings.get(stage_name, 0.0) + duration

        native_text_relative_raw = str(geometry_manifest_update.get("native_text_path") or "").strip()
        native_text_relative = Path(native_text_relative_raw) if native_text_relative_raw else None
        native_text = read_text_if_exists(base_dir / native_text_relative) if native_text_relative else ""
        native_detected = bool(geometry_manifest_update.get("native_text_detected", False) or native_text)
        native_quality = float(
            geometry_manifest_update.get("native_text_quality_score")
            if geometry_manifest_update.get("native_text_quality_score") is not None
            else (calculate_quality_score(native_text) if native_text else 0.0)
        )

        normalized_relative_raw = str(geometry_manifest_update.get("normalized_image_path") or "").strip()
        normalized_relative = Path(normalized_relative_raw) if normalized_relative_raw else None
        normalized_image = load_grayscale_image(base_dir / normalized_relative) if normalized_relative else None

    if normalized_image is None:
        raise RuntimeError(f"Failed to load normalized image for {page_id}")

    skew_angle = float(geometry_manifest_update.get("detected_skew_angle", page_manifest.get("detected_skew_angle", 0.0)) or 0.0)
    cardinal_orientation = {
        "applied_angle": geometry_manifest_update.get(
            "cardinal_rotation_applied",
            page_manifest.get("cardinal_rotation_applied", 0),
        ),
        "source": geometry_manifest_update.get(
            "cardinal_orientation_source",
            page_manifest.get("cardinal_orientation_source", "base"),
        ),
        "triggered": geometry_manifest_update.get(
            "cardinal_orientation_triggered",
            page_manifest.get("cardinal_orientation_triggered", False),
        ),
        "candidate_skews": geometry_manifest_update.get(
            "cardinal_orientation_candidates",
            page_manifest.get("cardinal_orientation_candidates", []),
        ),
        "ocr_candidates": geometry_manifest_update.get(
            "cardinal_orientation_ocr_candidates",
            page_manifest.get("cardinal_orientation_ocr_candidates", []),
        ),
        "selection_margin": geometry_manifest_update.get(
            "cardinal_orientation_selection_margin",
            page_manifest.get("cardinal_orientation_selection_margin", 0.0),
        ),
    }

    persist_region_manifests = bool(extraction.get("persist_region_manifests", False))
    ocr_candidate_max_dim = int(extraction.get("ocr_candidate_max_dim", 0) or 0)

    started = time.perf_counter()
    _, shared_threshold = cv2.threshold(normalized_image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    handwriting_confidence = detect_handwriting_confidence(normalized_image, threshold_image=shared_threshold)
    handwriting_detected = handwriting_confidence >= handwriting_threshold
    timings["detect_handwriting"] = time.perf_counter() - started

    started = time.perf_counter()
    text_regions = detect_text_regions(normalized_image, threshold_image=shared_threshold)
    timings["detect_text_regions"] = time.perf_counter() - started

    started = time.perf_counter()
    table_regions = detect_table_regions(normalized_image, threshold_image=shared_threshold)
    timings["detect_table_regions"] = time.perf_counter() - started

    region_ids: list[str] = []
    for index, region in enumerate(text_regions):
        region_id = f"{page_id}_REG_{index:04d}"
        region_payload = {
            "run_id": run_id,
            "page_id": page_id,
            "region_id": region_id,
            "region_type": region["type"],
            "bbox": region["bbox"],
            "confidence": region["confidence"],
            "source_layout_worker": "page_feature_pipeline",
        }
        if persist_region_manifests and region_manifest_dir is not None:
            (region_manifest_dir / f"{region_id}.json").write_text(json.dumps(region_payload, indent=4), encoding="utf-8")
        region_ids.append(region_id)

    for index, region in enumerate(table_regions):
        region_id = f"{page_id}_TAB_{index:04d}"
        region_payload = {
            "run_id": run_id,
            "page_id": page_id,
            "region_id": region_id,
            "region_type": region["type"],
            "bbox": region["bbox"],
            "confidence": region["confidence"],
            "source_layout_worker": "page_feature_pipeline",
        }
        if persist_region_manifests and region_manifest_dir is not None:
            (region_manifest_dir / f"{region_id}.json").write_text(json.dumps(region_payload, indent=4), encoding="utf-8")
        region_ids.append(region_id)

    if native_detected:
        if native_text_relative is None:
            native_text_relative = Path("work") / "runs" / run_id / "native_text" / f"{page_id}.txt"
        save_text(base_dir / native_text_relative, native_text)
        native_word_witness_relative = Path("work") / "runs" / run_id / "native_word_witness" / f"{page_id}.json"
        native_word_witness = extract_native_word_witness(raw_pdf_path)
        save_json(base_dir / native_word_witness_relative, native_word_witness)

    route_type, structural_signals = select_route(native_detected, native_quality, region_ids, handwriting_detected, extraction)
    dark_ratio = compute_dark_ratio(normalized_image)
    pre_ocr_blank_skip = should_skip_ocr_as_blank(
        native_detected=native_detected,
        route_type=route_type,
        handwriting_detected=handwriting_detected,
        region_count=len(region_ids),
        dark_ratio=dark_ratio,
        settings=state_machine,
    )

    ocr_relative = Path("work") / "runs" / run_id / "ocr_text" / f"{page_id}.txt"
    ocr_path = base_dir / ocr_relative
    extraction_engine_used = None
    ocr_variant_used = None
    ocr_alnum = None
    ocr_quality_score = 0.0
    ocr_word_count = 0
    ocr_lexical_word_count = 0
    ocr_numeric_token_count = 0
    ocr_noise_ratio = 0.0
    ocr_selection_score = 0.0
    ocr_selection_margin = 0.0
    ocr_retry_used = False
    ocr_candidate_summaries: list[dict] = []
    ocr_witness_state = "unknown"
    ocr_witness_reasons: list[str] = []

    started = time.perf_counter()
    if route_type in {"native_text_only", "native_text_plus_layout"}:
        save_text(ocr_path, native_text)
        extraction_engine_used = "native_pymupdf"
        native_metrics = build_ocr_metrics(native_text)
        ocr_variant_used = "native_text"
        ocr_alnum = native_metrics["alnum_count"]
        ocr_quality_score = native_metrics["quality_score"]
        ocr_word_count = native_metrics["word_count"]
        ocr_lexical_word_count = native_metrics["lexical_word_count"]
        ocr_numeric_token_count = native_metrics["numeric_token_count"]
        ocr_noise_ratio = native_metrics["noise_ratio"]
        ocr_selection_score = ocr_candidate_selection_score(native_metrics)
        ocr_witness_state = "strong"
        ocr_witness_reasons = ["native_text"]
    elif pre_ocr_blank_skip:
        save_text(ocr_path, "")
        extraction_engine_used = "visual_blank_skip"
        ocr_variant_used = "base_default"
        ocr_alnum = 0
        ocr_witness_state = "blank_skip"
        ocr_witness_reasons = ["visual_blank_skip"]
        word_witness = build_empty_word_witness(
            engine="visual_blank_skip",
            coordinate_space="normalized_image_pixels",
            page_size=[int(normalized_image.shape[1]), int(normalized_image.shape[0])],
            source_variant="visual_blank_skip",
        )
    else:
        pil_image = Image.fromarray(normalized_image)
        candidate_images: dict[int, Image.Image] = {0: pil_image}
        candidate_ocr_payloads: dict[int, tuple[Image.Image, list[int]]] = {}

        def get_ocr_payload(rotation: int) -> tuple[Image.Image, list[int]]:
            source_image = candidate_images[rotation]
            output_page_size = [int(source_image.size[0]), int(source_image.size[1])]
            if rotation not in candidate_ocr_payloads:
                candidate_ocr_payloads[rotation] = (
                    resize_pil_image_max_dim(source_image, ocr_candidate_max_dim),
                    output_page_size,
                )
            return candidate_ocr_payloads[rotation]

        rescue_handwriting_route = should_rescue_handwriting_route(
            route_type=route_type,
            table_regions=len(table_regions),
            text_regions=len(text_regions),
            settings=state_machine,
        )
        base_ocr_image, base_output_page_size = get_ocr_payload(0)
        base_default_candidate = make_ocr_candidate(
            base_ocr_image,
            "base_default",
            0,
            None,
            favor_numeric=False,
            output_page_size=base_output_page_size,
        )
        favor_numeric = len(table_regions) >= int(extraction.get("ocr_retry_table_threshold", 3)) and (
            int(base_default_candidate["metrics"]["numeric_token_count"]) >= int(extraction.get("ocr_retry_numeric_token_threshold", 8))
            or int(base_default_candidate["metrics"]["lexical_word_count"]) < int(
                extraction.get("ocr_retry_numeric_bias_lexical_ceiling", 8)
            )
        )
        if favor_numeric:
            base_default_candidate["selection_score"] = ocr_candidate_selection_score(
                base_default_candidate["metrics"],
                favor_numeric=True,
            )
        candidates = [base_default_candidate]
        if rescue_handwriting_route:
            candidates[0]["variant"] = "handwriting_rescue_base_default"

        base_metrics = candidates[0]["metrics"]
        if should_try_rotation(len(table_regions), len(text_regions), skew_angle, base_metrics, extraction):
            for rotation in (90, 180, 270):
                candidate_images[rotation] = pil_image.rotate(rotation, expand=True)
            for rotation, _ in sorted(candidate_images.items(), key=lambda item: item[0]):
                rotation_image, rotation_output_size = get_ocr_payload(rotation)
                variant = "base_psm6" if rotation == 0 else f"rot{rotation}_psm6"
                candidates.append(
                    make_ocr_candidate(
                        rotation_image,
                        variant,
                        rotation,
                        "--psm 6",
                        favor_numeric=favor_numeric,
                        output_page_size=rotation_output_size,
                    )
                )

            best_candidate, selection_margin = select_best_ocr_candidate(candidates)
            fragmented_layout = (len(table_regions) + len(text_regions)) >= int(extraction.get("ocr_retry_sparse_region_threshold", 12))
            if (
                fragmented_layout
                or best_candidate["metrics"]["quality_score"] < float(extraction.get("ocr_retry_quality_threshold", 0.24))
                or best_candidate["metrics"]["word_count"] <= int(extraction.get("ocr_retry_word_threshold", 4))
            ):
                top_rotations: list[int] = []
                for candidate in sorted(candidates, key=lambda item: item["selection_score"], reverse=True):
                    rotation = int(candidate["rotation"])
                    if rotation in top_rotations:
                        continue
                    top_rotations.append(rotation)
                    if len(top_rotations) >= int(extraction.get("ocr_retry_candidate_orientation_limit", 2)):
                        break
                sparse_psm = str(extraction.get("ocr_retry_sparse_psm", 11))
                for rotation in top_rotations:
                    sparse_image, sparse_output_size = get_ocr_payload(rotation)
                    variant = f"base_psm{sparse_psm}" if rotation == 0 else f"rot{rotation}_psm{sparse_psm}"
                    candidates.append(
                        make_ocr_candidate(
                            sparse_image,
                            variant,
                            rotation,
                            f"--psm {sparse_psm}",
                            favor_numeric=favor_numeric,
                            output_page_size=sparse_output_size,
                        )
                    )

        scored_best_candidate, selection_margin = select_best_ocr_candidate(candidates)
        best_candidate = stabilize_ocr_candidate_choice(candidates, scored_best_candidate)
        if best_candidate["variant"] != scored_best_candidate["variant"]:
            selection_margin = float(extraction.get("ocr_witness_unstable_margin", 12.0))

        preliminary_witness = assess_ocr_witness(
            best_candidate["metrics"],
            selection_margin=selection_margin,
            candidate_count=len(candidates),
            table_like=favor_numeric,
            extraction=extraction,
        )
        line_strategy = select_ocr_line_strategy(
            route_type=route_type,
            table_region_count=len(table_regions),
            text_region_count=len(text_regions),
            skew_angle=skew_angle,
            cardinal_rotation_applied=int(cardinal_orientation.get("applied_angle", 0) or 0),
            candidate=best_candidate,
            witness_state=preliminary_witness["state"],
            extraction=extraction,
        )
        word_witness = build_ocr_word_witness_from_words(
            best_candidate["ocr_words"],
            page_size=best_candidate["page_size"],
            source_variant=best_candidate["variant"],
            line_strategy=line_strategy,
            extraction=extraction,
        )
        text = build_text_from_lines(word_witness["lines"])
        final_metrics = build_ocr_metrics(text)
        final_selection_score = ocr_candidate_selection_score(final_metrics, favor_numeric=favor_numeric)
        witness = assess_ocr_witness(
            final_metrics,
            selection_margin=selection_margin,
            candidate_count=len(candidates),
            table_like=favor_numeric,
            extraction=extraction,
        )
        ocr_variant_used = best_candidate["variant"]
        ocr_alnum = final_metrics["alnum_count"]
        ocr_quality_score = final_metrics["quality_score"]
        ocr_word_count = final_metrics["word_count"]
        ocr_lexical_word_count = final_metrics["lexical_word_count"]
        ocr_numeric_token_count = final_metrics["numeric_token_count"]
        ocr_noise_ratio = final_metrics["noise_ratio"]
        ocr_selection_score = final_selection_score
        ocr_selection_margin = selection_margin
        ocr_retry_used = len(candidates) > 1
        ocr_candidate_summaries = [summarize_ocr_candidate(candidate) for candidate in candidates]
        ocr_witness_state = witness["state"]
        ocr_witness_reasons = witness["reasons"]

        save_text(ocr_path, text)
        extraction_engine_used = "tesseract_v5"
    timings["extract_text"] = time.perf_counter() - started

    if route_type in {"native_text_only", "native_text_plus_layout"} and native_word_witness is not None:
        word_witness = native_word_witness
    if word_witness is None:
        word_witness = build_empty_word_witness(
            engine=extraction_engine_used or "unknown",
            coordinate_space="normalized_image_pixels",
            page_size=[int(normalized_image.shape[1]), int(normalized_image.shape[0])],
            source_variant=ocr_variant_used or "unknown",
        )
    save_json(base_dir / word_witness_relative, word_witness)

    rendered_image_path = str(
        geometry_manifest_update.get(
            "rendered_image_path",
            page_manifest.get("rendered_image_path", ""),
        )
        or ""
    )
    normalized_image_path = str(
        geometry_manifest_update.get(
            "normalized_image_path",
            page_manifest.get("normalized_image_path", ""),
        )
        or ""
    )

    manifest_update = {
        "native_text_detected": native_detected,
        "native_text_quality_score": native_quality,
        "rendered_image_path": rendered_image_path,
        "detected_skew_angle": skew_angle,
        "cardinal_rotation_applied": cardinal_orientation.get("applied_angle", 0),
        "cardinal_orientation_source": cardinal_orientation.get("source", "base"),
        "cardinal_orientation_triggered": cardinal_orientation.get("triggered", False),
        "cardinal_orientation_candidates": cardinal_orientation.get("candidate_skews", []),
        "cardinal_orientation_ocr_candidates": cardinal_orientation.get("ocr_candidates", []),
        "cardinal_orientation_selection_margin": cardinal_orientation.get("selection_margin", 0.0),
        "normalized_image_path": normalized_image_path,
        "handwriting_detected": handwriting_detected,
        "handwriting_confidence": handwriting_confidence,
        "layout_path": str((Path("manifests") / "regions").as_posix()),
        "region_ids": region_ids,
        "route_type": route_type,
        "route_confidence": 1.0,
        "structural_signals": structural_signals,
        "ocr_text_path": str(ocr_relative.as_posix()),
        "extraction_engine_used": extraction_engine_used,
        "ocr_variant_used": ocr_variant_used,
        "ocr_alnum_count": ocr_alnum,
        "ocr_quality_score": ocr_quality_score,
        "ocr_word_count": ocr_word_count,
        "ocr_lexical_word_count": ocr_lexical_word_count,
        "ocr_numeric_token_count": ocr_numeric_token_count,
        "ocr_noise_ratio": ocr_noise_ratio,
        "ocr_selection_score": ocr_selection_score,
        "ocr_selection_margin": ocr_selection_margin,
        "ocr_retry_used": ocr_retry_used,
        "ocr_candidate_summaries": ocr_candidate_summaries,
        "ocr_witness_state": ocr_witness_state,
        "ocr_witness_reasons": ocr_witness_reasons,
        "word_witness_path": str(word_witness_relative.as_posix()),
        "word_witness_engine": word_witness.get("engine"),
        "word_witness_coordinate_space": word_witness.get("coordinate_space"),
        "word_witness_word_count": word_witness.get("word_count", 0),
        "word_witness_line_count": word_witness.get("line_count", 0),
        "word_witness_source_variant": word_witness.get("source_variant"),
        "word_witness_line_strategy": word_witness.get("line_strategy"),
        "pre_ocr_blank_skip": pre_ocr_blank_skip,
        "pre_ocr_blank_dark_ratio": round(dark_ratio, 6),
        "current_state": "extraction_complete",
    }
    if geometry_manifest_update:
        manifest_update = {**geometry_manifest_update, **manifest_update}
    if native_text_relative:
        manifest_update["native_text_path"] = str(native_text_relative.as_posix())
    if native_word_witness_relative:
        manifest_update["native_word_witness_path"] = str(native_word_witness_relative.as_posix())
        manifest_update["native_word_witness_word_count"] = native_word_witness.get("word_count", 0) if native_word_witness else 0
        manifest_update["native_word_witness_line_count"] = native_word_witness.get("line_count", 0) if native_word_witness else 0
    return {
        "page_id": page_id,
        "page_number": page_number,
        "manifest_update": manifest_update,
        "timings": {key: round(value, 4) for key, value in timings.items()},
        "route_type": route_type,
        "pre_ocr_blank_skip": pre_ocr_blank_skip,
        "ocr_variant_used": ocr_variant_used,
    }


def run_page_geometry_normalization(base_dir: Path, run_id: str, thresholds: dict) -> dict:
    log_dir = base_dir / "logs" / "runs" / run_id
    manifest_dir = base_dir / "manifests"
    run_handler = ManifestHandler(manifest_dir / "runs")
    page_handler = ManifestHandler(manifest_dir / "pages")
    logger = PipelineLogger(log_dir, "page_geometry_normalization")

    service = thresholds.get("service", {})
    max_workers = max(1, int(service.get("page_worker_max_workers", 1)))
    cv2.setNumThreads(int(service.get("opencv_num_threads", 1)))

    run_manifest = run_handler.load(run_id)
    page_ids = run_manifest.get("page_ids", [])
    page_tasks = [(page_id, page_handler.load(page_id)) for page_id in page_ids]
    aggregate_timings = {
        "render_and_native": 0.0,
        "detect_skew": 0.0,
        "correct_skew": 0.0,
    }

    logger.info(
        "PAGE_GEOMETRY_NORMALIZATION_START",
        "SUCCESS",
        run_id=run_id,
        message=f"Normalizing {len(page_tasks)} pages with {min(max_workers, max(1, len(page_tasks)))} workers.",
    )

    if not page_tasks:
        stats = {"page_count": 0, "stage_totals_seconds": aggregate_timings}
        run_handler.update(run_id, {"geometry_normalization_stats": stats, "status": "geometry_normalized"})
        logger.info("PAGE_GEOMETRY_NORMALIZATION_COMPLETE", "SUCCESS", run_id=run_id)
        return stats

    with ThreadPoolExecutor(max_workers=min(max_workers, len(page_tasks)), thread_name_prefix="page_geometry") as executor:
        futures = {
            executor.submit(process_geometry_page, base_dir, run_id, page_id, page_manifest, thresholds): page_id
            for page_id, page_manifest in page_tasks
        }
        for future in as_completed(futures):
            result = future.result()
            page_handler.update(result["page_id"], result["manifest_update"])
            for stage_name, duration in result["timings"].items():
                aggregate_timings[stage_name] += duration
            logger.info(
                "PAGE_GEOMETRY_NORMALIZATION_PAGE_COMPLETE",
                "SUCCESS",
                run_id=run_id,
                page_id=result["page_id"],
                message=f"skew={result['manifest_update'].get('detected_skew_angle', 0.0)} residual={result['manifest_update'].get('residual_skew_angle', 0.0)}",
            )

    stats = {
        "page_count": len(page_tasks),
        "stage_totals_seconds": {key: round(value, 2) for key, value in aggregate_timings.items()},
    }
    run_handler.update(run_id, {"geometry_normalization_stats": stats, "status": "geometry_normalized"})
    logger.info("PAGE_GEOMETRY_NORMALIZATION_COMPLETE", "SUCCESS", run_id=run_id, message=json.dumps(stats))
    return stats


def run_page_feature_pipeline(base_dir: Path, run_id: str, thresholds: dict) -> dict:
    log_dir = base_dir / "logs" / "runs" / run_id
    manifest_dir = base_dir / "manifests"
    run_handler = ManifestHandler(manifest_dir / "runs")
    page_handler = ManifestHandler(manifest_dir / "pages")
    logger = PipelineLogger(log_dir, "page_feature_pipeline")

    service = thresholds.get("service", {})
    extraction = thresholds.get("extraction", {})
    max_workers = max(int(service.get("page_worker_max_workers", 1)), int(service.get("ocr_max_workers", 1)))
    cv2.setNumThreads(int(service.get("opencv_num_threads", 1)))
    os.environ["OMP_THREAD_LIMIT"] = str(service.get("tesseract_omp_thread_limit", 1))
    os.environ["OMP_NUM_THREADS"] = str(service.get("tesseract_omp_num_threads", 1))

    run_manifest = run_handler.load(run_id)
    page_ids = run_manifest.get("page_ids", [])
    region_manifest_dir: Path | None = None
    if bool(extraction.get("persist_region_manifests", False)):
        region_manifest_dir = manifest_dir / "regions"
        region_manifest_dir.mkdir(parents=True, exist_ok=True)

    page_tasks = [(page_id, page_handler.load(page_id)) for page_id in page_ids]
    aggregate_timings = {
        "render_and_native": 0.0,
        "detect_skew": 0.0,
        "correct_skew": 0.0,
        "detect_handwriting": 0.0,
        "detect_text_regions": 0.0,
        "detect_table_regions": 0.0,
        "extract_text": 0.0,
    }
    blank_skip_count = 0

    logger.info(
        "PAGE_FEATURE_PIPELINE_START",
        "SUCCESS",
        run_id=run_id,
        message=f"Processing {len(page_tasks)} pages with {min(max_workers, max(1, len(page_tasks)))} workers.",
    )

    if not page_tasks:
        stats = {"page_count": 0, "blank_skip_count": 0, "stage_totals_seconds": aggregate_timings}
        run_handler.update(run_id, {"feature_pipeline_stats": stats, "status": "page_features_complete"})
        logger.info("PAGE_FEATURE_PIPELINE_COMPLETE", "SUCCESS", run_id=run_id)
        return stats

    with ThreadPoolExecutor(max_workers=min(max_workers, len(page_tasks)), thread_name_prefix="page_feature") as executor:
        futures = {
            executor.submit(process_page, base_dir, run_id, page_id, page_manifest, thresholds, region_manifest_dir): page_id
            for page_id, page_manifest in page_tasks
        }
        for future in as_completed(futures):
            result = future.result()
            page_handler.update(result["page_id"], result["manifest_update"])
            for stage_name, duration in result["timings"].items():
                aggregate_timings[stage_name] += duration
            if result["pre_ocr_blank_skip"]:
                blank_skip_count += 1
            logger.info(
                "PAGE_FEATURE_PIPELINE_PAGE_COMPLETE",
                "SUCCESS",
                run_id=run_id,
                page_id=result["page_id"],
                message=f"route={result['route_type']} blank_skip={result['pre_ocr_blank_skip']} variant={result['ocr_variant_used']}",
            )

    stats = {
        "page_count": len(page_tasks),
        "blank_skip_count": blank_skip_count,
        "stage_totals_seconds": {key: round(value, 2) for key, value in aggregate_timings.items()},
    }
    run_handler.update(run_id, {"feature_pipeline_stats": stats, "status": "page_features_complete"})
    logger.info("PAGE_FEATURE_PIPELINE_COMPLETE", "SUCCESS", run_id=run_id, message=json.dumps(stats))
    return stats
