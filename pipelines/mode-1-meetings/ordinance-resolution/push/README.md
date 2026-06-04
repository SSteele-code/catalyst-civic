# Ordinance/Resolution PUSH (Mode 1, VA)

Loads parse output metadata records into:

- `m1_ordinance_resolution.documents`

Input: parse output under `$CC_DATA_ROOT\_Sources\M1-Meetings\Ordinance_Resolution\_output\`

Defaults to latest parse run scope (from `ordinance_resolution_preparse_state.json`).

## Run

```powershell
python pipelines/mode-1-meetings/ordinance-resolution/push/push_ordinance_resolution_output_to_db.py --dry-run --force
```

```powershell
python pipelines/mode-1-meetings/ordinance-resolution/push/push_ordinance_resolution_output_to_db.py --force
```

Use `--all-output` only when intentionally loading full historical output.
