# Ordinance/Resolution PUSH (Mode 1, VA)

Loads parse output metadata records into:

- `m1_ordinance_resolution.documents`

Input:

- `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Ordinance_Resolution\_output\<M1.AG.OR...>\*.parse.json`

Defaults to latest parse run scope (from `ordinance_resolution_preparse_state.json`).

## Run

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\Ordinance_Resolution\PUSH\push_ordinance_resolution_output_to_db.py --dry-run --force
```

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\Ordinance_Resolution\PUSH\push_ordinance_resolution_output_to_db.py --force
```

Use `--all-output` only when intentionally loading full historical output.

