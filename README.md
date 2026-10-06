# ragdoll

Redfish/API/GUI/DRAC/Other Log Linter - Converts iDRAC Telemetry and other information into more useful formats.

At the moment it fetches temperature, fan speed and power data from a Dell iDRAC and draws it as a chart in the terminal.

It can read from two sources:

| Source | Metrics | How it works | History |
|---|---|---|---|
| `web` (default; also `gui`) | temperature | Logs in to the iDRAC web interface and downloads the temperature statistics CSV | Hourly history held by the iDRAC |
| `snmp` | temperature, fan, power | Reads the current value from the Dell probe tables over SNMP v2c | Only the current value; each run appends a reading to the local cache, so history builds up over time |

## Version

Current version: **0.3.0**. Print it with `python3 ragdoll.py --version`.

Versions are `MAJOR.MINOR.PATCH` with no number above 9: when one would pass 9 it rolls over into the next,
so 0.0.9 is followed by 0.1.0. See [CHANGELOG.md](CHANGELOG.md) for what changed in each version.

## Install

```
pip install -r requirements.txt
```

This installs `requests` (web source), `pysnmp` (snmp source), `keyring` (saved credentials), `terminaltables` (table output), `XlsxWriter` and `xlwt` (`.xlsx` and `.xls` output), and the two graphing modules, `plotext` and `termgraph`.
`plotext` is pinned to 5.3.2 because 6.x has a different API.

## Capabilities

- **Sources:** `--source web|snmp`.
- **Source names:** `gui` is an alias for `web` in `--source` and `--list`, for example `--source gui`.
- **Metrics:** `--metric temperature|fan|power` (default `temperature`). `fan` and `power` need `--source snmp`.
- **Sensors:** `--list` prints an aligned table of `source`, `metric`, `sensor` and the iDRAC's key for every sensor, from every source and metric.
  A source or metric after it narrows the listing (`--list snmp`, `--list fan`), and so do `--source` and `--metric`.
  A source that can't be reached or needs credentials you haven't given is reported on stderr and skipped.
  `--sensor` selects one sensor to graph.
  Unknown sensor names are rejected. The defaults are `inlet`, `fan1a` and `system-power`.

  | Metric | Unit | Example sensors (snmp) |
  |---|---|---|
  | `temperature` | °C | `inlet`, `exhaust`, `cpu1`, `cpu2` (the web source offers `inlet` only) |
  | `fan` | RPM | `fan1a`, `fan1b`, `fan2a` ... `fan7b` |
  | `power` | W or A | `system-power` (watts), `ps1-current`, `ps2-current` (amps) |
