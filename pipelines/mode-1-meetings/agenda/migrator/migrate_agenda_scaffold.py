import os
import re
import sys
import json
import psycopg2
from pathlib import Path
from datetime import datetime

# Database Configuration
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_DB = os.getenv("PG_DB", "catalyst_civic")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASS = os.getenv("PG_PASS", "postgres")
OUTPUT_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Agendas" / "_output"


def pulse_id_from_meeting_id(meeting_id):
    return re.sub(r"\.SCF\d+$", "", str(meeting_id or ""), flags=re.IGNORECASE)

def _collapse_ws(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def load_page_text_map(scaffold_md_path):
    """
    Loads per-page OCR text from sibling page_####.json files.
    Returns {page_number(int): text(str)}.
    """
    page_text = {}
    mode_dir = Path(scaffold_md_path).resolve().parent
    for page_json in sorted(mode_dir.glob("page_*.json")):
        try:
            payload = json.loads(page_json.read_text(encoding="utf-8"))
            pnum = payload.get("page", {}).get("source_page_number")
            text = payload.get("text", {}).get("content", "")
            if isinstance(pnum, int):
                page_text[pnum] = str(text or "")
        except Exception:
            continue
    return page_text


def build_source_text_from_page_map(page_text_map):
    if not page_text_map:
        return ""
    chunks = []
    for page_num in sorted(page_text_map.keys()):
        chunks.append(f"--- PAGE {page_num:04d} ---\n{page_text_map[page_num]}\n")
    return "\n".join(chunks)


def load_pulse_source_text(meeting_id):
    """
    Loads the consolidated OCR text emitted to _output/<meeting_id>/<meeting_id>.txt
    so DB can retain source substance for strict audits.
    """
    meeting_id = str(meeting_id or "")
    pulse_id = pulse_id_from_meeting_id(meeting_id)

    pulse_dir = OUTPUT_ROOT / pulse_id
    txt_files = []
    if pulse_dir.exists():
        txt_files = list(pulse_dir.glob("*.txt"))
    if not txt_files:
        fallback = [p for p in OUTPUT_ROOT.glob(f"{pulse_id}*.txt") if p.is_file()]
        txt_files = fallback
    if not txt_files:
        return ""
    try:
        return txt_files[0].read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def load_pulse_factsheet_metadata(meeting_id):
    """
    Pulls source file identity fields from _output factsheet so meeting metadata
    can always be reconciled to vaulted source PDFs.
    """
    pulse_id = pulse_id_from_meeting_id(meeting_id)
    pulse_hash_prefix = pulse_id.split(".")[-1].lower() if "." in pulse_id else ""

    def read_facts(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    # Preferred location: finalized pulse folder.
    pulse_dir = OUTPUT_ROOT / pulse_id
    if pulse_dir.exists():
        factsheets = sorted(pulse_dir.glob("*.factsheet.json"))
        for fs in factsheets:
            facts = read_facts(fs)
            if facts:
                break
        else:
            facts = None
    else:
        facts = None

    # Fallback location: active RUN_* folders before vault/janitor finalization.
    if not facts:
        candidates = []
        for d in OUTPUT_ROOT.iterdir():
            if d.is_dir() and d.name.startswith("RUN_"):
                candidates.extend(d.glob("*.factsheet.json"))
        for fs in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
            payload = read_facts(fs)
            if not payload:
                continue
            h = str(payload.get("source_pdf_hash") or "").lower().strip()
            if pulse_hash_prefix and h.startswith(pulse_hash_prefix):
                facts = payload
                break

    if not facts:
        return {"source_pulse_id": pulse_id}

    out = {}
    for key in ("source_pdf_original_name", "source_pdf_internal_name", "source_pdf_hash"):
        val = str(facts.get(key) or "").strip()
        if val:
            out[key] = val
    if isinstance(facts.get("page_count"), int):
        out["page_count"] = facts["page_count"]
    out["source_pulse_id"] = pulse_id
    return out


def parse_iso_date_from_source_original_name(source_pdf_original_name):
    name = str(source_pdf_original_name or "").strip()
    if not name:
        return None
    match = re.search(r"M1\.AG\.\d{6}\.(\d{8})\.\d{8}\.pdf$", name, re.IGNORECASE)
    if not match:
        return None
    ymd = match.group(1)
    try:
        dt = datetime.strptime(ymd, "%Y%m%d").date()
        return dt.isoformat()
    except Exception:
        return None


def build_item_substance(item, page_text_map):
    """
    Provides semantic payload for DB fields from OCR text.
    Uses full source page text for the item's page to maximize retained substance.
    """
    try:
        page_num = int(item.get("source_page", 0) or 0)
    except Exception:
        page_num = 0
    page_text = _collapse_ws(page_text_map.get(page_num, ""))
    if not page_text:
        return ""
    return page_text

def parse_markdown_scaffold(file_path):
    """
    Parses the Catalyst Civic Agenda Scaffold Markdown.
    Extracts high-level metadata and structured agenda items.
    """
    content = Path(file_path).read_text(encoding="utf-8")
    
    # 1. Extract Suggestion Box Metadata
    meta = {}
    suggestion_box = re.search(r"\[SCHEMA_SUGGESTION_BOX\](.*?)\[/SCHEMA_SUGGESTION_BOX\]", content, re.DOTALL)
    if suggestion_box:
        box_text = suggestion_box.group(1).strip()
        for line in box_text.splitlines():
            if ":" in line and not line.strip().startswith("-"):
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()

    # 2. Extract Top-level info from headers/bullets if not in box
    meeting_type = meta.get("meeting_type")
    if not meeting_type:
        m = re.search(r"- Meeting Type:\s*(.*)", content)
        meeting_type = m.group(1).strip() if m else "UNKNOWN"
    
    meeting_date_str = meta.get("meeting_date")
    if not meeting_date_str:
        m = re.search(r"- Meeting Date:\s*(.*)", content)
        meeting_date_str = m.group(1).strip() if m else None

    # Try to parse date into ISO format for Postgres
    meeting_date = None
    if meeting_date_str:
        try:
            # Handle formats like "August 12, 2014"
            meeting_date = datetime.strptime(meeting_date_str, "%B %d, %Y").date().isoformat()
        except ValueError:
            meeting_date = None

    meeting_time = meta.get("meeting_time")
    if not meeting_time:
        m = re.search(r"- Meeting Time:\s*(.*)", content)
        meeting_time = m.group(1).strip() if m else "UNKNOWN"

    location = meta.get("location", "UNKNOWN")
    if location == "UNKNOWN":
        m = re.search(r"- Location:\s*(.*)", content)
        if m: location = m.group(1).strip()

    meeting_id = meta.get("artifact_machine_code", meta.get("source_machine_code", "UNKNOWN"))
    source_id = meta.get("source_machine_code", "UNKNOWN")

    # 3. Extract Items from "## Scaffolded Agenda" section
    items = []
    # Use re.DOTALL and only stop at the next top-level header (e.g., ## Schema Suggestion)
    scaffold_match = re.search(r"## Scaffolded Agenda\s*[\r\n]+(.*?)(?=[\r\n]+#{1,2}\s+|$)", content, re.DOTALL)
    if scaffold_match:
        scaffold_text = scaffold_match.group(1).strip()
        
        # Split by sections (###)
        sections = re.split(r"[\r\n]+###\s+", "\n" + scaffold_text)
        
        global_ordinal = 1
        for sec in sections:
            sec = sec.strip()
            if not sec: continue
            
            # First line is the section header: "1. [I] Call to Order"
            lines = sec.splitlines()
            header = lines[0]
            
            # Parse header: "1. [I] Title"
            # Pattern: ordinal. [label] Title
            h_match = re.match(r"(\d+)\.\s+\[(.*?)\]\s+(.*)", header)
            if h_match:
                s_ord = h_match.group(1)
                s_label = h_match.group(2)
                s_title = h_match.group(3)
                
                # Add the Section
                items.append({
                    "ordinal": global_ordinal,
                    "section_ordinal": s_ord,
                    "label": s_label,
                    "title": s_title,
                    "item_type": "SECTION",
                    "source_page": 1 
                })
                global_ordinal += 1
                
                # Parse sub-items in the rest of the lines
                current_page = items[-1]["source_page"]
                for sub_line in lines[1:]:
                    sub_line = sub_line.strip()
                    if not sub_line or sub_line.startswith("Items: none"): continue
                    
                    # Page indicator: "- Source Page: 1"
                    p_match = re.search(r"Source Page:\s*(\d+)", sub_line)
                    if p_match:
                        current_page = int(p_match.group(1))
                        items[-1]["source_page"] = current_page
                        continue
                        
                    # Item line: "- 1. (a) Approval of Agenda [p1]"
                    i_match = re.match(r"-\s*(\d+)\.\s+\((.*?)\)\s+(.*?)(?:\s+\[p\d+\])?$", sub_line)
                    if i_match:
                        items.append({
                            "ordinal": global_ordinal,
                            "section_ordinal": s_ord,
                            "item_ordinal": i_match.group(1),
                            "label": i_match.group(2),
                            "title": i_match.group(3),
                            "item_type": "ITEM",
                            "source_page": current_page
                        })
                        global_ordinal += 1

    return {
        "meeting": {
            "meeting_id": meeting_id,
            "source_id": source_id,
            "meeting_type": meeting_type,
            "meeting_date": meeting_date,
            "meeting_time": meeting_time,
            "location": location,
            "metadata": meta 
        },
        "items": items
    }

def get_table_columns(cur, table_name):
    schema, table = table_name.split(".")
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s", (schema, table))
    return {row[0] for row in cur.fetchall()}

def migrate(file_path):
    print(f"Processing: {file_path}")
    data = parse_markdown_scaffold(file_path)
    page_text_map = load_page_text_map(file_path)
    source_text_full = build_source_text_from_page_map(page_text_map)
    if not source_text_full:
        source_text_full = load_pulse_source_text(data["meeting"]["meeting_id"])
    factsheet_meta = load_pulse_factsheet_metadata(data["meeting"]["meeting_id"])
    if "page_count" not in factsheet_meta and page_text_map:
        factsheet_meta["page_count"] = len(page_text_map)
    if not data["meeting"].get("meeting_date"):
        fallback_iso = parse_iso_date_from_source_original_name(factsheet_meta.get("source_pdf_original_name"))
        if fallback_iso:
            data["meeting"]["meeting_date"] = fallback_iso
            factsheet_meta["meeting_date_fallback"] = "source_pdf_original_name"
    
    if not data["meeting"]["meeting_id"]:
        print("Error: No meeting_id found in scaffold.")
        return

    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, database=PG_DB, user=PG_USER, password=PG_PASS
        )
        cur = conn.cursor()

        # 1. Upsert Meeting with dynamic column support
        m = data["meeting"]
        meeting_table = "m1_agenda.meetings"
        cols = get_table_columns(cur, meeting_table)
        
        # Prepare dynamic insert
        base_fields = {
            "meeting_id": m["meeting_id"],
            "source_id": m["source_id"],
            "jurisdiction": "Richlands",
            "meeting_type": m["meeting_type"],
            "meeting_date": m["meeting_date"],
            "meeting_time": m["meeting_time"],
            "location": m["location"],
            "metadata": json.dumps({
                **m["metadata"],
                **factsheet_meta,
                "source_text_full": source_text_full,
                "source_text_chars": len(source_text_full),
            })
        }
        
        # Add any metadata fields that have matching columns
        for k, v in m["metadata"].items():
            if k.lower() in cols and k.lower() not in base_fields:
                base_fields[k.lower()] = v

        field_names = list(base_fields.keys())
        field_placeholders = ["%s"] * len(field_names)
        update_set = ", ".join([f"{f} = EXCLUDED.{f}" for f in field_names if f != "meeting_id"])

        query = f"""
            INSERT INTO {meeting_table} ({", ".join(field_names)})
            VALUES ({", ".join(field_placeholders)})
            ON CONFLICT (meeting_id) DO UPDATE SET
                {update_set},
                updated_at = CURRENT_TIMESTAMP;
        """
        cur.execute(query, list(base_fields.values()))

        # 2. Upsert Items
        items_table = "m1_agenda.items"
        item_cols = get_table_columns(cur, items_table)
        
        for item in data["items"]:
            # Better unique ID for hierarchical items
            suffix = f"S{item.get('section_ordinal', 0)}"
            if item["item_type"] == "ITEM":
                suffix += f".I{item.get('item_ordinal', item['ordinal'])}"
            
            item_id = f"{m['meeting_id']}.{suffix}"
            
            substance = build_item_substance(item, page_text_map)
            item_fields = {
                "item_id": item_id,
                "meeting_id": m["meeting_id"],
                "ordinal": item["ordinal"],
                "label": item["label"],
                "title": item["title"],
                "item_type": item["item_type"],
                "source_page": item["source_page"],
                "metadata": json.dumps({
                    "source_page_number": item["source_page"],
                    "substance_strategy": "full_page_text",
                    "substance_chars": len(substance),
                }),
            }
            
            # Map scaffold item fields to newly sculpted columns
            mapping = {
                "item_label": item["label"],
                "item_ordinal": item.get("item_ordinal") or str(item["ordinal"]),
                "section_ordinal": str(item.get("section_ordinal", "0")),
                "section_title": item["title"] if item["item_type"] == "SECTION" else None,
                "artifact_machine_code": m["meeting_id"],
                "source_page_number": str(item["source_page"])
            }
            for col, val in mapping.items():
                if col in item_cols:
                    item_fields[col] = val

            if "content" in item_cols:
                item_fields["content"] = substance
            if "item_text" in item_cols:
                item_fields["item_text"] = substance

            i_field_names = list(item_fields.keys())
            i_placeholders = ["%s"] * len(i_field_names)
            i_update_set = ", ".join([f"{f} = EXCLUDED.{f}" for f in i_field_names if f != "item_id"])

            i_query = f"""
                INSERT INTO {items_table} ({", ".join(i_field_names)})
                VALUES ({", ".join(i_placeholders)})
                ON CONFLICT (item_id) DO UPDATE SET
                    {i_update_set};
            """
            cur.execute(i_query, list(item_fields.values()))

        conn.commit()
        print(f"Successfully migrated meeting {m['meeting_id']} and {len(data['items'])} items.")
        cur.close()
        conn.close()

    except Exception as e:
        print(f"Database error: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python migrate_agenda_scaffold.py <path_to_scaffold.md>")
    else:
        migrate(sys.argv[1])
