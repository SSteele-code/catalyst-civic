import sys
import os
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import fitz # PyMuPDF

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler

def render_page(page_pdf_path, output_path):
    doc = fitz.open(page_pdf_path)
    try:
        page = doc[0]
        zoom = 300 / 72
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
        pix.save(output_path)
    finally:
        doc.close()

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    manifest_dir = os.path.join(base_dir, "manifests")
    run_manifest_dir = os.path.join(manifest_dir, "runs")
    page_manifest_dir = os.path.join(manifest_dir, "pages")
    config_path = os.path.join(base_dir, "config", "thresholds.json")
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    page_worker_max_workers = max(1, int(config.get("service", {}).get("page_worker_max_workers", 1)))
    
    logger = PipelineLogger(log_dir, "render_pdf_page_to_image")
    run_handler = ManifestHandler(run_manifest_dir)
    page_handler = ManifestHandler(page_manifest_dir)
    
    try:
        run_manifest = run_handler.load(run_id)
        page_ids = run_manifest.get("page_ids", [])
        render_dir = os.path.join(base_dir, "work", "runs", run_id, "pages_rendered")
        os.makedirs(render_dir, exist_ok=True)
        page_tasks = []

        for page_id in page_ids:
            page_manifest = page_handler.load(page_id)
            page_pdf_path = os.path.join(base_dir, page_manifest["raw_pdf_path"])
            output_filename = f"{page_id}.png"
            output_path = os.path.join(render_dir, output_filename)
            page_tasks.append((page_id, output_filename, page_pdf_path, output_path))
        
        logger.info(
            "RENDER_START",
            "SUCCESS",
            run_id=run_id,
            message=f"Rendering {len(page_tasks)} pages with {min(page_worker_max_workers, max(1, len(page_tasks)))} workers."
        )

        max_workers = min(page_worker_max_workers, len(page_tasks)) or 1
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="render_page") as executor:
            futures = {
                executor.submit(render_page, page_pdf_path, output_path): (page_id, output_filename)
                for page_id, output_filename, page_pdf_path, output_path in page_tasks
            }

            for future in as_completed(futures):
                page_id, output_filename = futures[future]
                future.result()
                page_handler.update(page_id, {
                    "rendered_image_path": os.path.join("work", "runs", run_id, "pages_rendered", output_filename),
                    "current_state": "rendered"
                })
                logger.info("PAGE_RENDERED", "SUCCESS", run_id=run_id, page_id=page_id, message=f"Created {output_filename}")
            
        run_handler.update(run_id, {"status": "render_complete"})
        logger.info("RENDER_COMPLETE", "SUCCESS", run_id=run_id)
        
    except Exception as e:
        logger.error("RENDER_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python render_pdf_page_to_image.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
