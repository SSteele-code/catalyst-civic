import os
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path


DEFAULT_PARSER_OUTBOX = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Modules" / "PDF Parser" / "outbox"
DEFAULT_OUTPUT_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Modes" / "M1" / "Agenda"
MACHINE_CODE_PATTERN = re.compile(r"^M\d+\.[A-Z0-9]{2,6}\.\d{6}\.\d{8}\.\d{8}$", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write agenda-only pages from parser machine_readable/pages.jsonl into one folder + one file."
    )
    parser.add_argument("--machine-readable", type=Path, help="Explicit machine_readable folder path.")
    parser.add_argument("--parser-outbox", type=Path, default=DEFAULT_PARSER_OUTBOX, help="Parser outbox root.")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT, help="Sort_Station output root.")
    parser.add_argument(
        "--latest-only",
        action="store_true",
        help="When --machine-readable is not supplied, use only the most recent parser outbox run.",
    )
    return parser.parse_args()


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if text:
                yield json.loads(text)


def looks_like_machine_code(value: str | None) -> bool:
    if not value:
        return False
    return bool(MACHINE_CODE_PATTERN.fullmatch(value.strip()))


def machine_code_from_filename(value: str | None) -> str | None:
    if not value:
        return None
    stem = Path(str(value).strip()).stem.strip()
    if looks_like_machine_code(stem):
        return stem
    return None


def pick_machine_code(machine_readable: Path, run_json: dict) -> str:
    for key in (
        "source_pdf_name",
        "source_pdf_display_name",
        "source_pdf_original_name",
        "source_pdf_alias_name",
        "source_pdf_intake_name",
    ):
        code = machine_code_from_filename(run_json.get(key))
        if code:
            return code

    doc_code = str(run_json.get("document_machine_code") or "").strip()
    if doc_code:
        return doc_code

    return machine_readable.parent.name


def is_agenda_page(record: dict) -> bool:
    page = record.get("page") or {}
    routing_tags = page.get("routing_tags", [])
    if routing_tags:
        return "route_agenda" in routing_tags

    # Fallback to legacy checks
    page_type = str(page.get("page_type") or "").lower()
    function_type = str(page.get("function_type") or "").lower()
    return page_type == "agenda" or function_type == "agenda"


def find_machine_readable_dirs(args: argparse.Namespace) -> list[Path]:
    if args.machine_readable:
        target = args.machine_readable.resolve()
        if not (target / "pages.jsonl").exists():
            raise FileNotFoundError(f"Missing pages.jsonl: {target}")
        return [target]

    outbox = args.parser_outbox.resolve()
    if not outbox.exists():
        return []

    candidates: list[Path] = []
    for child in outbox.iterdir():
        if not child.is_dir():
            continue
        mr = child / "machine_readable"
        if (mr / "pages.jsonl").exists():
            candidates.append(mr)

    candidates.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
    if args.latest_only:
        return candidates[:1]
    return candidates


def source_page_number(row: dict) -> int:
    page = row.get("page") or {}
    value = page.get("source_page_number")
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except Exception:
        return 10**9


def safe_token(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    token = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
    token = token.strip("._-")
    return token or fallback


def page_file_name(row: dict) -> str:
    page = row.get("page") or {}
    page_num = source_page_number(row)
    page_num_token = f"{page_num:04d}" if page_num < 10**8 else "xxxx"
    machine = safe_token(page.get("page_machine_code"), "")
    page_id = safe_token(page.get("page_id"), "unknown")
    if machine:
        return f"page_{page_num_token}__{machine}.json"
    return f"page_{page_num_token}__{page_id}.json"


def write_one_folder(machine_readable: Path, output_root: Path) -> tuple[str, Path, int, int, int]:
    run_json = read_json(machine_readable / "run.json")
    machine_code = pick_machine_code(machine_readable, run_json)

    rows = list(iter_jsonl(machine_readable / "pages.jsonl"))
    agenda_rows = [row for row in rows if is_agenda_page(row)]

    out_dir = output_root / machine_code
    if out_dir.exists():
        if out_dir.resolve().parent != output_root.resolve():
            raise RuntimeError(f"Refusing to clear path outside output root: {out_dir}")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Keep agenda pages separated: one file per agenda page.
    sorted_agenda = sorted(agenda_rows, key=source_page_number)
    written = 0
    for row in sorted_agenda:
        out_file = out_dir / page_file_name(row)
        with out_file.open("w", encoding="utf-8") as handle:
            json.dump(row, handle, indent=2)
            handle.write("\n")
        written += 1

    return machine_code, out_dir, len(rows), len(agenda_rows), written


def main() -> int:
    args = parse_args()
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    targets = find_machine_readable_dirs(args)
    if not targets:
        print("No machine_readable folders found.")
        return 0

    for target in targets:
        machine_code, out_dir, total, kept, written = write_one_folder(target, output_root)
        print(f"SORTED machine_code={machine_code} agenda_pages={kept}/{total} files={written} output={out_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
