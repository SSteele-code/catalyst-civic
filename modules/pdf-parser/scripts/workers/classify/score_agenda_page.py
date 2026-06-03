import sys
import os
import json
import re

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler
from src.common.validation import safe_regex_search

def calculate_agenda_score(text):
    if not text:
        return 0.0, 0.0, 0.0, 0.0, 0.0, [], []
    
    text_lower = text.lower()
    
    # --- EXPANDED SIGNALS: MODE 1 (MEETINGS) ---
    start_signals = [
        r"\bagenda\b",
        r"call to order",
        r"regular meeting",
        r"special meeting",
        r"called meeting",
        r"public hearing",
        r"town council meeting",
        r"council meeting"
    ]

    anchor_signals = [
        r"\bagenda\b",
        r"call to order",
        r"regular meeting",
        r"special meeting",
        r"called meeting",
        r"approval of minutes",
        r"public comments",
        r"adjourn",
        r"\bresolution\b",
        r"\bordinance\b",
        r"council report",
        r"robert's rules",    # 5.1.2
        r"parliamentary",     # 5.1.2
        r"procedure"          # 5.1.2
    ]
    
    continuation_signals = [
        r"agenda item",
        r"minutes",
        r"approval language",
        r"motion",
        r"seconded",
        r"unanimous",
        r"voted",
        r"ayes?",
        r"nays?",
        r"old business",
        r"new business",
        r"council member",
        r"member names",
        r"mayor",
        r"town manager",
        r"staff summary",
        r"action item",
        r"agenda title",
        r"staff contact",
        r"reviewed by",
        r"budget amendments?",
        r"council report",
        r"manager report",
        r"attorney report",
        r"town clerk",
        r"\bresolution\b",
        r"\bordinance\b",
        r"whereas",
        r"be it resolved",
        r"now[, ]+therefore",
        r"attest",
        r"votes?:",
        r"total calls?( for service)?"
    ]

    attachment_signals = [
        r"agency agreement",
        r"task order",
        r"purchase agreement",
        r"contract number",
        r"lease[- ]purchase",
        r"lease agreement",
        r"terms and conditions",
        r"\barticle [ivx]+\b",
        r"duties and obligations",
        r"general provisions",
        r"indemnif(y|ication)",
        r"arbitration",
        r"certificate of coverage",
        r"named insured",
        r"policy number",
        r"opinion of counsel",
        r"documentation package",
        r"meeting minutes request",
        r"internal revenue service",
        r"tax-exempt governmental bonds",
        r"\bform 8038",
        r"installment sale",
        r"\bvendor\b",
        r"\blessee\b",
        r"\bclient\b"
    ]
    
    start_score = 0.0
    anchor_score = 0.0
    continuation_score = 0.0
    attachment_score = 0.0
    matches = []
    attachment_matches = []

    for signal in start_signals:
        if safe_regex_search(signal, text_lower):
            start_score += 0.35
            matches.append(f"start:{signal}")
    
    for signal in anchor_signals:
        if safe_regex_search(signal, text_lower):
            anchor_score += 0.45
            matches.append(signal)
            
    for signal in continuation_signals:
        if safe_regex_search(signal, text_lower):
            continuation_score += 0.20
            matches.append(signal)

    for signal in attachment_signals:
        if safe_regex_search(signal, text_lower):
            attachment_score += 0.15
            attachment_matches.append(signal)
            
    if "appropriation" in text_lower or "budget" in text_lower:
        anchor_score -= 0.1
        continuation_score -= 0.1
        
    final_start = max(0.0, min(1.0, start_score))
    final_anchor = max(0.0, min(1.0, anchor_score))
    final_cont = max(0.0, min(1.0, continuation_score))
    final_attachment = max(0.0, min(1.0, attachment_score))
    
    return max(final_anchor, final_cont), final_anchor, final_cont, final_start, final_attachment, matches, attachment_matches

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    manifest_dir = os.path.join(base_dir, "manifests")
    run_manifest_dir = os.path.join(manifest_dir, "runs")
    page_manifest_dir = os.path.join(manifest_dir, "pages")
    
    logger = PipelineLogger(log_dir, "score_agenda_page")
    run_handler = ManifestHandler(run_manifest_dir)
    page_handler = ManifestHandler(page_manifest_dir)
    
    try:
        run_manifest = run_handler.load(run_id)
        page_ids = run_manifest.get("page_ids", [])
        
        logger.info("AGENDA_SCORING_START", "SUCCESS", run_id=run_id)
        
        for page_id in page_ids:
            page_manifest = page_handler.load(page_id)
            ocr_text_path = page_manifest.get("ocr_text_path")
            
            if not ocr_text_path:
                continue
                
            full_text_path = os.path.join(base_dir, ocr_text_path)
            with open(full_text_path, "r", encoding="utf-8") as f:
                text = f.read()
                
            score, anchor, cont, start_signal, attachment_signal, matches, attachment_matches = calculate_agenda_score(text)
            
            extra_scores = page_manifest.get("classification_scores", {})
            extra_scores["mode_1"] = score
            
            page_handler.update(page_id, {
                "classification_scores": extra_scores,
                "semantic_features": {
                    "mode_1_score": score,
                    "mode_1_anchor": anchor,
                    "mode_1_continuation": cont,
                    "mode_1_attachment": attachment_signal,
                    "mode_1_document_start": start_signal,
                    "mode_1_matched_keywords": matches,
                    "mode_1_attachment_matches": attachment_matches,
                    "mode_1_source": "ocr_text"
                }
            })
            
            logger.info("PAGE_SCORED_MODE_1", "SUCCESS", run_id=run_id, page_id=page_id, message=f"M1: {score:.2f} (A:{anchor:.2f}, C:{cont:.2f})")
            
        logger.info("AGENDA_SCORING_COMPLETE", "SUCCESS", run_id=run_id)
        
    except Exception as e:
        logger.error("AGENDA_SCORING_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python score_agenda_page.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
