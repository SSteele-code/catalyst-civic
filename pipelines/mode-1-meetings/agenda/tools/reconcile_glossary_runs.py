import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from Registry_Loader.registry_loader import (
    PG_DB,
    PG_HOST,
    PG_PASS,
    PG_PORT,
    PG_USER,
    extract_suggestion_meta,
    generate_id,
    parse_entities,
)

OUTPUT_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Agendas" / "_output"
MODE_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Modes" / "M1" / "Agenda"
REPORTS_DIR = BASE_DIR / "tools" / "reports"


def connect_db():
    return psycopg2.connect(
        host=os.getenv("PG_HOST", PG_HOST),
        port=os.getenv("PG_PORT", PG_PORT),
        database=os.getenv("PG_DB", PG_DB),
        user=os.getenv("PG_USER", PG_USER),
        password=os.getenv("PG_PASS", PG_PASS),
    )


def list_pulses(limit: int | None = None) -> list[str]:
    dirs = [p for p in OUTPUT_ROOT.iterdir() if p.is_dir() and p.name.startswith("M1.AG.")]
    dirs.sort(key=lambda p: p.stat().st_mtime)
    pulses = [d.name for d in dirs]
    if limit is not None and limit > 0:
        pulses = pulses[-limit:]
    return pulses


def fetch_existing_entities(cur, source_id: str) -> list[tuple[int, str, str, str]]:
    cur.execute(
        """
        SELECT o.fact_id, r.registry_id, r.category, r.canonical_name
        FROM cco.observations o
        JOIN cco.registry r ON r.registry_id = o.registry_id
        WHERE o.source_id = %s
        ORDER BY o.fact_id
        """,
        (source_id,),
    )
    return cur.fetchall()


def observation_exists(cur, registry_id: str, source_id: str, fact_key: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM cco.observations
        WHERE registry_id = %s AND source_id = %s AND fact_key = %s
        LIMIT 1
        """,
        (registry_id, source_id, fact_key),
    )
    return cur.fetchone() is not None


def upsert_entity(cur, source_id: str, meeting_date: str | None, ent: dict[str, Any]) -> None:
    registry_id = generate_id(str(ent["category"]), str(ent["canonical_name"]))
    cur.execute(
        """
        INSERT INTO cco.registry (registry_id, category, canonical_name)
        VALUES (%s, %s, %s)
        ON CONFLICT (registry_id) DO UPDATE SET updated_at = CURRENT_TIMESTAMP
        """,
        (registry_id, ent["category"], ent["canonical_name"]),
    )
    cur.execute(
        """
        INSERT INTO cco.identities (registry_id, alias_name, source_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (registry_id, alias_name) DO NOTHING
        """,
        (registry_id, ent["canonical_name"], source_id),
    )
    if not observation_exists(cur, registry_id, source_id, str(ent["fact_key"])):
        cur.execute(
            """
            INSERT INTO cco.observations (registry_id, fact_key, fact_value, source_id, evidence, effective_date)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                registry_id,
                ent["fact_key"],
                json.dumps(ent["fact_value"]),
                source_id,
                ent["evidence"],
                meeting_date,
            ),
        )


def delete_stale_observations(cur, fact_ids: list[int]) -> None:
    if not fact_ids:
        return
    cur.execute("DELETE FROM cco.observations WHERE fact_id = ANY(%s)", (fact_ids,))


def delete_orphan_identities_and_registry(cur) -> None:
    cur.execute(
        """
        DELETE FROM cco.identities i
        WHERE NOT EXISTS (
            SELECT 1 FROM cco.observations o
            WHERE o.registry_id = i.registry_id AND o.source_id = i.source_id
        )
        """
    )
    cur.execute(
        """
        DELETE FROM cco.registry r
        WHERE NOT EXISTS (
            SELECT 1 FROM cco.observations o WHERE o.registry_id = r.registry_id
        )
        """
    )


def run(limit: int | None, prune_stale: bool) -> dict[str, Any]:
    pulses = list_pulses(limit)
    conn = connect_db()
    cur = conn.cursor()
    report: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pulses_scanned": len(pulses),
        "pulses": [],
        "totals": {
            "added_entities": 0,
            "stale_deleted": 0,
            "skipped_missing_scf1": 0,
            "skipped_parse_error": 0,
        },
    }

    for pulse in pulses:
        mode_dir = MODE_ROOT / pulse
        scf1 = mode_dir / f"{pulse}.SCF1.md"
        pulse_row = {
            "pulse": pulse,
            "source_id": None,
            "existing_count": 0,
            "recomputed_count": 0,
            "added_count": 0,
            "stale_count": 0,
            "notes": [],
        }

        if not scf1.exists():
            pulse_row["notes"].append("missing_scf1")
            report["totals"]["skipped_missing_scf1"] += 1
            report["pulses"].append(pulse_row)
            continue

        try:
            source_id, meeting_date, new_entities = parse_entities(str(scf1))
        except Exception as exc:
            pulse_row["notes"].append(f"parse_error:{exc}")
            report["totals"]["skipped_parse_error"] += 1
            report["pulses"].append(pulse_row)
            continue

        pulse_row["source_id"] = source_id
        pulse_row["recomputed_count"] = len(new_entities)

        existing = fetch_existing_entities(cur, source_id)
        pulse_row["existing_count"] = len(existing)
        existing_map = {(cat.upper(), name.upper()): (fid, rid, cat, name) for fid, rid, cat, name in existing}
        new_map = {(str(e["category"]).upper(), str(e["canonical_name"]).upper()): e for e in new_entities}

        to_add_keys = [k for k in new_map.keys() if k not in existing_map]
        stale_fact_ids: list[int] = []
        for key, val in existing_map.items():
            if key not in new_map:
                stale_fact_ids.append(val[0])

        for key in to_add_keys:
            upsert_entity(cur, source_id, meeting_date, new_map[key])
        pulse_row["added_count"] = len(to_add_keys)
        report["totals"]["added_entities"] += len(to_add_keys)

        if prune_stale and stale_fact_ids:
            delete_stale_observations(cur, stale_fact_ids)
            pulse_row["stale_count"] = len(stale_fact_ids)
            report["totals"]["stale_deleted"] += len(stale_fact_ids)

        report["pulses"].append(pulse_row)

    if prune_stale:
        delete_orphan_identities_and_registry(cur)
    conn.commit()
    cur.close()
    conn.close()
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Run-by-run glossary reconciliation against agenda source scope.")
    ap.add_argument("--limit", type=int, default=None, help="Optional number of most recent pulses to reconcile.")
    ap.add_argument(
        "--no-prune-stale",
        action="store_true",
        help="Append missing entries only; do not delete stale observations.",
    )
    args = ap.parse_args()

    report = run(limit=args.limit, prune_stale=not args.no_prune_stale)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"glossary_reconcile_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Pulses scanned: {report['pulses_scanned']}")
    print(f"Added entities: {report['totals']['added_entities']}")
    print(f"Stale deleted: {report['totals']['stale_deleted']}")
    print(f"Report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
