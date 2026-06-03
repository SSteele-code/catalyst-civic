# Ordinance/Resolution Pull (Mode 1, VA)

Pulls ordinance/resolution **metadata-only** records from:

- `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Agendas\_output`

Writes to:

- `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Ordinance_Resolution\_sources`
- `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Ordinance_Resolution\_staging\RUN_*`

This stage extracts only:

- document type (`ORDINANCE` / `RESOLUTION`)
- document number/id (when detectable)
- title/header metadata
- meeting linkage metadata (`source_pdf_code`, anchor meeting date, packet code)

No full ordinance/resolution body extraction is performed in this stage.

## Run

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\Ordinance_Resolution\PULL\pull_ordinance_resolution_from_agenda_output.py --dry-run
```

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\Ordinance_Resolution\PULL\pull_ordinance_resolution_from_agenda_output.py --force
```

