import sys
import os
import json

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler
from src.common.constants import MODE_1_AGENDA, MODE_2_BUDGET, MODE_0_UNKNOWN

def get_feature(page, key, default=0.0):
    return page.get("semantic_features", {}).get(key, default)

def get_table_count(page):
    return sum(1 for rid in page.get("region_ids", []) if "_TAB_" in rid)

def get_skew_abs(page):
    try:
        return abs(float(page.get("detected_skew_angle") or 0.0))
    except (TypeError, ValueError):
        return 0.0

def get_alnum_count(base_dir, page):
    ocr_text_path = page.get("ocr_text_path")
    if not ocr_text_path:
        return 0

    full_text_path = os.path.join(base_dir, ocr_text_path)
    if not os.path.exists(full_text_path):
        return 0

    with open(full_text_path, "r", encoding="utf-8") as f:
        text = f.read() or ""

    return sum(1 for ch in text if ch.isalnum())

def set_page_type(page_handler, logger, run_id, page_data, page, page_type, confidence, reason, state="recovered"):
    page_handler.update(page["page_id"], {
        "page_type": page_type,
        "page_type_confidence": confidence,
        "decision_reason": reason,
        "escalation_policy": "none",
        "current_state": state
    })
    page["page_type"] = page_type
    page["page_type_confidence"] = confidence
    page["decision_reason"] = reason
    page["escalation_policy"] = "none"
    page["current_state"] = state
    logger.info("PAGE_RECOVERED", "SUCCESS", run_id=run_id, page_id=page["page_id"],
                message=f"Recovered as {page_type} via {reason}.")

def is_low_content_blank(base_dir, page, alnum_threshold):
    ocr_text_path = page.get("ocr_text_path")
    if not ocr_text_path:
        return False

    scores = page.get("classification_scores", {})
    if (
        scores.get(MODE_1_AGENDA, 0.0) > 0.15
        or scores.get(MODE_2_BUDGET, 0.0) > 0.20
        or get_feature(page, "mode_1_attachment") > 0.20
    ):
        return False

    if get_table_count(page) > 1:
        return False

    return get_alnum_count(base_dir, page) <= alnum_threshold

