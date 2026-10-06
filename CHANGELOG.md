# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versions are `MAJOR.MINOR.PATCH`, but no number goes above 9: when a number would pass 9 it rolls over into the
next one, so 0.0.9 is followed by 0.1.0 and 0.9.9 by 1.0.0. Versions are therefore sequential release numbers
and do not follow the semantic versioning rules for what each number means.

The project had no version history before this file was written, so versions 0.0.1 to 0.1.7 were assigned
afterwards, one per step of development, all on 2026-10-06. The script reports its version with `--version`.

## [Unreleased]

## [0.1.7] - 2026-10-06

### Changed
- The default cache directory is `~/.cache/ragdoll` (or `$XDG_CACHE_HOME/ragdoll`) instead of `~/.cache/idrac`.
  Cached files from earlier versions are not found in the new location: move the old directory
  (`mv ~/.cache/idrac ~/.cache/ragdoll`) to keep your SNMP history.
- The option to choose the cache directory is now `--cachedir`. `--cache-dir` still works as an alias.

## [0.1.6] - 2026-10-06

### Changed
- Credentials are saved in the keyring under the service name `ragdoll:<host>` instead of `idrac-temp:<host>`.
  Credentials saved by an earlier version are not found under the new name: run `--save-credentials` again, and
  remove the old entries from your keyring if you have any.

### Fixed
- `--save-credentials` was ignored when combined with `--list` (since 0.1.2), because the listing exited first.
  Credentials are now saved before the listing runs, for `--source` or, if that is not given, the source named
  after `--list`, otherwise `web`. The confirmation message names the source.

## [0.1.5] - 2026-10-06

### Added
- "Help Support Development" section in the README.

### Changed
- The script is renamed from `idrac_temp.py` to `ragdoll.py`, and the project is now called ragdoll. Update any
  cron entries or aliases that call the old name.
- New project description: "Redfish/API/GUI/DRAC/Other Log Linter - Converts iDRAC Telemetry and other information
  into more useful formats". It is used in the README and in the script's `--help`.

## [0.1.4] - 2026-10-06

### Added
- `LICENSE` file containing the Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International
  (CC BY-NC-SA 4.0) legal text. The license is referenced in the script's docstring and in the README.
- "Version" and "License" sections in the README.
- `.gitignore`, ignoring `CLAUDE.md` and `__pycache__/`.

## [0.1.3] - 2026-10-06

### Changed
- `--list` prints an aligned table with a `SOURCE METRIC SENSOR KEY` header row instead of tab-separated lines.
- Added `--version`.

## [0.1.2] - 2026-10-06

### Changed
- `--list` with no value lists every sensor from every source and metric, with the source shown for each.
- `--list` takes an optional source (`web`, `snmp`) or metric (`temperature`, `fan`, `power`) to narrow the listing.
  `--source` and `--metric` narrow it too.
- `--source` and `--metric` no longer have defaults on the command line; `web` and `temperature` apply when not listing.

### Added
- A source that cannot be queried while listing is reported on stderr and skipped instead of stopping the listing.

## [0.1.1] - 2026-10-06

### Added
- `--metric temperature|fan|power`. The `snmp` source now reads fan speeds (RPM) from the Dell fan probe table
  and power from the amperage probe table: `system-power` in watts and `ps1-current`, `ps2-current` in amps.
- Default sensor per metric: `inlet`, `fan1a` and `system-power`.
- Chart titles and axes use the unit of the metric and sensor (°C, RPM, W, A).
- Cache files are named per metric, for example `<host>_snmp_fan-fan1a.csv`.

### Fixed
- termgraph draws one block per unit when every value in a series is identical (for example a fan at a constant
  speed), producing lines thousands of characters wide. The script now draws a full-width bar and shows the value
  in the label.

## [0.1.0] - 2026-10-06

### Added
- Credentials can be saved in the operating system keyring with `--save-credentials`, which saves only after a
  successful login, and removed with `--forget-credentials`.
