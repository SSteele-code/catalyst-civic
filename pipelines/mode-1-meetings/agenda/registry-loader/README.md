# Registry Loader (M1)

The **Registry Loader** is a player-centric ingestion script that extracts key entities (People, Organizations, etc.) from agenda scaffolds and populates the `cco` industrial registry.

## Purpose
- **Entity Identification**: Automatically scouts for players mentioned in the agenda text (e.g., in "Show Cause" items).
- **Durable Identity**: Maps variations of a name to a stable `registry_id` using the `cco.identities` table.
- **Fact Tracking**: Stores specific attributes (like a person's role in a meeting) as temporal observations in `cco.observations`.

## Usage
Run the script against a scaffolded Markdown file:

```powershell
python registry_loader.py "C:\path\to\scaffold.md"
```

## Data Model (CCO)
- **`cco.registry`**: The master list of unique players.
- **`cco.identities`**: The alias map (handles 'Councilwoman Mollo' vs 'Laura Mollo').
- **`cco.observations`**: The factual record. Stores what was true about a player on a specific date (e.g., 'James Clinton Holmes' was a 'PROPERTY_OWNER' on 2014-08-12).

## Philosophy
- **Evidence-First**: Every entry in the observations table includes a snippet of text (`evidence`) and a `source_id` link to the primary record.
- **Idempotent Identifiers**: Registry IDs are generated deterministically based on category and canonical name.
