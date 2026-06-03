#!/usr/bin/env python
"""
Department Reports PARSE entrypoint.

Compatibility wrapper that forwards execution to the canonical parser implementation
in PRE_PARSE.
"""
from __future__ import annotations

from pathlib import Path
import runpy


def main() -> int:
    target = (
        Path(__file__).resolve().parents[1]
        / "PRE_PARSE"
        / "pre_parse_department_reports_from_agenda_staging.py"
    )
    runpy.run_path(str(target), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

