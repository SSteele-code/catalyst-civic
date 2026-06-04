# Ordinance/Resolution PRE_PARSE (Mode 1, VA)

Reads metadata-only staging artifacts from:

- `$CC_DATA_ROOT\_Sources\M1-Meetings\Ordinance_Resolution\_staging\RUN_*`

Writes normalized parse records to:

- `$CC_DATA_ROOT\_Sources\M1-Meetings\Ordinance_Resolution\_output\<M1.AG.OR...>\`

Each parse payload includes:

- `table_projection` schema (`m1.ordinance_resolution.table_projection.v1`)
- `glossary` section (`m1.ordinance_resolution.glossary.v1`) for CCO push
- lineage + `pusher_ready` metadata

This stage performs no DB writes.

## Run

```powershell
py -3.12 $CC_DATA_ROOT\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\Ordinance_Resolution\PRE_PARSE\pre_parse_ordinance_resolution_from_agenda_staging.py --dry-run
```

```powershell
py -3.12 $CC_DATA_ROOT\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\Ordinance_Resolution\PRE_PARSE\pre_parse_ordinance_resolution_from_agenda_staging.py --force
```

