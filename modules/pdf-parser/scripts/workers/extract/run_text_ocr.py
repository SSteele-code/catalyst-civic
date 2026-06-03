import sys
import os
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import pytesseract
from PIL import Image

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler

# Tesseract Configuration
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
WORD_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'&/.-]*")
COMMON_OCR_PUNCTUATION = set(".,;:!?()[]{}'\"-_/\\&%$#@+=*")

def alnum_count(text):
    return sum(1 for ch in (text or "") if ch.isalnum())

def build_text_metrics(text):
    raw_text = text or ""
    tokens = WORD_TOKEN_PATTERN.findall(raw_text)
    lexical_tokens = [
        token for token in tokens
        if len(token) >= 4 and re.search(r"[A-Za-z]", token) and re.search(r"[AEIOUYaeiouy]", token)
    ]
    short_tokens = [token for token in tokens if len(token) <= 2]
    printable_nonspace = [ch for ch in raw_text if ch.isprintable() and not ch.isspace()]
    noise_chars = sum(1 for ch in printable_nonspace if not ch.isalnum() and ch not in COMMON_OCR_PUNCTUATION)
    token_count = len(tokens)
    lexical_ratio = (len(lexical_tokens) / token_count) if token_count else 0.0
    short_ratio = (len(short_tokens) / token_count) if token_count else 0.0
    noise_ratio = (noise_chars / len(printable_nonspace)) if printable_nonspace else 0.0
    return {
        "alnum_count": alnum_count(raw_text),
        "word_count": token_count,
        "lexical_word_count": len(lexical_tokens),
        "lexical_ratio": lexical_ratio,
        "short_word_ratio": short_ratio,
        "noise_char_count": noise_chars,
        "noise_ratio": noise_ratio,
    }

def candidate_score(metrics):
    score = float(metrics["alnum_count"])
    score += float(metrics["word_count"]) * 4.0
    score += float(metrics["lexical_word_count"]) * 18.0
    score += float(metrics["lexical_ratio"]) * 120.0
    score += max(0.0, (1.0 - float(metrics["noise_ratio"]))) * 20.0
    score -= float(metrics["short_word_ratio"]) * 80.0
    score -= float(metrics["noise_char_count"]) * 4.0
    return round(score, 2)

def is_weak_candidate(metrics):
    return (
        int(metrics["alnum_count"]) <= 24
        or int(metrics["word_count"]) <= 6
        or int(metrics["lexical_word_count"]) <= 2
        or (
            float(metrics["lexical_ratio"]) < 0.08
            and float(metrics["short_word_ratio"]) > 0.82
        )
    )

def build_candidate(variant, text, source_image):
    metrics = build_text_metrics(text)
    return {
        "variant": variant,
        "text": text,
        "metrics": metrics,
        "selection_score": candidate_score(metrics),
        "source_image": source_image,
    }

def choose_best_candidate(candidates):
    return max(
        candidates,
        key=lambda item: (
            item["selection_score"],
            item["metrics"]["lexical_word_count"],
            item["metrics"]["lexical_ratio"],
            item["metrics"]["alnum_count"],
        ),
    )

def count_table_regions(page_manifest):
    return sum(1 for rid in page_manifest.get("region_ids", []) if "_TAB_" in rid)

def count_text_regions(page_manifest):
    return sum(1 for rid in page_manifest.get("region_ids", []) if "_TAB_" not in rid)

def ocr_image(image, config=None):
    if config:
        return pytesseract.image_to_string(image, config=config)
    return pytesseract.image_to_string(image)

def should_try_rotation(page_manifest, text, alnum_threshold, table_threshold, skew_threshold):
    table_count = count_table_regions(page_manifest)
    skew_angle = abs(float(page_manifest.get("detected_skew_angle") or 0.0))
    return alnum_count(text) <= alnum_threshold and (
        table_count >= table_threshold or skew_angle >= skew_threshold
    )

def should_rescue_handwriting_route(page_manifest, dense_region_threshold, dense_text_threshold):
    if page_manifest.get("route_type") != "ocr_handwriting_page":
        return False
    table_regions = count_table_regions(page_manifest)
    text_regions = count_text_regions(page_manifest)
    total_regions = table_regions + text_regions
    return total_regions >= dense_region_threshold and text_regions >= dense_text_threshold

