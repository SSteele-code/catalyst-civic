import os
import re
import sys
import psycopg2
from pathlib import Path

# Database Configuration
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_DB = os.getenv("PG_DB", "catalyst_civic")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASS = os.getenv("PG_PASS", "postgres")

def parse_schema_suggestions(file_path):
    """
    Parses the [SCHEMA_SUGGESTION_BOX] section from a scaffold MD.
    Extracts the required_fields map.
    """
    content = Path(file_path).read_text(encoding="utf-8")
    suggestion_box = re.search(r"\[SCHEMA_SUGGESTION_BOX\](.*?)\[/SCHEMA_SUGGESTION_BOX\]", content, re.DOTALL)
    
    if not suggestion_box:
        return None

    box_text = suggestion_box.group(1).strip()
    required_fields = {}
    
    # Simple parsing for 'required_fields:' section
    # meeting: [field1, field2]
    rf_match = re.search(r"required_fields:\s*(.*?)(?=\n\S|$)", box_text, re.DOTALL)
    if rf_match:
        rf_text = rf_match.group(1).strip()
        for line in rf_text.splitlines():
            if ":" in line:
                key, fields_raw = line.split(":", 1)
                fields_raw = fields_raw.strip().strip("[]")
                fields = [f.strip() for f in fields_raw.split(",") if f.strip()]
                required_fields[key.strip()] = fields
                
    return required_fields

def sculpt_schema(required_fields):
    """
    Updates the database schema by adding missing columns.
    Maps meeting -> m1_agenda.meetings
    Maps section/item -> m1_agenda.items
    """
    # Mapping suggested entity types to actual tables
    table_map = {
        "meeting": "m1_agenda.meetings",
        "section": "m1_agenda.items",
        "item": "m1_agenda.items"
    }

    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, database=PG_DB, user=PG_USER, password=PG_PASS
        )
        cur = conn.cursor()

        for entity, fields in required_fields.items():
            table_name = table_map.get(entity)
            if not table_name:
                print(f"Skipping unknown entity type: {entity}")
                continue

            # Fetch existing columns for this table
            schema_name, raw_table_name = table_name.split(".")
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_schema = %s AND table_name = %s;
            """, (schema_name, raw_table_name))
            
            existing_columns = {row[0] for row in cur.fetchall()}

            for field in fields:
                # Sanitize field name (Postgres columns should be lowercase and simple)
                field_sanitized = field.lower().strip()
                if field_sanitized not in existing_columns:
                    print(f"Sculpting: Adding column '{field_sanitized}' to {table_name}")
                    # Flexible Default: Every new column starts as TEXT unless explicitly specified otherwise.
                    # This allows the system to "grow" without data type errors.
                    try:
                        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {field_sanitized} TEXT;")
                        print(f"Success: Added '{field_sanitized}'")
                    except Exception as col_err:
                        print(f"Error adding column {field_sanitized}: {col_err}")
                        conn.rollback() # Rollback the single error
                        continue
            
            conn.commit()

        cur.close()
        conn.close()
        print("Schema Sculpting Complete.")

    except Exception as e:
        print(f"Database connection error: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python schema_sculptor.py <path_to_scaffold.md>")
    else:
        scaffold_path = sys.argv[1]
        print(f"Reading suggestions from: {scaffold_path}")
        suggestions = parse_schema_suggestions(scaffold_path)
        if suggestions:
            sculpt_schema(suggestions)
        else:
            print("No Schema Suggestion Box found.")
