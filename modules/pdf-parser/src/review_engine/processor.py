from __future__ import annotations

import re
from pathlib import Path

import cv2
import fitz
import numpy as np
import pytesseract
from PIL import Image
from pytesseract import Output


pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


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


def normalize_bbox(x: float, y: float, width: float, height: float) -> list[float]:
    return [
        round(float(x), 2),
        round(float(y), 2),
        round(float(width), 2),
        round(float(height), 2),
    ]


def load_grayscale_image(image_path: Path | None, pdf_path: Path | None, render_dpi: int) -> np.ndarray | None:
    if image_path and image_path.exists():
        image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if image is not None:
            return image
    if pdf_path and pdf_path.exists():
        doc = fitz.open(pdf_path)
        try:
            page = doc[0]
            zoom = render_dpi / 72.0
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
            return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
        finally:
            doc.close()
    return None


def binary_inverse(image: np.ndarray) -> np.ndarray:
    _, thresh = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return thresh


def compute_dark_ratio(image: np.ndarray) -> float:
    thresh = binary_inverse(image)
    return float(cv2.countNonZero(thresh)) / float(image.shape[0] * image.shape[1])


def merge_positions(positions: list[float], tolerance: int) -> list[int]:
    if not positions:
        return []
    positions = sorted(float(position) for position in positions)
    merged: list[list[float]] = [[positions[0]]]
    for position in positions[1:]:
        if abs(position - merged[-1][-1]) <= tolerance:
            merged[-1].append(position)
        else:
            merged.append([position])
    return [int(round(sum(group) / len(group))) for group in merged]


