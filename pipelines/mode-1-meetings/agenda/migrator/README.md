# Agenda Migrator (M1)

The **Agenda Migrator** is an industrial-grade ingestion script designed to move scaffolded agenda data from Markdown files into the `m1_agenda` Postgres schema.

## Purpose
- **Idempotent Ingestion**: Uses `ON CONFLICT` to ensure that re-running the script updates existing records rather than duplicating them.
- **Dynamic Column Mapping**: Automatically detects new columns added by the *Schema Sculptor* and attempts to populate them from the Markdown metadata.
- **Hierarchical Support**: Preserves the ordinal sequence and labels of agenda sections.

## Usage
Run the script using the local Python 3.12 environment, passing the path to a scaffolded `.md` file:

```powershell
python migrate_agenda_scaffold.py "C:\path\to\scaffold.md"
```

## Logic
1. **Parsing**: Extracts metadata from the `[SCHEMA_SUGGESTION_BOX]` and items from the `## Scaffolded Agenda` section.
2. **Meeting Upsert**: Checks the `m1_agenda.meetings` table and populates standard fields + any matching metadata fields.
3. **Item Upsert**: Maps sections/items to `m1_agenda.items`, generating unique IDs based on the meeting's machine code and the item's label/ordinal.

## Invariants
- **Machine Code Authority**: The `artifact_machine_code` is the primary key for meetings.
- **Richlands Default**: Currently defaults the jurisdiction to 'Richlands' during insertion.
