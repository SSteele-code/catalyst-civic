# Ordinance/Resolution PUSH Part 1 (Glossary -> CCO)

Loads parse glossary entities into:

- `cco.registry`
- `cco.identities`
- `cco.observations`

Input:

- `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Ordinance_Resolution\_output\<M1.AG.OR...>\*.parse.json`

Defaults to latest parse run scope (from `ordinance_resolution_preparse_state.json`).

## Run

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\Ordinance_Resolution\PUSH\push_ordinance_resolution_glossary_to_authority.py --dry-run --force
```

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\Ordinance_Resolution\PUSH\push_ordinance_resolution_glossary_to_authority.py --force
```

Use `--all-output` only for intentional historical backfills.

