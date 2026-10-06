# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versions are `MAJOR.MINOR.PATCH`, but no number goes above 9: when a number would pass 9 it rolls over into the
next one, so 0.0.9 is followed by 0.1.0 and 0.9.9 by 1.0.0. Versions are therefore sequential release numbers
and do not follow the semantic versioning rules for what each number means.

The project had no version history before this file was written, so versions 0.0.1 to 0.4.0 were assigned
afterwards, one per step of development, all on 2026-10-06. The script reports its version with `--version`.

## [Unreleased]

## [0.4.0] - 2026-10-06

### Added
- `--detail DETAIL` returns just one attribute of each inventory item, as its value only: for example
  `--get inventory --name bios --detail version` prints `2.19.0`. The attribute name is not case-sensitive.
- In plain text the values are bare, one per line; `--output table` shows `CATEGORY`, `NAME` and the detail under its
  own name; `csv`, `xlsx` and `xls` give the `category,name,attribute,value` rows for just that attribute.
- Items without the detail are skipped. If none has it, the error lists the details the items do have.
- `--detail` is rejected without `--list inventory` or `--get inventory`, and together with `--field`.

## [0.3.9] - 2026-10-06

### Added
- `--field FIELD` returns just one field of what `--list` or `--get` prints, for example
  `--get inventory --category system --field details`, `--get --field value` (a bare `16`) or
  `--list fan --field sensor`. It is not case-sensitive.
- The fields are the columns of the chosen `--output`: for sensors `source`, `metric`, `sensor`, `unit`, `value`,
  `limits`, `key` in `text` and `table`, and the four limit columns in place of `limits` in `csv`, `xlsx` and `xls`;
  for the inventory `category`, `name`, `details` in `text` and `table`, and `category`, `name`, `attribute`,
  `value` in `csv`, `xlsx` and `xls`. A wrong name is rejected with the fields that exist.
- In plain text one field is printed as bare values, one per line with no heading, which suits scripts; the other
  formats keep the heading.
- `--field` is rejected without `--list` or `--get`.

## [0.3.8] - 2026-10-06

### Added
- `--category CATEGORY` limits `--list inventory` and `--get inventory` to one category (`system`, `idrac`, `bios`,
  `firmware`, `cpu`, `memory`, `nic`, `pci`, `controller`, `disk`, `virtual-disk`, `raid-battery`). Only that
  category's tables are read, so it is faster than the whole inventory (2 s against 5 s on the test server).
- `--name NAME` limits it to the items with that name, for example `--name DIMM.Socket.A1`. It is not
  case-sensitive and `*` and `?` are wildcards. With `--category` it narrows that category; without, it looks in
  every category.
- A name that matches nothing is rejected with the names that exist, and an unknown category with the valid ones.
  Both flags are rejected without `--list inventory` or `--get inventory`.
- Inventory spreadsheets are named after the filters, for example `<host>_inventory_memory_DIMM.Socket.A1.xlsx`.

## [0.3.7] - 2026-10-06

### Added
- `--get inventory` prints the hardware inventory, exactly the same as `--list inventory`, in every `--output`
  format. Plain `--get` still reads one sensor.

### Changed
- `--get` now takes an optional value. Any word other than `inventory` is rejected with a message pointing to
  `--metric` and `--sensor`.
- The inventory is also rejected when `--sensor` is given (it already rejected `--metric`), and its error messages no
  longer say "--list".

## [0.3.6] - 2026-10-06

### Added
- `--list inventory` lists the hardware over SNMP: system, iDRAC, BIOS and firmware, CPUs, memory modules, network
  ports, PCI devices, storage controllers, disks, virtual disks and the RAID battery (66 items, 314 attributes on the
  test server). Sizes are in GiB, speeds in MHz, MAC addresses are shown as `24:6E:...`, and status and state values
  are shown as words (`ok`, `online`, `RAID 5`).
- It works with `--output text|table|csv|xlsx|xls`. `text` and `table` give one row per item; `csv`, `xlsx` and `xls`
  give one row per attribute (`category,name,attribute,value`). A spreadsheet is named `<host>_inventory.xlsx` unless
  `--file` is given.
- `--list inventory` is rejected with `--source web` and with `--metric`, and `--save-credentials` with it saves for snmp.

### Changed
- The text, table, CSV and spreadsheet writers for `--list` now share common code that the inventory listing also
  uses. The sensor listings are unchanged.

