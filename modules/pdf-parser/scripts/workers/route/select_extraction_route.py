import sys
import os
import json

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    manifest_dir = os.path.join(base_dir, "manifests")
    run_manifest_dir = os.path.join(manifest_dir, "runs")
    page_manifest_dir = os.path.join(manifest_dir, "pages")
    
    # Load thresholds
    config_path = os.path.join(base_dir, "config", "thresholds.json")
    with open(config_path, "r") as f:
        thresholds = json.load(f)
    
    QUALITY_MIN = thresholds["extraction"]["native_text_quality_threshold"]
    
    logger = PipelineLogger(log_dir, "select_extraction_route")
    run_handler = ManifestHandler(run_manifest_dir)
    page_handler = ManifestHandler(page_manifest_dir)
    
    try:
        run_manifest = run_handler.load(run_id)
        page_ids = run_manifest.get("page_ids", [])
        
        logger.info("ROUTE_SELECTION_START", "SUCCESS", run_id=run_id)
        
        for page_id in page_ids:
            page_manifest = page_handler.load(page_id)
            
            # --- DISJOINT SIGNAL SET: STRUCTURAL (ROUTING ONLY) ---
            # 1. Native Quality (Structural)
            has_native = page_manifest.get("native_text_detected", False)
            quality = page_manifest.get("native_text_quality_score", 0.0)
            
            # 2. Layout Complexity (Structural)
            region_ids = page_manifest.get("region_ids", [])
            has_table_structure = any("_TAB_" in rid for rid in region_ids)
            has_handwriting = page_manifest.get("handwriting_detected", False) # For 3.2.2
            
            # ROUTING DECISION MATRIX (Spec Section 11)
            route = "manual_review_required"
            
            if has_handwriting:
                route = "ocr_handwriting_page"
            elif has_native and quality >= QUALITY_MIN:
                route = "native_text_only"
                if has_table_structure:
                    route = "native_text_plus_layout"
            else:
                route = "ocr_text_page"
                if has_table_structure:
                    route = "ocr_mixed_layout_page"
            
            page_handler.update(page_id, {
                "route_type": route,
                "route_confidence": 1.0,
                "current_state": "route_selected",
                "structural_signals": {
                    "has_native": has_native,
                    "native_quality": quality,
                    "has_table_structure": has_table_structure
                }
            })
            
            logger.info("ROUTE_SELECTED", "SUCCESS", run_id=run_id, page_id=page_id, message=f"Route: {route}")
            
        logger.info("ROUTE_SELECTION_COMPLETE", "SUCCESS", run_id=run_id)
        
    except Exception as e:
        logger.error("ROUTE_SELECTION_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python select_extraction_route.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