- **Credentials:** never stored in the script. The web source takes `--user` and `--pass` (also spelt `--username` and `--password`); the snmp source takes `--community`.
  Missing credentials are looked up in this order: command-line flag, environment variable
  (`IDRAC_USER`, `IDRAC_PASS`, `IDRAC_COMMUNITY`), the OS keyring, and finally a password prompt.
  See [Saving credentials](#saving-credentials).
- **Output:** `--output chart|table|raw|csv|xlsx|xls` selects what is produced from the readings (default `chart`).
  `table` uses `terminaltables`; `raw` and `csv` print CSV; `xlsx` and `xls` write a spreadsheet, named with `--file`.
  Other kinds of output can be added later without changing the other options; `--module`, `--chart`, `--width` and
  `--height` apply to `chart` only. See [Table output](#table-output), [CSV output](#csv-output) and
  [Spreadsheet output](#spreadsheet-output).
- **Charts:** `--chart vertical|horizontal|stacked|histogram|line|scatter` (default `vertical`).
  `line` and `scatter` need plotext.
- **Graphing module:** `--module plotext|termgraph` (default `plotext`).
- **Size:** `--width` (default 80) and `--height` (default 24) fit a standard terminal.
  termgraph ignores `--height`.
- **Time range:** `--last` takes a row count (default `10`) or a period such as `hour`, `day`, `week`, `month`, `year`, `6h`, `2d`.
  Periods are counted back from the newest sample in the data, not from the local clock.
- **Caching:** data is cached per host, source, metric and sensor, for example `<host>_snmp_fan-fan1a.csv`.
  - Location: `~/.cache/ragdoll` (or `$XDG_CACHE_HOME/ragdoll`), changed with `--cachedir` (`--cache-dir` also works).
  - Web data is reused until it is `--max-age` seconds old (default 3600); `--refresh` forces a fetch.
  - If a fetch fails and a cache file exists, the cache is used and a warning is printed.
  - The snmp source polls on every run.
- **SQLite database:** `--db [PATH]` also stores the readings in a SQLite database and charts from it, so history from
  earlier runs is included. See [Storing readings in SQLite](#storing-readings-in-sqlite).
- **Offline charts:** `--no-fetch` charts what is already stored without contacting the iDRAC.
  See [Charting without contacting the iDRAC](#charting-without-contacting-the-idrac).
- **Time zones:** all times are kept in UTC and shown in local time. `--tz ZONE` shows a named zone instead
  (for example `--tz Australia/Sydney`), `--utc` shows UTC, and `--tz-offset` sets the web source's clock offset. See [Time zones](#time-zones).
- **Raw output:** `--raw` prints the CSV instead of a chart. Its times are UTC, for example `2026-10-06T04:27:44Z`.
- **TLS:** the iDRAC certificate is not verified by default (they are usually self-signed); `--secure` turns verification on.

## Time zones

Times from the two sources are in different clocks, so ragdoll converts everything to **UTC** when it reads or
polls, and stores it that way: as ISO 8601 text ending in `Z` in the cache and `--raw` output, and as Unix
timestamps in the database. Charts convert back to your computer's local time (the time zone name is shown in the
title, for example `[AEDT]`).

- `--tz ZONE` shows any named time zone instead, using the IANA names from your system's time zone database,
  for example `--tz Australia/Sydney`, `--tz America/New_York` or `--tz Asia/Tokyo`. Daylight saving is applied,
  so the same sample can show as `[EDT]` in summer and `[EST]` in winter.
- `--utc` is the same as `--tz UTC`. The two cannot be combined.
- `--tz` only changes how times are displayed. It is unrelated to `--tz-offset`, which says what time the
  iDRAC's own clock shows, so that its CSV can be converted to UTC.

- **snmp:** the iDRAC does not report a time over SNMP, so each reading is stamped with this computer's clock at the
  moment it was polled. Keep this computer's clock correct (for example with NTP).
- **web:** the iDRAC's CSV uses the iDRAC's own wall clock with no time zone. ragdoll converts it to UTC using the
  clock's offset from UTC, which it measures by comparing the iDRAC's clock (read through Redfish) with this
  computer's clock, rounded to the nearest quarter hour. It does not trust the time zone configured on the iDRAC,
  because an iDRAC can show UTC while set to another time zone. If Redfish is unavailable, or you want to override
  the measurement, pass `--tz-offset` (for example `--tz-offset UTC`, `+10:00` or `-05:00`). Conversion happens
  when the data is fetched, so the cached copy and the database are already in UTC.
- Cache files and databases written by versions before 0.2.0 stored local or iDRAC times with no zone. They are
  not read: an old cache is refetched or ignored, and an old database is refused with a message. Delete the
  database and run again to rebuild it.

## Storing readings in SQLite

`--db` keeps every reading in a SQLite database, using Python's built-in `sqlite3` module (nothing extra to install).
The charts are then drawn from the database, so they include readings from every earlier run and from both the
web source and snmp.

```
python3 ragdoll.py --host 192.0.2.20 --source snmp --sensor cpu1 --db --chart line --last week
python3 ragdoll.py --host 192.0.2.20 --source snmp --sensor cpu1 --db /path/to/readings.db --raw
```

- `--db` on its own uses `~/.local/share/ragdoll/ragdoll.db` (or `$XDG_DATA_HOME/ragdoll/ragdoll.db`);
  `--db PATH` uses a database of your choosing. It is created on first use.
- Storing is repeatable: a reading already in the database is skipped, so each run can safely pass its whole
  history. The web source's full history (about 61,000 hourly samples) is stored on the first run and takes
  around a second.
- With `--raw`, readings are stored and the CSV is printed without drawing a chart, which suits a cron job.
- Without `--db`, nothing is written to a database and the charts come from the cache as before.

One table, `readings`, with one row per sample:

| Column | Meaning |
|---|---|
| `host`, `source`, `metric`, `sensor` | what was read, for example `192.0.2.20`, `snmp`, `temperature`, `cpu1` |
| `time` | Unix time of the sample (integer seconds since 1970-01-01 UTC) |
| `average`, `peak` | the reading in the unit for that metric (°C, RPM, W or A); identical for snmp |

The primary key is `(host, source, metric, sensor, time)`. Query it with any SQLite tool, for example:

```
sqlite3 ~/.local/share/ragdoll/ragdoll.db \
  "SELECT time, average FROM readings WHERE sensor = 'cpu1' ORDER BY time DESC LIMIT 10"
```

## Table output

`--output table` prints the selected readings as a table, oldest first, instead of drawing a chart. It uses the plain
ASCII style of `terminaltables`, so it also works when piped or redirected.

```
python3 ragdoll.py --host 192.0.2.20 --source snmp --metric fan --sensor fan1a --last 5 --output table
```
```
192.0.2.20 Fan1A Speed (Average / Peak) [AEDT]
+---------------------+---------------+------------+
| Time (AEDT)         | Average (RPM) | Peak (RPM) |
+---------------------+---------------+------------+
| 2026-10-06 15:53:05 |          3840 |       3840 |
+---------------------+---------------+------------+
```

- The time column is in local time, or the zone given with `--tz` or `--utc`; the zone name is in the header.
- The unit follows the metric (°C, RPM, W or A).
- `--last` chooses how many rows (a row count or a period), as for charts. The default is 10 rows.
- `--module`, `--chart`, `--width` and `--height` have no effect on a table.

## CSV output

Two CSV outputs print the selected readings, oldest first. `--last` chooses how many (default 10; `--last 0`
means every reading).

- **`--output csv`** is for spreadsheets, databases and scripts. Each row says what it is, so files from different
  hosts or sensors can be joined. The time is ISO 8601 with its UTC offset, in local time or the zone from `--tz` or
  `--utc`.
- **`--output raw`** is the form ragdoll itself stores: `Average,Peak,Time`, with UTC times ending in `Z`. It is
  what is kept in the cache.

```
$ python3 ragdoll.py --host 192.0.2.20 --source gui --last 3 --output csv
time,host,source,metric,sensor,average,peak,unit
2026-10-06T04:00:31+11:00,192.0.2.20,web,temperature,inlet,14,14,°C
2026-10-06T05:00:31+11:00,192.0.2.20,web,temperature,inlet,14,14,°C
2026-10-06T06:00:31+11:00,192.0.2.20,web,temperature,inlet,14,14,°C
```

```
$ python3 ragdoll.py --host 192.0.2.20 --source gui --last 3 --output raw
Average,Peak,Time
14,14,2026-10-05T17:00:31Z
14,14,2026-10-05T18:00:31Z
14,14,2026-10-05T19:00:31Z
```

`--output raw` is not the same as the older `--raw` flag. `--raw` prints every stored reading, ignores `--last`, and
exits before any other output is chosen; it is kept so existing scripts and cron jobs keep working. `--output raw`
prints only the selected readings.

## Spreadsheet output

`--output xlsx` writes an Excel workbook, and `--output xls` writes the older Excel 97-2003 format. `--file` names the
file; without it a name is made in the current directory.

```
$ python3 ragdoll.py --host 192.0.2.20 --source gui --last week --output xlsx
Wrote 104 readings to 192.0.2.20_web_temperature-inlet_last-1w.xlsx

$ python3 ragdoll.py --host 192.0.2.20 --source gui --last 3 --output xls --file inlet.xls
Wrote 3 readings to inlet.xls
```

- **File name:** `--file report` becomes `report.xlsx` (or `.xls`) if it has no extension. A name ending in the other
  spreadsheet extension is rejected, and `--file` with any other `--output` is an error. An existing file is
  overwritten.
- **Automatic name:** `<host>_<source>_<metric>-<sensor>_last-<period>.<ext>`, where the period describes `--last`:
  `last-10rows`, `last-all` (for `--last 0`), `last-2h`, `last-1d`, `last-1w`, `last-1mo` or `last-1y`. The metric and
  sensor are included so different sensors do not overwrite each other.
- **Contents:** one sheet, `Readings`, with a bold heading row (frozen in `.xlsx`) and four columns: `Time (<zone>)`
  in the zone from `--tz`, `--utc` or local time, `Time (UTC)`, `Average (<unit>)` and `Peak (<unit>)`. Times are real
  Excel dates formatted `yyyy-mm-dd hh:mm:ss`, so they can be charted and sorted. Spreadsheets have no time zone
  type, which is why both times are given.
- `--last` chooses how many readings, as for the other outputs (default 10; `--last 0` is every reading).
- **`.xls` limit:** the old format allows 65,535 readings. More is refused with a message; use `.xlsx` or a smaller
  `--last`. `.xlsx` has no such limit in practice (the 25,747-row history of one iDRAC is a 490 KB file).
- Automatically named files go in the current directory, so add `*.xlsx` and `*.xls` to `.gitignore` if you run it
  inside a repository.

## Charting without contacting the iDRAC

`--no-fetch` skips the fetch or SNMP poll and charts what is already stored. No credentials are needed and nothing
is added to the cache or the database.

```
# from the database (with --db)
python3 ragdoll.py --host 192.0.2.20 --source snmp --sensor cpu1 --db --no-fetch --chart line --last week

# from the cache, however old it is (without --db)
python3 ragdoll.py --host 192.0.2.10 --no-fetch --chart line --last month
```

- With `--db` the readings come from the database; without it, from the cache file, ignoring `--max-age`.
- With `--raw`, the stored readings are printed as CSV.
- If nothing is stored for that host, source, metric and sensor, it says so and asks you to run once without
  `--no-fetch`.
- It cannot be combined with `--list`, `--save-credentials` or `--refresh`, which all need to contact the iDRAC.
- Sensor names cannot be checked against the iDRAC in this mode, so a misspelt sensor is reported as having no
  stored readings.

## Saving credentials

Credentials can be kept in the operating system's keyring (Secret Service on Linux, Keychain on macOS,
Credential Manager on Windows) through the `keyring` module, so they never appear on the command line or in shell history.

```
# log in once and save; credentials are only saved if the login succeeds
python3 ragdoll.py --host 192.0.2.10 --user <user> --pass <password> --save-credentials --list

# afterwards no credentials are needed
python3 ragdoll.py --host 192.0.2.10 --last week --chart line

# remove them again
python3 ragdoll.py --host 192.0.2.10 --forget-credentials
```

- Entries are stored per host, under the service name `ragdoll:<host>`.
- If you give `--user` without a password, you are prompted for it.
- On a machine without a keyring (a headless server or a cron job), use the `IDRAC_USER`, `IDRAC_PASS` and
  `IDRAC_COMMUNITY` environment variables instead, for example loaded from a file with mode 600.
- Cached data files in `~/.cache/ragdoll` contain sensor readings only, never credentials.

## Examples

List the sensors on an iDRAC:

```
python3 ragdoll.py --host 192.0.2.10 --user <user> --pass <password> --list
python3 ragdoll.py --host 192.0.2.20 --community <community> --list snmp
python3 ragdoll.py --host 192.0.2.20 --community <community> --list fan
```

Line chart of the last week of inlet temperature from the web interface:

```
python3 ragdoll.py --host 192.0.2.10 --user <user> --pass <password> --last week --chart line
```

Last 12 samples as horizontal bars using termgraph:

```
python3 ragdoll.py --host 192.0.2.10 --user <user> --pass <password> --last 12 --module termgraph --chart horizontal
```

List the fan and power sensors, then chart them:

```
python3 ragdoll.py --host 192.0.2.20 --source snmp --community <community> --metric fan --list
python3 ragdoll.py --host 192.0.2.20 --source snmp --community <community> --metric fan --sensor fan1a --chart line
python3 ragdoll.py --host 192.0.2.20 --source snmp --community <community> --metric power --sensor system-power --chart line
```

### Chart CPU temperatures

CPU temperatures are only available over SNMP; the web source offers `inlet` only.

```
python3 ragdoll.py --host 192.0.2.20 --source snmp --community <community> --metric temperature --sensor cpu1 --chart line --last day
```

- `--metric temperature` is the default, so it can be left out.
- `--sensor cpu1` selects the CPU1 probe. Use `cpu2` for the second CPU, and `--list` to see every sensor.
- Leave out `--community` if the host uses the standard default, or save it once with `--save-credentials`.
- SNMP returns only the current value. Each run adds one reading to the cache (`~/.cache/ragdoll`), and the chart
  shows what has been collected so far, so the first run shows a single point.

To build up history, run it periodically with `--raw`, which takes a reading without drawing a chart, for example
from cron every 10 minutes:

```
*/10 * * * * python3 /path/to/ragdoll.py --host 192.0.2.20 --source snmp --community <community> --sensor cpu1 --raw >/dev/null
```

Once there are enough readings, the `--chart line --last day` command above shows how CPU1 changed over the day.
For a quick look at the latest readings as bars, use `--last 12 --chart horizontal`.

Avoid leaving the password in your shell history: put a space before the command if your shell ignores
space-prefixed commands, or pass it from an environment variable, e.g. `--pass "$IDRAC_PASS"`.

## Example output

Real output, with the iDRAC's address replaced by a documentation address. Times are in the machine's local time
zone (AEDT here), which is named in each title.

### List the sensors

`--list temperature` shows every temperature sensor from every source. This iDRAC offers the same inlet sensor
through the web interface and SNMP, and the extra sensors only through SNMP.

```
$ python3 ragdoll.py --host 192.0.2.20 --list temperature
SOURCE  METRIC       SENSOR   KEY
snmp    temperature  cpu1     1.3.6.1.4.1.674.10892.5.4.700.20.1.6.1.3
snmp    temperature  cpu2     1.3.6.1.4.1.674.10892.5.4.700.20.1.6.1.4
snmp    temperature  exhaust  1.3.6.1.4.1.674.10892.5.4.700.20.1.6.1.2
snmp    temperature  inlet    1.3.6.1.4.1.674.10892.5.4.700.20.1.6.1.1
web     temperature  inlet    iDRAC.Embedded.1#Inlet.1#ThermalHistory
```

### Line chart

A line chart suits trends best, because its scale fits the data.

```
$ python3 ragdoll.py --host 192.0.2.20 --source gui --last day --chart line --height 16
               192.0.2.20 Inlet Temperature (Average / Peak) [AEDT]
     ┌─────────────────────────────────────────────────────────────────────────┐
18.00┤ ▞▞ Average                                                              │
17.33┤ ▞▞ Peak                                                                 │
     │   ▝▚▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▖                                           │
16.67┤       ▗▞▘              ▝▚▄  ▝▚▄                                         │
16.00┤     ▄▞▘                   ▀▄▄▄▄▀▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄                        │
     │   ▄▀                                  ▀▄        ▚▖                      │
15.33┤ ▄▀                                      ▀▄       ▝▚▖                    │
14.67┤▀                                          ▀▀▀▀▀▀▀▀▀▝▀▀▀▀▀▚▖             │
     │                                                      ▝▚▖  ▝▚▖           │
14.00┤                                                        ▝▚▄▄▄▝▚▄▄▄▄▄▄▄▄▄▄│
     └┬───────────────────────────────────┬───────────────────────────────────┬┘
   05/10/2026 15:00:32          05/10/2026 22:30:31         06/10/2026 06:00:31
°C
```

### Bar charts

The default chart is `vertical`. Bar charts start their scale at zero, so a small change between readings is hard to
see; use `line` for that. termgraph's `horizontal` bars show the timestamp and value on each line.

```
$ python3 ragdoll.py --host 192.0.2.20 --source gui --last 12 --chart vertical --height 14
               192.0.2.20 Inlet Temperature (Average / Peak) [AEDT]
    ┌──────────────────────────────────────────────────────────────────────────┐
17.0┤ ██ Average ███████████████████   ███   ███                               │
14.2┤ ██ Peak    ██████████████████████████████████████████████████████████████│
11.3┤██████████████████████████████████████████████████████████████████████████│
 8.5┤██████████████████████████████████████████████████████████████████████████│
    │██████████████████████████████████████████████████████████████████████████│
 5.7┤██████████████████████████████████████████████████████████████████████████│
 2.8┤██████████████████████████████████████████████████████████████████████████│
 0.0┤██████████████████████████████████████████████████████████████████████████│
    └─────────┬───────────────────────┬──────────────────────────────┬─────────┘
       Oct 05 20:00:31         Oct 06 00:00:31             Oct 06 05:00:31
°C
```

```
$ python3 ragdoll.py --host 192.0.2.20 --source gui --last 4 --chart horizontal --module termgraph
# 192.0.2.20 Inlet Temperature (Average / Peak) [AEDT]

▇ Average ▇ Peak

Oct_06_03:00:31: ▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇ 14.00°C
                 ▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇ 15.00°C
Oct_06_04:00:31: ▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇ 14.00°C
                 ▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇ 14.00°C
Oct_06_05:00:31: ▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇ 14.00°C
                 ▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇ 14.00°C
Oct_06_06:00:31: ▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇ 14.00°C
                 ▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇ 14.00°C
```

### Table

```
$ python3 ragdoll.py --host 192.0.2.20 --source gui --last 5 --output table
192.0.2.20 Inlet Temperature (Average / Peak) [AEDT]
+---------------------+--------------+-----------+
| Time (AEDT)         | Average (°C) | Peak (°C) |
+---------------------+--------------+-----------+
| 2026-10-06 02:00:31 |           15 |        15 |
| 2026-10-06 03:00:31 |           14 |        15 |
| 2026-10-06 04:00:31 |           14 |        14 |
| 2026-10-06 05:00:31 |           14 |        14 |
| 2026-10-06 06:00:31 |           14 |        14 |
+---------------------+--------------+-----------+
```

SNMP gives the current value only, so Average and Peak are the same, and `--tz` changes the zone shown:

```
$ python3 ragdoll.py --host 192.0.2.20 --source snmp --metric fan --sensor fan1a --last 3 --output table --tz UTC
192.0.2.20 Fan1A Speed (Average / Peak) [UTC]
+---------------------+---------------+------------+
| Time (UTC)          | Average (RPM) | Peak (RPM) |
+---------------------+---------------+------------+
| 2026-10-06 04:53:05 |          3840 |       3840 |
| 2026-10-06 04:54:26 |          3840 |       3840 |
+---------------------+---------------+------------+
```

### Raw CSV

`--raw` prints all the readings in the cache format, with UTC times. Only the first rows are shown here.

```
$ python3 ragdoll.py --host 192.0.2.20 --raw | head -4
Average,Peak,Time
22,23,2016-10-14T15:59:16Z
20,21,2016-10-17T13:59:19Z
20,21,2016-10-17T14:59:19Z
```

## Notes

- The iDRAC writes `-128` for both Average and Peak when a sample has no reading (on one iDRAC, nearly half of its
  history). These rows are skipped everywhere: they are not charted, printed by `--raw`, cached or stored, and a
  database written by an earlier version that holds them is cleaned the next time `--db` stores readings: the
  rows are deleted and the file is compacted, which frees the space they used.
- Units are °C, RPM, watts and amps. The iDRAC reports temperatures and power supply currents in tenths, and these are converted.
- Each metric and sensor has its own cache file, so build up history with one cron entry per sensor you care about.
- The "Average" and "Peak" series are identical for SNMP, since each reading is a single value.
- termgraph does not scale a chart whose values are all identical (common for fans and power on an idle server), so for those it draws a full-width bar and shows the value in the label. plotext has no such limitation.
- The iDRAC web interface ignores unknown sensor names and returns inlet data, which is why sensor names are checked against the list first.
- Sources are registered in the `SOURCES` table in `ragdoll.py`; adding another (for example Redfish) means providing a `list` and a `fetch` function.

## License

This work is licensed under the [Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International
License](https://creativecommons.org/licenses/by-nc-sa/4.0/) (CC BY-NC-SA 4.0). The full text is in [LICENSE](LICENSE).

In short: you may share and adapt it with attribution, not for commercial purposes, and you must distribute
your changes under the same license.

## Help Support Development

If you find this software useful and would like to support its development, please consider buying me a coffee:

https://ko-fi.com/richardatlateralblast