### Changed
- The README is restructured. Features are grouped by task (sources and metrics, listing, getting a value, charts,
  other outputs, time zones, storage, credentials, running regularly) instead of in the order they were added, and
  the duplicated examples are merged into one "Example output" section.
- New in the README: a table of contents, a quick start, an option reference table, the default sensor of each
  metric, and the supported Python version (written and tested with 3.14; at least 3.9 is needed for `zoneinfo`,
  older versions untried).
- Corrected statements that had gone stale: the intro now lists all six metrics, and the units note includes volts,
  watt-hours and bytes per second.

## [0.3.5] - 2026-10-06

### Added
- `--get` polls one snmp sensor and prints its current value and unit (`16 °C`, `3840 RPM`, `112 W`), for quick
  checks and scripts. It defaults to `--source snmp`, and `--metric` and `--sensor` choose the sensor. Nothing is
  stored, so the cache and any `--db` database are left alone.
- For a counter metric (`network`) it reads twice, 2 seconds apart, and prints bytes per second.
- `--output table|csv|xlsx|xls` shows the sensor as a one-row listing (with unit, limits and key); `--output text`
  is the bare value and the default. `--file` names a spreadsheet.
- `--get` is rejected with `--list`, `--no-fetch` and `--source web`, and an unknown sensor is reported with the
  sensors that exist. A failure exits with status 1.

### Fixed
- Spreadsheet messages said "1 sensors"; they now say "1 sensor".

## [0.3.4] - 2026-10-06

### Added
- `--list` accepts `--output text|table|csv|xlsx|xls`. `text`, the aligned plain text, is the default and is also
  available by name. `table` uses `terminaltables`. `csv`, `xlsx` and `xls` keep numbers as numbers and split the
  limits into four columns (`lower_critical`, `lower_warning`, `upper_warning`, `upper_critical`); a missing value is
  an empty cell.
- Spreadsheet listings use `--file`, or are named `<host>_sensors[_<source>][_<metric>]` in the current directory,
  for example `192.0.2.20_sensors_snmp_fan.xlsx`.
- `--output chart` and `--output raw` are rejected with `--list`, and `--output text` without `--list`.

### Changed
- `--output` no longer has a fixed default: it is `chart` for readings and `text` for `--list`.

### Fixed
- Writing a sensor listing to `.xlsx` or `.xls` failed with a traceback when every cell in a column was empty (for
  example the upper limits of the fans). The column width is now worked out safely.

## [0.3.3] - 2026-10-06

### Added
- The `--list` table has three new columns: `UNIT` (°C, RPM, V, W, A, Wh, B/s or code), `VALUE` (the current reading)
  and `LIMITS` (`warn lower..upper crit lower..upper`, `-` where missing). The `KEY` column stays last. The `METRIC`
  column was already there.
- SNMP sensors take their value and limits from the poll that built the listing. The web source's row uses the newest
  cached reading and the saved limits, or `-` when there are none, because a live value would need a full fetch.
  `network` counters show `-`.
- `list` functions of the sources now return a `Reading` (key, value, limits) for each sensor.
- The README's "List the sensors" example shows the new columns.

## [0.3.2] - 2026-10-06

### Added
- `--list sensors` lists every sensor of every source and metric with its source, in the same aligned table as
  before (source, metric, sensor, key). It is the same as `--list` on its own, now spelt out; the help text and error
  message name it, and the value is case-insensitive.

### Changed
- The `--list` help text and its error message now name all six metrics.

## [0.3.1] - 2026-10-06

### Added
- `--metric voltage`: the power supply input voltages (`ps1-voltage`, `ps2-voltage`), in volts. The discrete
  power-good probes in the same table return no reading and are skipped.
- `--metric health`: a status code per component (system, CPUs, memory modules, power supplies, intrusion,
  batteries, RAID controller and battery, disks, virtual disks, fan and power redundancy), 41 sensors on the test
  server, so state changes can be charted. Only the *status* columns are used, because the disk, virtual disk and
  battery *state* columns are numbered differently.
- `--metric network`: byte counters of the iDRAC's own interfaces (IF-MIB `ifHCInOctets` and `ifHCOutOctets`),
  shown as bytes per second between readings. The counter is what is stored; a counter that goes down (a restart) is
  skipped, and one reading explains that two are needed.
