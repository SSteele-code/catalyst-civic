import sys
import os
import json

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler
from src.common.constants import ESCALATION_HUMAN_REVIEW, MODE_1_AGENDA, MODE_2_BUDGET, MODE_0_UNKNOWN

def get_feature(page, key, default=0.0):
    return page.get("semantic_features", {}).get(key, default)

def resolve_page_type_with_context(prev_page, current_page, next_page, thresholds):
    """
    4.2.1: Neighbor-aware classification logic.
    5.1.1: Competitive Carry-Forward (Tightened).
    """
    scores = current_page.get("classification_scores", {})
    mode_1_score = scores.get(MODE_1_AGENDA, 0.0)
    mode_2_score = scores.get(MODE_2_BUDGET, 0.0)
    switch_margin = thresholds.get("competitive_switch_margin", 0.25)
    carry_threshold = thresholds.get("carry_forward_threshold", 0.20)
    look_ahead_current = thresholds.get("look_ahead_current_score_threshold", 0.45)
    look_ahead_neighbor = thresholds.get("look_ahead_neighbor_score_threshold", 0.85)
    mode_1_semantic_threshold = thresholds.get("mode_1_semantic_fallback_threshold", 0.55)
    mode_1_semantic_counter = thresholds.get("mode_1_semantic_counter_threshold", 0.25)
    mode_1_attachment_threshold = thresholds.get("mode_1_attachment_threshold", 0.30)
    mode_1_attachment_counter = thresholds.get("mode_1_attachment_counter_threshold", 0.55)
    
    # 1. Direct Anchor Detection (Strongest Signal)
    m1_anchor = get_feature(current_page, "mode_1_anchor")
    m2_anchor = get_feature(current_page, "mode_2_anchor")
    
    if m1_anchor >= thresholds["mode_1_score_threshold"]:
        return MODE_1_AGENDA, m1_anchor, "DIRECT_ANCHOR_M1"
    if m2_anchor >= thresholds["mode_2_score_threshold"]:
        return MODE_2_BUDGET, m2_anchor, "DIRECT_ANCHOR_M2"
        
    # 2. Competitive Section Carry-Forward (5.1.1 Fix)
    if prev_page:
        prev_type = prev_page.get("page_type")
        if prev_type in [MODE_1_AGENDA, MODE_2_BUDGET]:
            cont_key = f"{prev_type}_continuation"
            cont_signal = get_feature(current_page, cont_key)
            
            # COMPETE: Only carry forward if the alternate mode isn't stronger
            alt_mode = MODE_2_BUDGET if prev_type == MODE_1_AGENDA else MODE_1_AGENDA
            alt_score = scores.get(alt_mode, 0.0)
            
            if cont_signal >= carry_threshold:
                # TIGHTEN: If the alternative mode has a significantly higher score, switch instead of carry
                if alt_score > (cont_signal + switch_margin):
                    return alt_mode, alt_score, f"COMPETITIVE_SWITCH_TO_{alt_mode}"
                
                return prev_type, cont_signal, f"CARRY_FORWARD_FROM_{prev_type}"

    # 2b. Attachment continuity for meeting-packet exhibits, contracts, bond forms, and similar addenda
    attachment_signal = get_feature(current_page, "mode_1_attachment")
    if (
        prev_page
        and prev_page.get("page_type") == MODE_1_AGENDA
        and attachment_signal >= mode_1_attachment_threshold
        and mode_2_score <= mode_1_attachment_counter
        and get_feature(current_page, "mode_2_anchor") < 0.40
    ):
        return MODE_1_AGENDA, max(mode_1_score, attachment_signal), "MODE1_ATTACHMENT_CONTINUITY"

    # 3. Mode 1 semantic fallback for legislative / report pages that are clearly meeting-adjacent
    if (
        mode_1_score >= mode_1_semantic_threshold
        and mode_2_score <= mode_1_semantic_counter
        and (
            get_feature(current_page, "mode_1_anchor") >= 0.35
            or get_feature(current_page, "mode_1_continuation") >= 0.40
        )
    ):
        return MODE_1_AGENDA, mode_1_score, "MODE1_SEMANTIC_FALLBACK"

    # 4. Look-Ahead Logic
    if next_page:
        n_scores = next_page.get("classification_scores", {})
        for mode in [MODE_1_AGENDA, MODE_2_BUDGET]:
            if scores.get(mode, 0.0) >= look_ahead_current and n_scores.get(mode, 0.0) >= look_ahead_neighbor:
                return mode, scores.get(mode), f"LOOK_AHEAD_MATCH_{mode}"

    # 5. Fallback: Highest direct score above review threshold
    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    if sorted_scores and sorted_scores[0][1] >= thresholds["review_threshold"]:
        return sorted_scores[0][0], sorted_scores[0][1], "DIRECT_SCORE_FALLBACK"
        
    return MODE_0_UNKNOWN, 0.0, "INSUFFICIENT_SIGNALS"

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    manifest_dir = os.path.join(base_dir, "manifests")
    run_manifest_dir = os.path.join(manifest_dir, "runs")
    page_manifest_dir = os.path.join(manifest_dir, "pages")
    
    # Load thresholds
    config_path = os.path.join(base_dir, "config", "thresholds.json")
    with open(config_path, "r") as f:
        config = json.load(f)
    thresholds = config["classification"]
    
    logger = PipelineLogger(log_dir, "resolve_page_type")
    run_handler = ManifestHandler(run_manifest_dir)
    page_handler = ManifestHandler(page_manifest_dir)
    
    try:
        run_manifest = run_handler.load(run_id)
        page_ids = run_manifest.get("page_ids", [])
        
        logger.info("TYPE_RESOLUTION_START", "SUCCESS", run_id=run_id, message="Starting Sliding Window Resolution (N=3)")
        
        page_data = []
        for pid in page_ids:
            page_data.append(page_handler.load(pid))
            
        for i in range(len(page_data)):
            prev_p = page_data[i-1] if i > 0 else None
            curr_p = page_data[i]
            next_p = page_data[i+1] if i < len(page_data) - 1 else None
            
            resolved_type, conf, reason = resolve_page_type_with_context(prev_p, curr_p, next_p, thresholds)
            curr_p["page_type"] = resolved_type
            
            escalation = "none"
            if resolved_type == MODE_0_UNKNOWN:
                escalation = ESCALATION_HUMAN_REVIEW
                
            page_handler.update(curr_p["page_id"], {
                "page_type": resolved_type,
                "page_type_confidence": conf,
                "decision_reason": reason,
                "escalation_policy": escalation,
                "current_state": "tagged"
            })
            
            logger.info("PAGE_RESOLVED", "SUCCESS", run_id=run_id, page_id=curr_p["page_id"], 
                        message=f"Resolved: {resolved_type} | Reason: {reason} | Conf: {conf:.2f}")
            
        logger.info("TYPE_RESOLUTION_COMPLETE", "SUCCESS", run_id=run_id)
        
    except Exception as e:
        logger.error("TYPE_RESOLUTION_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python resolve_page_type.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
