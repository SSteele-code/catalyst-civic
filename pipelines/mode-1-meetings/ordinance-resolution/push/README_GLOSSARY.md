# Ordinance/Resolution PUSH Part 1 (Glossary -> CCO)

Loads parse glossary entities into:

- `cco.registry`
- `cco.identities`
- `cco.observations`

Input: parse output under `$CC_DATA_ROOT\_Sources\M1-Meetings\Ordinance_Resolution\_output\`

Defaults to latest parse run scope (from `ordinance_resolution_preparse_state.json`).

## Run

```powershell
python pipelines/mode-1-meetings/ordinance-resolution/push/push_ordinance_resolution_glossary_to_authority.py --dry-run --force
```

```powershell
python pipelines/mode-1-meetings/ordinance-resolution/push/push_ordinance_resolution_glossary_to_authority.py --force
```

Use `--all-output` only for intentional historical backfills.