- New `power` sensors from the power usage table: `energy` (Wh), `peak-power`, `peak-current`, `idle-power`,
  `max-power`, `headroom` and `peak-headroom`.
- `--limits` draws a sensor's warning and critical limits on plotext `line` and `scatter` charts. Each SNMP poll saves
  them next to the cache (`<host>_<metric>-<sensor>.limits.json`), shared by sources.

### Changed
- Each metric now has its own reader, so metrics need not be a single probe table. An SNMP poll shares one engine
  and connection between its walks instead of creating one per walk, which made a 41-sensor health poll take 1.8 s
  instead of 4.5 s.
- `--list` shows the new sensors, and the sensor listing keys use the full row index.
- The `--metric` help and README list the new metrics.

### Fixed
- Readings were formatted with six significant digits, so a value of seven digits or more would have been stored
  in scientific notation and lost precision (for example the 2,224,483,828 byte counter, or an energy total over
  999,999 Wh). Values are now written in full.

### Added
- "More SNMP telemetry" section in `TODO.md`: voltage, threshold lines, health status, network traffic, energy and
  peak power, amperage units from the probe type, and the event log, with their OIDs confirmed against the Dell iDRAC MIB.

## [0.3.0] - 2026-10-06

### Added
- `--output xlsx` and `--output xls` write the selected readings to an Excel workbook (`XlsxWriter` for `.xlsx`,
  `xlwt` for `.xls`). One `Readings` sheet with a bold heading row and columns `Time (<zone>)`, `Time (UTC)`,
  `Average (<unit>)` and `Peak (<unit>)`; times are real Excel dates.
- `--file FILE` names the output file. It only applies to `xlsx` and `xls`, adds the extension if the name has none,
  and rejects a name with the other spreadsheet extension.
- Without `--file`, a name is made in the current directory:
  `<host>_<source>_<metric>-<sensor>_last-<period>.<ext>`, for example
  `192.0.2.20_web_temperature-inlet_last-1w.xlsx`.
- `.xls` refuses more than 65,535 readings, with a message suggesting `.xlsx` or a smaller `--last`.
- `XlsxWriter` and `xlwt` added to `requirements.txt`; "Spreadsheet output" section in the README.

## [0.2.9] - 2026-10-06

### Added
- `--output csv` prints the selected readings as CSV with `time`, `host`, `source`, `metric`, `sensor`, `average`,
  `peak` and `unit` columns, so each row describes itself. Times are ISO 8601 with their UTC offset, in local time or
  the zone chosen with `--tz` or `--utc`.
- `--output raw` prints the selected readings in the cache format (`Average,Peak,Time`, UTC times ending in `Z`).
- Both honour `--last` (`--last 0` means every reading). The older `--raw` flag is unchanged: it prints every
  stored reading and ignores `--last`.
- "CSV output" section in the README.

### Added
- "Example output" section in the README with real output for `--list`, line, bar and termgraph charts, the table
  output, SNMP readings with `--tz`, and `--raw`.

## [0.2.8] - 2026-10-06

### Added
- `--output table` prints the readings as a table (time, average and peak, with units) using `terminaltables`, in
  the zone chosen by `--tz` or `--utc`. It uses the plain ASCII style, which also works when piped. The chart title
  is printed above the table, because `terminaltables` drops a title wider than the table.
- `terminaltables` added to `requirements.txt`.
- "Table output" section in the README.

## [0.2.7] - 2026-10-06

### Added
- `--output` selects what is produced from the readings. `chart` is the default and the only choice for now.
  Outputs are registered in an `OUTPUTS` table, so new kinds (a table, JSON, ...) can be added later. `--module`,
  `--chart`, `--width` and `--height` apply to `chart`.

### Fixed
- Piping output into a command that exits early (for example `ragdoll.py --raw | head -2`) printed a
  `BrokenPipeError` traceback. The script now exits quietly.

## [0.2.6] - 2026-10-06

### Fixed
- The time labels on the x-axis of `line` and `scatter` charts (plotext) were shifted by this computer's UTC offset,
  so on an AEDT machine every label was 11 hours late: a sample at 06:00:31 local was labelled 17:00:31. This also
  affected `--tz` and `--utc`, which showed the same shift. plotext 5.3.2's date axis adds the machine's offset to
  whatever it is given. The chart now plots Unix times and draws the axis labels itself, in the chosen time zone.
  Bar, stacked and histogram charts, and termgraph, were not affected.