def run_ocr_for_page(
    base_dir,
    run_id,
    page_id,
    page_manifest,
    ocr_text_dir,
    ocr_retry_alnum_threshold,
    ocr_retry_table_threshold,
    ocr_retry_skew_threshold,
    handwriting_dense_region_threshold,
    handwriting_dense_text_region_threshold,
):
    route = page_manifest.get("route_type")
    rescue_handwriting_route = should_rescue_handwriting_route(
        page_manifest,
        handwriting_dense_region_threshold,
        handwriting_dense_text_region_threshold
    )

    if route not in ["ocr_text_page", "ocr_mixed_layout_page"] and not rescue_handwriting_route:
        return None

    image_path = os.path.join(base_dir, page_manifest["normalized_image_path"])
    output_filename = f"{page_id}.txt"
    output_path = os.path.join(ocr_text_dir, output_filename)

    with Image.open(image_path) as image:
        image.load()
        base_text = ocr_image(image)
        base_variant = "handwriting_rescue_base_default" if rescue_handwriting_route else "base_default"
        candidates = [build_candidate(base_variant, base_text, image)]

        if should_try_rotation(
            page_manifest,
            base_text,
            ocr_retry_alnum_threshold,
            ocr_retry_table_threshold,
            ocr_retry_skew_threshold
        ):
            candidates.append(build_candidate("base_psm6", ocr_image(image, config="--psm 6"), image))

            rotated_90 = image.rotate(90, expand=True)
            rotated_270 = image.rotate(270, expand=True)
            candidates.append(build_candidate("rot90_psm6", ocr_image(rotated_90, config="--psm 6"), rotated_90))
            candidates.append(build_candidate("rot270_psm6", ocr_image(rotated_270, config="--psm 6"), rotated_270))

        best_candidate = choose_best_candidate(candidates)

        if (
            best_candidate["metrics"]["alnum_count"] <= ocr_retry_alnum_threshold
            or is_weak_candidate(best_candidate["metrics"])
        ):
            sparse_candidate = build_candidate(
                f"{best_candidate['variant']}_psm11",
                ocr_image(best_candidate["source_image"], config="--psm 11"),
                best_candidate["source_image"],
            )
            if sparse_candidate["selection_score"] > best_candidate["selection_score"]:
                candidates.append(sparse_candidate)
                best_candidate = sparse_candidate

        # If normalized OCR looks weak, retry from pre-skew rendered image.
        if is_weak_candidate(best_candidate["metrics"]):
            rendered_relative = page_manifest.get("rendered_image_path")
            if rendered_relative:
                rendered_path = os.path.join(base_dir, rendered_relative)
                if os.path.exists(rendered_path):
                    with Image.open(rendered_path) as rendered_image:
                        rendered_image.load()
                        rot90 = rendered_image.rotate(90, expand=True)
                        rot270 = rendered_image.rotate(270, expand=True)
                        rendered_candidates = [
                            build_candidate("render_default", ocr_image(rendered_image), rendered_image),
                            build_candidate("render_psm3", ocr_image(rendered_image, config="--psm 3"), rendered_image),
                            build_candidate("render_psm4", ocr_image(rendered_image, config="--psm 4"), rendered_image),
                            build_candidate("render_psm6", ocr_image(rendered_image, config="--psm 6"), rendered_image),
                            build_candidate("render_rot90_psm4", ocr_image(rot90, config="--psm 4"), rot90),
                            build_candidate("render_rot270_psm4", ocr_image(rot270, config="--psm 4"), rot270),
                        ]
                    rendered_best = choose_best_candidate(rendered_candidates)
                    if (
                        rendered_best["selection_score"] > best_candidate["selection_score"] + 8.0
                        or (is_weak_candidate(best_candidate["metrics"]) and not is_weak_candidate(rendered_best["metrics"]))
                    ):
                        best_candidate = rendered_best

        variant = best_candidate["variant"]
        text = best_candidate["text"]

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(text)

    return {
        "page_id": page_id,
        "route": route,
        "rescue_handwriting_route": rescue_handwriting_route,
        "variant": variant,
        "ocr_alnum_count": best_candidate["metrics"]["alnum_count"],
        "ocr_text_path": os.path.join("work", "runs", run_id, "ocr_text", output_filename),
    }

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    manifest_dir = os.path.join(base_dir, "manifests")
    run_manifest_dir = os.path.join(manifest_dir, "runs")
    page_manifest_dir = os.path.join(manifest_dir, "pages")

    config_path = os.path.join(base_dir, "config", "thresholds.json")
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    extraction = config.get("extraction", {})
    service = config.get("service", {})
    state_machine = config.get("state_machine", {})
    ocr_retry_alnum_threshold = extraction.get("ocr_retry_alnum_threshold", 24)
    ocr_retry_table_threshold = extraction.get("ocr_retry_table_threshold", 3)
    ocr_retry_skew_threshold = extraction.get("ocr_retry_skew_threshold", 8.0)
    handwriting_dense_region_threshold = state_machine.get("handwriting_dense_region_threshold", 40)
    handwriting_dense_text_region_threshold = state_machine.get("handwriting_dense_text_region_threshold", 20)
    ocr_max_workers = max(1, int(service.get("ocr_max_workers", 1)))
    os.environ["OMP_THREAD_LIMIT"] = str(service.get("tesseract_omp_thread_limit", 1))
    os.environ["OMP_NUM_THREADS"] = str(service.get("tesseract_omp_num_threads", 1))
    
    logger = PipelineLogger(log_dir, "run_text_ocr")
    run_handler = ManifestHandler(run_manifest_dir)
    page_handler = ManifestHandler(page_manifest_dir)
    
    try:
        run_manifest = run_handler.load(run_id)
        page_ids = run_manifest.get("page_ids", [])
        ocr_text_dir = os.path.join(base_dir, "work", "runs", run_id, "ocr_text")
        os.makedirs(ocr_text_dir, exist_ok=True)
        page_tasks = []

        for page_id in page_ids:
            page_manifest = page_handler.load(page_id)
            route = page_manifest.get("route_type")
            rescue_handwriting_route = should_rescue_handwriting_route(
                page_manifest,
                handwriting_dense_region_threshold,
                handwriting_dense_text_region_threshold
            )
            if route in ["ocr_text_page", "ocr_mixed_layout_page"] or rescue_handwriting_route:
                page_tasks.append((page_id, page_manifest))
        
        logger.info(
            "OCR_EXTRACTION_START",
            "SUCCESS",
            run_id=run_id,
            message=f"pages={len(page_tasks)} workers={min(ocr_max_workers, max(1, len(page_tasks)))} omp={os.environ['OMP_THREAD_LIMIT']}"
        )

        if not page_tasks:
            logger.info("OCR_EXTRACTION_COMPLETE", "SUCCESS", run_id=run_id)
            return

        max_workers = min(ocr_max_workers, len(page_tasks))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ocr_page") as executor:
            futures = {
                executor.submit(
                    run_ocr_for_page,
                    base_dir,
                    run_id,
                    page_id,
                    page_manifest,
                    ocr_text_dir,
                    ocr_retry_alnum_threshold,
                    ocr_retry_table_threshold,
                    ocr_retry_skew_threshold,
                    handwriting_dense_region_threshold,
                    handwriting_dense_text_region_threshold,
                ): page_id
                for page_id, page_manifest in page_tasks
            }

            for future in as_completed(futures):
                result = future.result()
                if result is None:
                    continue
                page_id = result["page_id"]
                page_handler.update(page_id, {
                    "ocr_text_path": result["ocr_text_path"],
                    "extraction_engine_used": "tesseract_v5",
                    "ocr_variant_used": result["variant"],
                    "ocr_alnum_count": result["ocr_alnum_count"],
                    "current_state": "extraction_complete"
                })

                logger.info(
                    "OCR_EXTRACTED",
                    "SUCCESS",
                    run_id=run_id,
                    page_id=page_id,
                    message=f"route={result['route']} rescue={result['rescue_handwriting_route']} variant={result['variant']} alnum={result['ocr_alnum_count']}"
                )
            
        logger.info("OCR_EXTRACTION_COMPLETE", "SUCCESS", run_id=run_id)
        
    except Exception as e:
        logger.error("OCR_EXTRACTION_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_text_ocr.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