def is_mode2_visual_candidate(base_dir, page, low_content_alnum_threshold, score_threshold, table_threshold, skew_threshold):
    if is_low_content_blank(base_dir, page, low_content_alnum_threshold):
        return False
    if get_feature(page, "mode_1_attachment") >= 0.25:
        return False
    if get_feature(page, "mode_2_legal_context") >= 0.30:
        return False

    mode_2_score = page.get("classification_scores", {}).get(MODE_2_BUDGET, 0.0)
    if mode_2_score >= score_threshold:
        return True

    return get_table_count(page) >= table_threshold and get_skew_abs(page) >= skew_threshold

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    manifest_dir = os.path.join(base_dir, "manifests")
    run_manifest_dir = os.path.join(manifest_dir, "runs")
    page_manifest_dir = os.path.join(manifest_dir, "pages")
    
    logger = PipelineLogger(log_dir, "recover_unknown_pages")
    run_handler = ManifestHandler(run_manifest_dir)
    page_handler = ManifestHandler(page_manifest_dir)

    config_path = os.path.join(base_dir, "config", "thresholds.json")
    with open(config_path, "r") as f:
        config = json.load(f)
    thresholds = config.get("classification", {})
    segmentation = config.get("segmentation", {})
    mode_1_recovery_threshold = thresholds.get("mode_1_recovery_threshold", 0.35)
    mode_1_recovery_counter = thresholds.get("mode_1_recovery_counter_threshold", 0.15)
    mode_1_attachment_threshold = thresholds.get("mode_1_attachment_threshold", 0.30)
    attachment_chain_counter = thresholds.get("mode_1_attachment_counter_threshold", 0.55)
    mode_2_visual_bridge_threshold = thresholds.get("mode_2_visual_bridge_threshold", 0.30)
    mode_2_visual_bridge_table_threshold = thresholds.get("mode_2_visual_bridge_table_threshold", 3)
    mode_2_visual_bridge_skew_threshold = thresholds.get("mode_2_visual_bridge_skew_threshold", 8.0)
    low_content_alnum_threshold = segmentation.get("low_content_alnum_threshold", 24)
    
    try:
        run_manifest = run_handler.load(run_id)
        page_ids = run_manifest.get("page_ids", [])
        
        logger.info("UNKNOWN_RECOVERY_START", "SUCCESS", run_id=run_id)
        
        # Load all page data
        page_data = [page_handler.load(pid) for pid in page_ids]
        recovered_count = 0
        
        for i in range(len(page_data)):
            curr_p = page_data[i]
            
            if curr_p.get("page_type") != MODE_0_UNKNOWN:
                continue

            if is_low_content_blank(base_dir, curr_p, low_content_alnum_threshold):
                continue

            scores = curr_p.get("classification_scores", {})
            mode_1_score = scores.get(MODE_1_AGENDA, 0.0)
            mode_2_score = scores.get(MODE_2_BUDGET, 0.0)
            mode_1_anchor = get_feature(curr_p, "mode_1_anchor")
            mode_1_cont = get_feature(curr_p, "mode_1_continuation")

            if (
                mode_1_score >= mode_1_recovery_threshold
                and mode_2_score <= mode_1_recovery_counter
                and (mode_1_anchor >= 0.35 or mode_1_cont >= 0.35)
            ):
                set_page_type(
                    page_handler, logger, run_id, page_data, curr_p,
                    MODE_1_AGENDA, mode_1_score, "MODE1_LOW_CONFIDENCE_RECOVERY"
                )
                recovered_count += 1
                continue
                
            # SANDWICH LOGIC: If Page N-1 and Page N+1 are the SAME mode, Page N is likely the same
            prev_p = page_data[i-1] if i > 0 else None
            next_p = page_data[i+1] if i < len(page_data) - 1 else None
            
            if prev_p and next_p:
                prev_type = prev_p.get("page_type")
                next_type = next_p.get("page_type")
                
                if prev_type == next_type and prev_type != MODE_0_UNKNOWN:
                    set_page_type(
                        page_handler, logger, run_id, page_data, curr_p,
                        prev_type, 0.5, f"SANDWICH_RECOVERY_{prev_type}"
                    )
                    recovered_count += 1
                    continue

            # CLUSTER LOGIC: If Page N-1 and N-2 are the same, Page N might be a tail
            if i > 1:
                prev_1 = page_data[i-1]
                prev_2 = page_data[i-2]
                if prev_1.get("page_type") == prev_2.get("page_type") and prev_1.get("page_type") != MODE_0_UNKNOWN:
                    # Check for even weak continuation signals
                    cont_signal = get_feature(curr_p, f"{prev_1.get('page_type')}_continuation")
                    if cont_signal > 0.05:
                        set_page_type(
                            page_handler, logger, run_id, page_data, curr_p,
                            prev_1.get("page_type"), 0.4, "CLUSTER_TAIL_RECOVERY"
                        )
                        recovered_count += 1

        i = 0
        while i < len(page_data):
            if page_data[i].get("page_type") != MODE_0_UNKNOWN:
                i += 1
                continue

            start = i
            while i + 1 < len(page_data) and page_data[i + 1].get("page_type") == MODE_0_UNKNOWN:
                i += 1
            end = i

            prev_page = page_data[start - 1] if start > 0 else None
            next_page = page_data[end + 1] if end + 1 < len(page_data) else None
            prev_type = prev_page.get("page_type") if prev_page else None
            next_type = next_page.get("page_type") if next_page else None
            run_pages = page_data[start:end + 1]

            if prev_type == MODE_1_AGENDA and next_type == MODE_1_AGENDA:
                attachment_hits = 0
                nonblank_hits = 0
                strong_mode_2_hits = 0

                for page in run_pages:
                    mode_2_score = page.get("classification_scores", {}).get(MODE_2_BUDGET, 0.0)
                    if get_feature(page, "mode_1_attachment") >= mode_1_attachment_threshold:
                        attachment_hits += 1
                    if not is_low_content_blank(base_dir, page, low_content_alnum_threshold):
                        nonblank_hits += 1
                    if mode_2_score > attachment_chain_counter or get_feature(page, "mode_2_anchor") >= 0.40:
                        strong_mode_2_hits += 1

                recover_reason = None
                if strong_mode_2_hits == 0 and attachment_hits > 0:
                    recover_reason = "ATTACHMENT_CHAIN_RECOVERY"
                elif strong_mode_2_hits == 0 and len(run_pages) <= 2 and nonblank_hits > 0:
                    recover_reason = "MODE1_SHORT_GAP_RECOVERY"

                if recover_reason:
                    for page in run_pages:
                        if is_low_content_blank(base_dir, page, low_content_alnum_threshold):
                            continue
                        confidence = max(
                            page.get("classification_scores", {}).get(MODE_1_AGENDA, 0.0),
                            get_feature(page, "mode_1_attachment"),
                            0.4
                        )
                        set_page_type(
                            page_handler, logger, run_id, page_data, page,
                            MODE_1_AGENDA, confidence, recover_reason
                        )
                        recovered_count += 1

            if prev_type == MODE_2_BUDGET or next_type == MODE_2_BUDGET:
                seed_offsets = []
                for offset, page in enumerate(run_pages):
                    if page.get("page_type") != MODE_0_UNKNOWN:
                        continue
                    if is_low_content_blank(base_dir, page, low_content_alnum_threshold):
                        continue
                    if not is_mode2_visual_candidate(
                        base_dir,
                        page,
                        low_content_alnum_threshold,
                        mode_2_visual_bridge_threshold,
                        mode_2_visual_bridge_table_threshold,
                        mode_2_visual_bridge_skew_threshold
                    ):
                        continue

                    absolute_index = start + offset
                    prev_adjacent = page_data[absolute_index - 1] if absolute_index > 0 else None
                    next_adjacent = page_data[absolute_index + 1] if absolute_index + 1 < len(page_data) else None
                    immediate_mode_2_neighbor = (
                        (prev_adjacent and prev_adjacent.get("page_type") == MODE_2_BUDGET)
                        or (next_adjacent and next_adjacent.get("page_type") == MODE_2_BUDGET)
                    )
                    mode_2_score = page.get("classification_scores", {}).get(MODE_2_BUDGET, 0.0)

                    if not immediate_mode_2_neighbor and mode_2_score < mode_2_visual_bridge_threshold:
                        continue

                    seed_offsets.append(offset)
                    confidence = max(
                        mode_2_score,
                        get_feature(page, "mode_2_anchor"),
                        get_feature(page, "mode_2_continuation"),
                        0.4
                    )
                    set_page_type(
                        page_handler, logger, run_id, page_data, page,
                        MODE_2_BUDGET, confidence, "MODE2_VISUAL_BRIDGE_RECOVERY"
                    )
                    recovered_count += 1

                if seed_offsets:
                    for offset, page in enumerate(run_pages):
                        if page.get("page_type") != MODE_0_UNKNOWN:
                            continue
                        if is_low_content_blank(base_dir, page, low_content_alnum_threshold):
                            continue
                        if page.get("classification_scores", {}).get(MODE_1_AGENDA, 0.0) >= 0.55:
                            continue
                        if get_feature(page, "mode_1_continuation") >= 0.40:
                            continue
                        if not is_mode2_visual_candidate(
                            base_dir,
                            page,
                            low_content_alnum_threshold,
                            mode_2_visual_bridge_threshold,
                            mode_2_visual_bridge_table_threshold,
                            mode_2_visual_bridge_skew_threshold
                        ):
                            continue
                        if min(abs(offset - seed_offset) for seed_offset in seed_offsets) > 2:
                            continue

                        confidence = max(
                            page.get("classification_scores", {}).get(MODE_2_BUDGET, 0.0),
                            get_feature(page, "mode_2_anchor"),
                            get_feature(page, "mode_2_continuation"),
                            0.35
                        )
                        set_page_type(
                            page_handler, logger, run_id, page_data, page,
                            MODE_2_BUDGET, confidence, "MODE2_CLUSTER_BRIDGE_RECOVERY"
                        )
                        recovered_count += 1

            i += 1

        logger.info("UNKNOWN_RECOVERY_COMPLETE", "SUCCESS", run_id=run_id, message=f"Recovered {recovered_count} pages.")
        
    except Exception as e:
        logger.error("UNKNOWN_RECOVERY_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python recover_unknown_pages.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