### Changed
- The number of x-axis labels on `line` and `scatter` charts follows the usable width: 3 at 80 columns, more in a
  wider terminal.

## [0.2.5] - 2026-10-06

### Changed
- When `--db` stores readings, any "no reading" rows (`-128`) that earlier versions (0.1.8 to 0.2.3) saved in the
  database are deleted and the file is compacted with `VACUUM`, so the space they used is freed. A test database
  holding 23,237 such rows shrank from about 990 KB to 8 KB. A database with no such rows is not touched.
  New readings of this kind were already never stored from 0.2.4.

## [0.2.4] - 2026-10-06

### Fixed
- Rows where the iDRAC reports `-128` for both Average and Peak, its marker for "no reading", are now skipped. They
  used to be charted as -128 °C spikes (23,237 of 48,984 rows, 47%, in one iDRAC's history). They are dropped when
  the web CSV is fetched, when a cache is read, when rows are stored in the database and when they are read back from
  it, so caches and databases from earlier versions are cleaned up without being rewritten.

### Changed
- `--raw` prints the readings that would be charted, in the cache format, instead of the cache file as it is, so it
  no longer includes the skipped rows. It still prints the file unchanged if it holds no readable rows, to help you
  inspect a malformed one.

## [0.2.3] - 2026-10-06

### Added
- `--tz ZONE` shows chart times in a named time zone (for example `Australia/Sydney` or `America/New_York`), using
  Python's `zoneinfo`, instead of this computer's local time zone. An unknown name is rejected, and `--tz` cannot
  be combined with `--utc`. The stored data, cache and `--raw` output stay in UTC.

## [0.2.2] - 2026-10-06

### Added
- `gui` is accepted as an alias for the `web` source in `--source` and `--list` (case-insensitive). The alias uses
  the same cache files and database rows as `web`.

## [0.2.1] - 2026-10-06

### Added
- `--username` as an alias for `--user`, and `--password` as an alias for `--pass`.

## [0.2.0] - 2026-10-06

### Fixed
- Times from the two sources were in different, unmarked time zones: snmp readings were stamped with this
  computer's local time and the web source's CSV used the iDRAC's own wall clock, so combining them in `--db` or
  in one chart misaligned them (by 11 hours on the author's setup).

### Changed
- All times are now UTC inside ragdoll. The cache and `--raw` output use ISO 8601 with a `Z` (for example
  `2026-10-06T04:27:44Z`) and the database stores `time` as an integer Unix timestamp (schema version 2).
- The web source converts the iDRAC's wall clock times to UTC when it fetches them. The offset is measured by
  comparing the iDRAC's clock (via Redfish `DateTime`) with this computer's clock, not taken from the iDRAC's
  configured time zone, which can be wrong.
- Charts show times in local time, with the time zone name in the title (for example `[AEDT]`).
- Caches and databases from earlier versions are not read. An old cache is refetched (web) or ignored (snmp), and an
  old database is refused with a message asking you to delete it and run again.

### Added
- `--utc` shows chart times in UTC.
- `--tz-offset OFFSET` sets the offset of the iDRAC's clock from UTC for the web source (for example `UTC`, `+10:00`,
  `-05:00`), instead of measuring it.
- "Time zones" section in the README.

### Added
- `TODO.md`, with database performance measurements and planned improvements.

## [0.1.9] - 2026-10-06

### Added
- `--no-fetch` charts what is already stored without contacting the iDRAC: from the `--db` database if given,
  otherwise from the cache regardless of age. `--raw` prints the stored readings as CSV.
- "Charting without contacting the iDRAC" section in the README.

### Changed
- `--no-fetch` is rejected with `--list`, `--save-credentials` or `--refresh`, which need the iDRAC.

## [0.1.8] - 2026-10-06

### Added
- `--db [PATH]` stores readings in a SQLite database, using the built-in `sqlite3` module, and draws charts from
  it so that history from earlier runs and sources is included. Without `PATH` the database is
  `~/.local/share/ragdoll/ragdoll.db` (or `$XDG_DATA_HOME/ragdoll/ragdoll.db`).
- One `readings` table keyed on host, source, metric, sensor and time. Rows already stored are skipped, so
  repeating a run is safe; the web source's full history (about 61,000 rows) is stored in about a second.
- "Storing readings in SQLite" section in the README.

### Added
- "Chart CPU temperatures" example in the README, including how to build up SNMP history with cron.

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
