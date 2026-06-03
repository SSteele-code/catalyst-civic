import sys
import os
import json
import re

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler
from src.common.validation import safe_regex_search

def calculate_budget_score(text):
    if not text:
        return 0.0, 0.0, 0.0, [], 0.0, 0.0
    
    text_lower = text.lower()
    
    # --- EXPANDED SIGNALS: MODE 2 (BUDGET/FINANCE) ---
    anchor_signals = [
        r"\bbudget\b",
        r"fiscal year",
        r"appropriation",
        r"updated financial policies",
        r"financial policies",
        r"financial report",
        r"monthly financial",
        r"income statement",
        r"statement of revenues",
        r"statement of expenditures",
        r"trial balance",
        r"check register",
        r"paid checks",
        r"bank account",
        r"cash balances?",
        r"fund balance",
        r"fund balance policy",
        r"reserve analysis",
        r"cash management",
        r"debt management",
        r"capital improvement",
        r"\bcip\b",
        r"enterprise fund",
        r"general fund"
    ]
    
    continuation_signals = [
        r"amendment",
        r"revenue",
        r"expenditure",
        r"account",
        r"balance",
        r"transfers",
        r"debt",
        r"lease",
        r"proposed",
        r"approved",
        r"department total",
        r"account number",
        r"cash",
        r"receipts?",
        r"disbursements?",
        r"\$\d{1,3}(,\d{3})*(\.\d{2})?" # Dollar values
    ]

    suppress_signals = [
        r"guiding principles",
        r"standards",
        r"task order",
        r"\bagreement\b",
        r"document checklist",
        r"opinion of counsel",
        r"application",
        r"endorsement",
        r"certificate of coverage",
        r"insurance",
        r"purchase agreement",
        r"additional terms",
        r"staff summary"
    ]

    meeting_context_signals = [
        r"town council meeting",
        r"council meeting",
        r"regular meeting",
        r"special meeting",
        r"call to order",
        r"attorney report",
        r"town manager report",
        r"council members? report",
        r"executive closed session",
        r"adjourn meeting",
        r"mayor'?s comments?",
        r"upon a motion",
        r"motion by",
        r"seconded",
        r"unanimous",
        r"council voted",
        r"action item",
        r"agenda title",
        r"staff contact",
        r"reviewed by"
    ]

    packet_cover_signals = [
        r"staff summary",
        r"action item",
        r"agenda title",
        r"staff contact",
        r"reviewed by"
    ]

    legal_document_signals = [
        r"agency agreement",
        r"purchase agreement",
        r"\bagreement\b",
        r"contract number",
        r"lease[- ]purchase",
        r"lease agreement",
        r"terms and conditions",
        r"\barticle [ivx]+\b",
        r"duties and obligations",
        r"general provisions",
        r"confidential",
        r"prior written consent",
        r"termination",
        r"terminate(d|ion)",
        r"arbitration",
        r"indemnif(y|ication)",
        r"hold harmless",
        r"independent contractors?",
        r"certificate of coverage",
        r"named insured",
        r"policy number",
        r"opinion of counsel",
        r"internal revenue service",
        r"tax-exempt governmental bonds",
        r"\bform 8038",
        r"meeting minutes request",
        r"documentation package",
        r"\bvendor\b",
        r"\blessee\b",
        r"\bclient\b"
    ]
    
    anchor_score = 0.0
    continuation_score = 0.0
    suppress_score = 0.0
    meeting_context_score = 0.0
    packet_cover_score = 0.0
    legal_document_score = 0.0
    matches = []
    
    for signal in anchor_signals:
        if safe_regex_search(signal, text_lower):
            anchor_score += 0.40
            matches.append(signal)
            
    for signal in continuation_signals:
        if safe_regex_search(signal, text_lower):
            continuation_score += 0.15
            matches.append(signal)

    for signal in suppress_signals:
        if safe_regex_search(signal, text_lower):
            suppress_score += 0.25
            matches.append(f"suppress:{signal}")

    for signal in meeting_context_signals:
        if safe_regex_search(signal, text_lower):
            meeting_context_score += 0.20
            matches.append(f"meeting:{signal}")

    for signal in packet_cover_signals:
        if safe_regex_search(signal, text_lower):
            packet_cover_score += 0.25
            matches.append(f"cover:{signal}")

    for signal in legal_document_signals:
        if safe_regex_search(signal, text_lower):
            legal_document_score += 0.20
            matches.append(f"legal:{signal}")
            
    # Contextual booster: High density of dollar signs
    dollar_count = text.count("$")
    if dollar_count > 10:
        continuation_score += 0.3
        matches.append("high_dollar_density")

    if suppress_score > 0.0:
        anchor_score = max(0.0, anchor_score - suppress_score)
        continuation_score = max(0.0, continuation_score - min(0.30, suppress_score * 0.5))

    if meeting_context_score > 0.0:
        anchor_score = max(0.0, anchor_score - min(0.85, meeting_context_score))
        continuation_score = max(0.0, continuation_score - min(0.45, meeting_context_score * 0.5))

    if legal_document_score > 0.0:
        anchor_score = max(0.0, anchor_score - min(1.0, legal_document_score))
        continuation_score = max(0.0, continuation_score - min(0.75, legal_document_score * 0.75))

        # Legal / procurement prose should not qualify as finance on generic numbers and tables alone.
        if anchor_score < 0.40 and "high_dollar_density" not in matches:
            continuation_score = min(continuation_score, 0.25)

    if packet_cover_score >= 0.75 and meeting_context_score >= 0.40:
        anchor_score = min(anchor_score, 0.25)
        continuation_score = min(continuation_score, 0.25)
        matches.append("packet_cover_blocks_mode_2")
        
    final_anchor = max(0.0, min(1.0, anchor_score))
    final_cont = max(0.0, min(1.0, continuation_score))
    
    return max(final_anchor, final_cont), final_anchor, final_cont, matches, meeting_context_score, legal_document_score

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    manifest_dir = os.path.join(base_dir, "manifests")
    run_manifest_dir = os.path.join(manifest_dir, "runs")
    page_manifest_dir = os.path.join(manifest_dir, "pages")
    
    logger = PipelineLogger(log_dir, "score_budget_page")
    run_handler = ManifestHandler(run_manifest_dir)
    page_handler = ManifestHandler(page_manifest_dir)
    
    try:
        run_manifest = run_handler.load(run_id)
        page_ids = run_manifest.get("page_ids", [])
        
        logger.info("BUDGET_SCORING_START", "SUCCESS", run_id=run_id)
        
        for page_id in page_ids:
            page_manifest = page_handler.load(page_id)
            ocr_text_path = page_manifest.get("ocr_text_path")
            
            if not ocr_text_path:
                continue
                
            full_text_path = os.path.join(base_dir, ocr_text_path)
            with open(full_text_path, "r", encoding="utf-8") as f:
                text = f.read()
                
            score, anchor, cont, matches, meeting_context_score, legal_document_score = calculate_budget_score(text)
            
            # --- VISUAL-SEMANTIC LINK (5.2.2) ---
            # Boost score if layout analysis found table/chart regions
            region_ids = page_manifest.get("region_ids", [])
            table_count = sum(1 for rid in region_ids if "_TAB_" in rid)
            
            if table_count > 0:
                visual_support = min(0.20, table_count * 0.04)
                if meeting_context_score >= 0.40:
                    matches.append("meeting_context_blocks_visual_support")
                    matches.append(f"visual_table_detected_x{table_count}")
                elif legal_document_score >= 0.40 and anchor < 0.40 and "high_dollar_density" not in matches:
                    matches.append("legal_context_blocks_visual_support")
                    matches.append(f"visual_table_detected_x{table_count}")
                elif anchor >= 0.40 or "high_dollar_density" in matches:
                    score = min(1.0, score + visual_support)
                    anchor = min(1.0, anchor + min(0.10, visual_support * 0.5))
                    cont = min(1.0, cont + visual_support)
                    matches.append(f"visual_table_support_x{table_count}")
                else:
                    matches.append(f"visual_table_detected_x{table_count}")
            
            extra_scores = page_manifest.get("classification_scores", {})
            extra_scores["mode_2"] = score
            
            page_handler.update(page_id, {
                "classification_scores": extra_scores,
                "semantic_features": {
                    "mode_2_score": score,
                    "mode_2_anchor": anchor,
                    "mode_2_continuation": cont,
                    "mode_2_meeting_context": meeting_context_score,
                    "mode_2_legal_context": legal_document_score,
                    "mode_2_matched_keywords": matches,
                    "mode_2_source": "ocr_text+visual_layout"
                }
            })
            
            logger.info("PAGE_SCORED_MODE_2", "SUCCESS", run_id=run_id, page_id=page_id, message=f"M2: {score:.2f} (A:{anchor:.2f}, C:{cont:.2f}, Tabs:{table_count})")
            
        logger.info("BUDGET_SCORING_COMPLETE", "SUCCESS", run_id=run_id)
        
    except Exception as e:
        logger.error("BUDGET_SCORING_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python score_budget_page.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
