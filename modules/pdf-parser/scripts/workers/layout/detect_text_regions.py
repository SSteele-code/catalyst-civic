import sys
import os
import cv2
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler

def detect_regions(image_path):
    image = cv2.imread(image_path)
    if image is None:
        raise ValueError(f"Could not read image: {image_path}")
        
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    
    # Dilation to merge text into blocks
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 5))
    dilate = cv2.dilate(thresh, kernel, iterations=3)
    
    cnts = cv2.findContours(dilate, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cnts = cnts[0] if len(cnts) == 2 else cnts[1]
    
    regions = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if w > 20 and h > 10: # Filter noise
            regions.append({
                "type": "text_block",
                "bbox": [x, y, w, h],
                "confidence": 1.0 # Heuristic detection
            })
            
    return regions

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    manifest_dir = os.path.join(base_dir, "manifests")
    run_manifest_dir = os.path.join(manifest_dir, "runs")
    page_manifest_dir = os.path.join(manifest_dir, "pages")
    region_manifest_dir = os.path.join(manifest_dir, "regions")
    os.makedirs(region_manifest_dir, exist_ok=True)
    config_path = os.path.join(base_dir, "config", "thresholds.json")
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    service = config.get("service", {})
    page_worker_max_workers = max(1, int(service.get("page_worker_max_workers", 1)))
    cv2.setNumThreads(int(service.get("opencv_num_threads", 1)))
    
    logger = PipelineLogger(log_dir, "detect_text_regions")
    run_handler = ManifestHandler(run_manifest_dir)
    page_handler = ManifestHandler(page_manifest_dir)
    
    try:
        run_manifest = run_handler.load(run_id)
        page_ids = run_manifest.get("page_ids", [])
        page_tasks = []
        
        for page_id in page_ids:
            page_manifest = page_handler.load(page_id)
            image_path = os.path.join(base_dir, page_manifest["normalized_image_path"])
            page_tasks.append((page_id, image_path))

        logger.info(
            "TEXT_REGION_DETECTION_START",
            "SUCCESS",
            run_id=run_id,
            message=f"Detecting text regions on {len(page_tasks)} pages with {min(page_worker_max_workers, max(1, len(page_tasks)))} workers."
        )

        max_workers = min(page_worker_max_workers, len(page_tasks)) or 1
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="detect_text_regions") as executor:
            futures = {
                executor.submit(detect_regions, image_path): page_id
                for page_id, image_path in page_tasks
            }

            for future in as_completed(futures):
                page_id = futures[future]
                regions = future.result()

                page_regions = []
                for i, reg in enumerate(regions):
                    region_id = f"{page_id}_REG_{i:04d}"
                    reg_data = {
                        "run_id": run_id,
                        "page_id": page_id,
                        "region_id": region_id,
                        "region_type": reg["type"],
                        "bbox": reg["bbox"],
                        "confidence": reg["confidence"],
                        "source_layout_worker": "detect_text_regions"
                    }

                    reg_path = os.path.join(region_manifest_dir, f"{region_id}.json")
                    with open(reg_path, "w") as f:
                        json.dump(reg_data, f, indent=4)

                    page_regions.append(region_id)

                page_handler.update(page_id, {
                    "layout_path": os.path.join("manifests", "regions"),
                    "region_ids": page_regions,
                    "current_state": "layout_analyzed"
                })

                logger.info("TEXT_REGIONS_DETECTED", "SUCCESS", run_id=run_id, page_id=page_id, message=f"Found {len(regions)} regions")
            
        logger.info("TEXT_REGION_DETECTION_COMPLETE", "SUCCESS", run_id=run_id)
        
    except Exception as e:
        logger.error("TEXT_REGION_DETECTION_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python detect_text_regions.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
