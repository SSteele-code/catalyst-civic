# Schema Sculptor (M1)

The **Schema Sculptor** is a database evolution tool that allows the Catalyst Civic schema to grow dynamically based on the requirements detected during agenda scaffolding.

## Purpose
- **Schema Growth**: Reads suggestions from `[SCHEMA_SUGGESTION_BOX]` in scaffold Markdown files.
- **Dynamic Evolution**: Automatically adds missing columns to the `m1_agenda` namespace.
- **Flexibility**: Ensures that new metadata or data fields can be supported without manual `ALTER TABLE` operations.

## Usage
Run the script to analyze a scaffold and apply schema updates:

```powershell
python schema_sculptor.py "C:\path\to\scaffold.md"
```

## Logic
1. **Extraction**: Parses the `required_fields` section from the suggestion box.
2. **Analysis**: Compares suggested fields against existing columns in `m1_agenda.meetings` and `m1_agenda.items`.
3. **Execution**: If a column is missing, it is added to the database using `TEXT` as the default, safe data type.

## Philosophy
- **Additive Only**: The sculptor only adds columns; it never deletes or modifies existing data.
- **No Hallucination**: Only fields explicitly listed as *required* in the scaffold suggestions are considered for sculpting.
- **Postgres Authority**: Leverages `information_schema` to accurately detect existing table structures.
