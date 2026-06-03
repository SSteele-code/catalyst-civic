import sys
import os
import json

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler
from src.common.constants import BOUNDARY_DOCUMENT_START, BOUNDARY_TYPE_CHANGE

def get_feature(page, key, default=0.0):
    return page.get("semantic_features", {}).get(key, default)

def is_low_content_blank(base_dir, page, alnum_threshold):
    ocr_text_path = page.get("ocr_text_path")
    if not ocr_text_path:
        return False

    scores = page.get("classification_scores", {})
    if (
        scores.get("mode_1", 0.0) > 0.15
        or scores.get("mode_2", 0.0) > 0.20
        or get_feature(page, "mode_1_attachment") > 0.20
    ):
        return False

    table_count = sum(1 for rid in page.get("region_ids", []) if "_TAB_" in rid)
    if table_count > 1:
        return False

    full_text_path = os.path.join(base_dir, ocr_text_path)
    if not os.path.exists(full_text_path):
        return False

    with open(full_text_path, "r", encoding="utf-8") as f:
        text = f.read() or ""

    alnum_count = sum(1 for ch in text if ch.isalnum())
    return alnum_count <= alnum_threshold

def get_effective_type(page_data, index, window, base_dir, low_content_alnum_threshold):
    current_type = page_data[index].get("page_type", "unknown")
    if current_type != "unknown":
        return current_type

    if not is_low_content_blank(base_dir, page_data[index], low_content_alnum_threshold):
        return current_type

    prev_type = None
    next_type = None

    for offset in range(1, window + 1):
        prev_index = index - offset
        next_index = index + offset

        if prev_type is None and prev_index >= 0:
            candidate = page_data[prev_index].get("page_type", "unknown")
            if candidate != "unknown":
                prev_type = candidate

        if next_type is None and next_index < len(page_data):
            candidate = page_data[next_index].get("page_type", "unknown")
            if candidate != "unknown":
                next_type = candidate

    if prev_type and next_type and prev_type == next_type:
        return prev_type
    if prev_type:
        return prev_type
    if next_type:
        return next_type
    return current_type

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    manifest_dir = os.path.join(base_dir, "manifests")
    run_manifest_dir = os.path.join(manifest_dir, "runs")
    page_manifest_dir = os.path.join(manifest_dir, "pages")

    config_path = os.path.join(base_dir, "config", "thresholds.json")
    with open(config_path, "r") as f:
        config = json.load(f)
    segmentation = config.get("segmentation", {})
    mode_1_start_threshold = segmentation.get("mode_1_document_start_threshold", 0.90)
    unknown_window = segmentation.get("unknown_absorption_window", 1)
    low_content_alnum_threshold = segmentation.get("low_content_alnum_threshold", 24)
    
    logger = PipelineLogger(log_dir, "detect_document_boundaries")
    run_handler = ManifestHandler(run_manifest_dir)
    page_handler = ManifestHandler(page_manifest_dir)
    
    try:
        run_manifest = run_handler.load(run_id)
        page_ids = run_manifest.get("page_ids", [])
        
        logger.info("BOUNDARY_DETECTION_START", "SUCCESS", run_id=run_id)
        
        page_data = [page_handler.load(page_id) for page_id in page_ids]
        segments = []
        current_segment = None
        
        for i, page_manifest in enumerate(page_data):
            page_id = page_manifest["page_id"]
            page_type = get_effective_type(page_data, i, unknown_window, base_dir, low_content_alnum_threshold)
            start_signal = get_feature(page_manifest, "mode_1_document_start")
            
            boundary_reason = None
            
            # --- INDUSTRIAL BOUNDARY LOGIC (3.3.1) ---
            if current_segment is None:
                boundary_reason = BOUNDARY_DOCUMENT_START
            elif page_type != current_segment["type"]:
                boundary_reason = BOUNDARY_TYPE_CHANGE
            elif page_type == "mode_1" and start_signal >= mode_1_start_threshold and current_segment["pages"]:
                previous_page = page_data[i-1] if i > 0 else None
                previous_start_signal = get_feature(previous_page, "mode_1_document_start") if previous_page else 0.0
                if previous_start_signal < mode_1_start_threshold:
                    boundary_reason = BOUNDARY_DOCUMENT_START
            
            if boundary_reason:
                if current_segment:
                    segments.append(current_segment)
                
                current_segment = {
                    "type": page_type,
                    "pages": [page_id],
                    "start_page": page_id,
                    "boundary_reason": boundary_reason
                }
            else:
                current_segment["pages"].append(page_id)
        
        if current_segment:
            segments.append(current_segment)
            
        run_handler.update(run_id, {
            "proposed_segments": segments,
            "status": "boundaries_detected"
        })
        
        logger.info("BOUNDARY_DETECTION_COMPLETE", "SUCCESS", run_id=run_id, message=f"Found {len(segments)} segments")
        
    except Exception as e:
        logger.error("BOUNDARY_DETECTION_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python detect_document_boundaries.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