def detect_table_regions(image: np.ndarray, settings: dict) -> list[dict]:
    thresh = binary_inverse(image)
    horizontal_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (max(40, image.shape[1] // int(settings.get("horizontal_kernel_divisor", 28))), 1),
    )
    vertical_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (1, max(40, image.shape[0] // int(settings.get("vertical_kernel_divisor", 28)))),
    )
    horizontal = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, horizontal_kernel)
    vertical = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, vertical_kernel)
    grid = cv2.add(horizontal, vertical)

    contours = cv2.findContours(grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = contours[0] if len(contours) == 2 else contours[1]
    min_width = int(settings.get("table_min_width", 120))
    min_height = int(settings.get("table_min_height", 60))
    min_area = int(settings.get("table_min_area", 6000))
    regions: list[dict] = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        area = width * height
        if width < min_width or height < min_height or area < min_area:
            continue
        regions.append(
            {
                "bbox": [x, y, width, height],
                "confidence": 1.0,
                "source": "deterministic_grid_detector",
            }
        )
    regions.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
    return regions


def extract_line_positions(mask: np.ndarray, orientation: str, settings: dict) -> list[int]:
    height, width = mask.shape[:2]
    min_span_ratio = float(settings.get("line_min_span_ratio", 0.45))
    thickness_limit = int(settings.get("line_thickness_limit", 18))
    contours = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = contours[0] if len(contours) == 2 else contours[1]
    positions: list[float] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if orientation == "horizontal":
            if w < int(width * min_span_ratio) or h > thickness_limit:
                continue
            positions.append(y + (h / 2.0))
        else:
            if h < int(height * min_span_ratio) or w > thickness_limit:
                continue
            positions.append(x + (w / 2.0))
    tolerance = int(settings.get("line_merge_tolerance_pixels", 10))
    return merge_positions(positions, tolerance)


def inject_region_edges(positions: list[int], upper_bound: int, tolerance: int) -> list[int]:
    working = list(positions)
    if not working or abs(working[0]) > tolerance:
        working.insert(0, 0)
    if abs(working[-1] - upper_bound) > tolerance:
        working.append(int(upper_bound))
    return merge_positions(working, tolerance)


def build_cell_grid(region_shape: tuple[int, int], horizontal_positions: list[int], vertical_positions: list[int], settings: dict) -> list[dict]:
    height, width = region_shape
    tolerance = int(settings.get("line_merge_tolerance_pixels", 10))
    horizontal_positions = inject_region_edges(horizontal_positions, height, tolerance)
    vertical_positions = inject_region_edges(vertical_positions, width, tolerance)
    min_row_boundaries = int(settings.get("grid_min_row_boundaries", 3))
    min_col_boundaries = int(settings.get("grid_min_col_boundaries", 3))
    if len(horizontal_positions) < min_row_boundaries or len(vertical_positions) < min_col_boundaries:
        return []

    min_width = int(settings.get("cell_min_width", 18))
    min_height = int(settings.get("cell_min_height", 12))
    max_cells = int(settings.get("table_max_cells", 400))
    cells: list[dict] = []
    for row_index in range(len(horizontal_positions) - 1):
        top = horizontal_positions[row_index]
        bottom = horizontal_positions[row_index + 1]
        cell_height = bottom - top
        if cell_height < min_height:
            continue
        for col_index in range(len(vertical_positions) - 1):
            left = vertical_positions[col_index]
            right = vertical_positions[col_index + 1]
            cell_width = right - left
            if cell_width < min_width:
                continue
            cells.append(
                {
                    "row_index": row_index + 1,
                    "column_index": col_index + 1,
                    "bbox": [left, top, cell_width, cell_height],
                }
            )
            if len(cells) >= max_cells:
                return cells
    return cells


def crop_with_padding(image: np.ndarray, bbox: list[int], padding: int) -> np.ndarray:
    x, y, width, height = bbox
    x0 = max(0, int(x) - padding)
    y0 = max(0, int(y) - padding)
    x1 = min(image.shape[1], int(x + width) + padding)
    y1 = min(image.shape[0], int(y + height) + padding)
    return image[y0:y1, x0:x1]


def preprocess_crop(image: np.ndarray) -> np.ndarray:
    bordered = cv2.copyMakeBorder(image, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=255)
    if bordered.shape[0] < 48 or bordered.shape[1] < 120:
        bordered = cv2.resize(bordered, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    return bordered


def ocr_crop(image: np.ndarray, settings: dict) -> tuple[str, float | None]:
    processed = preprocess_crop(image)
    fallback_psm = int(settings.get("ocr_fallback_psm", 6))
    for psm in (int(settings.get("ocr_psm", 7)), fallback_psm):
        data = pytesseract.image_to_data(Image.fromarray(processed), output_type=Output.DICT, config=f"--psm {psm}")
        tokens: list[str] = []
        confidences: list[float] = []
        total = len(data.get("text", []))
        for index in range(total):
            token = str(data["text"][index] or "").strip()
            if not token:
                continue
            confidence = safe_float(data["conf"][index], -1.0)
            tokens.append(token)
            if confidence >= 0.0:
                confidences.append(confidence)
        text = re.sub(r"\s+", " ", " ".join(tokens)).strip()
        if text:
            average_confidence = round(sum(confidences) / len(confidences), 2) if confidences else None
            return text, average_confidence
    return "", None


def process_table_region(image: np.ndarray, table_bbox: list[int], table_index: int, settings: dict) -> dict:
    x, y, width, height = [int(value) for value in table_bbox]
    region_image = image[y : y + height, x : x + width]
    thresh = binary_inverse(region_image)
    horizontal_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (max(20, width // int(settings.get("horizontal_kernel_divisor", 28))), 1),
    )
    vertical_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (1, max(20, height // int(settings.get("vertical_kernel_divisor", 28)))),
    )
    horizontal_mask = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, horizontal_kernel)
    vertical_mask = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, vertical_kernel)
    horizontal_positions = extract_line_positions(horizontal_mask, "horizontal", settings)
    vertical_positions = extract_line_positions(vertical_mask, "vertical", settings)
    cells = build_cell_grid(region_image.shape[:2], horizontal_positions, vertical_positions, settings)

    rows: dict[int, list[dict]] = {}
    blank_threshold = float(settings.get("cell_blank_dark_ratio_threshold", 0.001))
    padding = int(settings.get("cell_padding_pixels", 4))
    nonempty_cells = 0

    for cell in cells:
        cell_crop = crop_with_padding(region_image, cell["bbox"], padding)
        if cell_crop.size == 0 or compute_dark_ratio(cell_crop) <= blank_threshold:
            cell_text = ""
            confidence = None
        else:
            cell_text, confidence = ocr_crop(cell_crop, settings)
            if cell_text:
                nonempty_cells += 1
        absolute_bbox = [
            int(x + cell["bbox"][0]),
            int(y + cell["bbox"][1]),
            int(cell["bbox"][2]),
            int(cell["bbox"][3]),
        ]
        rows.setdefault(cell["row_index"], []).append(
            {
                "row_index": cell["row_index"],
                "column_index": cell["column_index"],
                "text": cell_text,
                "confidence": confidence,
                "bbox": normalize_bbox(*absolute_bbox),
            }
        )

    row_payloads = []
    text_rows: list[str] = []
    max_columns = 0
    for row_index in sorted(rows):
        row_cells = sorted(rows[row_index], key=lambda item: item["column_index"])
        max_columns = max(max_columns, len(row_cells))
        row_text = "| " + " | ".join((cell.get("text") or "").strip() for cell in row_cells) + " |"
        text_rows.append(row_text)
        row_payloads.append(
            {
                "row_index": row_index,
                "cells": row_cells,
                "text": row_text,
                "nonempty_cell_count": sum(1 for cell in row_cells if str(cell.get("text") or "").strip()),
            }
        )

    status = "completed" if row_payloads and nonempty_cells > 0 else "no_structured_cells"
    return {
        "table_index": table_index,
        "status": status,
        "bbox": normalize_bbox(x, y, width, height),
        "horizontal_line_count": len(horizontal_positions),
        "vertical_line_count": len(vertical_positions),
        "row_count": len(row_payloads),
        "column_count": max_columns,
        "cell_count": len(cells),
        "nonempty_cell_count": nonempty_cells,
        "rows": row_payloads,
        "text_rows": text_rows[:80],
    }


def process_review_page(page_manifest: dict, settings: dict) -> dict:
    image_path_raw = str(page_manifest.get("staged_normalized_image_path") or "").strip()
    pdf_path_raw = str(page_manifest.get("staged_raw_pdf_path") or "").strip()
    image_path = Path(image_path_raw) if image_path_raw else None
    pdf_path = Path(pdf_path_raw) if pdf_path_raw else None
    render_dpi = int(settings.get("render_dpi", 300))
    image = load_grayscale_image(
        image_path if image_path and image_path.exists() else None,
        pdf_path if pdf_path and pdf_path.exists() else None,
        render_dpi,
    )
    if image is None:
        return {
            "schema_version": "catalyst_review_page.v1",
            "status": "missing_input",
            "tables": [],
            "flattened_rows": [],
        }

    regions = detect_table_regions(image, settings)
    table_results = [
        process_table_region(image, region["bbox"], table_index=index + 1, settings=settings)
        for index, region in enumerate(regions)
    ]

    if not any(table["status"] == "completed" for table in table_results):
        full_page_bbox = [0, 0, image.shape[1], image.shape[0]]
        fallback_table = process_table_region(image, full_page_bbox, table_index=len(table_results) + 1, settings=settings)
        if fallback_table["status"] == "completed":
            table_results.append(fallback_table)

    flattened_rows: list[str] = []
    for table in table_results:
        flattened_rows.extend(table.get("text_rows", []))

    completed_tables = [table for table in table_results if table.get("status") == "completed"]
    status = "completed" if completed_tables else "no_grid_detected"
    return {
        "schema_version": "catalyst_review_page.v1",
        "status": status,
        "image_shape": [int(image.shape[1]), int(image.shape[0])],
        "detected_table_region_count": len(regions),
        "completed_table_count": len(completed_tables),
        "tables": table_results,
        "flattened_rows": flattened_rows[:200],
        "flattened_text": "\n".join(flattened_rows[:200]).strip(),
    }