- Missing credentials are looked up in the order: command-line flag, environment variable (`IDRAC_USER`,
  `IDRAC_PASS`, `IDRAC_COMMUNITY`), keyring, then a password prompt.
- `keyring` added to `requirements.txt`.
- "Saving credentials" section in the README.

### Changed
- `--user`, `--pass` and `--community` are no longer required on the command line.

## [0.0.9] - 2026-10-06

### Added
- `snmp` source, using the `pysnmp` module, reading the Dell temperature probe table (`inlet`, `exhaust`,
  `cpu1`, `cpu2`). SNMP reports only the current value, so each run appends a reading to the cached history.
- `--community` for the SNMP v2c community string.
- `pysnmp` added to `requirements.txt`.
- `README.md` describing the script, its capabilities and examples, using placeholder credentials.

## [0.0.8] - 2026-10-06

### Added
- `--source` option, with `web` (the iDRAC web interface) as the only source and the default. Sources are
  registered in a table so that others can be added.

### Changed
- Cache files include the source in their name, for example `<host>_web_temperature-inlet.csv`. Files from
  earlier versions are no longer used and can be deleted.

## [0.0.7] - 2026-10-06

### Added
- `--list` shows the sensors available on the iDRAC.

### Fixed
- The iDRAC web interface ignores unknown sensor names and returns inlet data, so a chart labelled `exhaust` showed
  inlet readings. `--sensor` is now checked against the iDRAC's own sensor list and unknown names are rejected.

## [0.0.6] - 2026-10-06

### Added
- `--height` (default 24 rows, plotext only) and a `--width` default of 80 columns, so charts fit a standard terminal.
  One row is left free for the shell prompt.
- Chart titles name the host and sensor, for example "192.168.8.98 Inlet Temperature (Average / Peak)", and
  values are labelled in °C.

### Changed
- `--width` is the total width of the graph. For termgraph the label and value columns are subtracted from it.

## [0.0.5] - 2026-10-06

### Added
- `--module plotext|termgraph`, with `plotext` as the default.
- `--chart line` and `--chart scatter` (plotext only). The existing chart types are also supported by plotext.
- `requirements.txt`, with `plotext` pinned to 5.3.2 because 6.x has a different API.

## [0.0.4] - 2026-10-06

### Added
- Local cache of fetched CSV data, one file per host and sensor, under `~/.cache/idrac` (or `$XDG_CACHE_HOME/idrac`).
- `--cache-dir`, `--max-age` (default 3600 seconds) and `--refresh`.
- If a fetch fails and a cache file exists, the cache is used and a warning is printed.
- Cache files are written to a temporary file first and then renamed, so an interrupted fetch cannot corrupt them.

## [0.0.3] - 2026-10-06

### Added
- `--last` accepts a period (`hour`, `day`, `week`, `month`, `year`, and forms such as `6h`, `2d`, `3 weeks`) as
  well as a row count. A period is counted back from the newest sample in the data.

### Changed
- `--last` defaults to 10 rows. It was previously all rows.

## [0.0.2] - 2026-10-06

### Added
- `--host` option for the iDRAC address. It was previously a positional argument.
- `--chart` option with `vertical` (default), `horizontal`, `stacked` and `histogram` charts (termgraph).

## [0.0.1] - 2026-10-06

### Added
- Initial script `idrac_temp.py`: downloads the inlet temperature statistics CSV from an iDRAC and graphs it in
  the terminal with termgraph.
- `--user` and `--pass` options, so no credentials are written in the script. `--sensor`, `--secure`, `--raw`,
  `--width` and `--last` (row count).
- Certificate verification is off by default because iDRACs use self-signed certificates.
- The CSV columns (`Average,Peak,Time`) are graphed as two series.

### Fixed
- The iDRAC rejects HTTP basic authentication and returns its login page. The script now logs in through
  `/data/login`, sends the session token with the request and logs out afterwards.
