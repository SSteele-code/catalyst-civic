# Ordinance/Resolution Pull (Mode 1, VA)

Pulls ordinance/resolution **metadata-only** records from agenda parser output.

Input: `$CC_DATA_ROOT\_Sources\M1-Meetings\Agendas\_output`

Output:
- `$CC_DATA_ROOT\_Sources\M1-Meetings\Ordinance_Resolution\_sources`
- `$CC_DATA_ROOT\_Sources\M1-Meetings\Ordinance_Resolution\_staging\RUN_*`

Extracts only document type (`ORDINANCE` / `RESOLUTION`), document number/id, title/header metadata, and meeting linkage metadata. No full body extraction at this stage.

## Run

```powershell
python pipelines/mode-1-meetings/ordinance-resolution/pull/pull_ordinance_resolution_from_agenda_output.py --dry-run
```

```powershell
python pipelines/mode-1-meetings/ordinance-resolution/pull/pull_ordinance_resolution_from_agenda_output.py --force
```
