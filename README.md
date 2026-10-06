# idrac_temp

Fetches temperature, fan speed and power data from a Dell iDRAC and draws it as a chart in the terminal.

It can read from two sources:

| Source | Metrics | How it works | History |
|---|---|---|---|
| `web` (default) | temperature | Logs in to the iDRAC web interface and downloads the temperature statistics CSV | Hourly history held by the iDRAC |
| `snmp` | temperature, fan, power | Reads the current value from the Dell probe tables over SNMP v2c | Only the current value; each run appends a reading to the local cache, so history builds up over time |

## Version

Current version: **0.1.4**. Print it with `python3 idrac_temp.py --version`.

Versions are `MAJOR.MINOR.PATCH` with no number above 9: when one would pass 9 it rolls over into the next,
so 0.0.9 is followed by 0.1.0. See [CHANGELOG.md](CHANGELOG.md) for what changed in each version.

## Install

```
pip install -r requirements.txt
```

This installs `requests` (web source), `pysnmp` (snmp source), `keyring` (saved credentials), and the two graphing modules, `plotext` and `termgraph`.
`plotext` is pinned to 5.3.2 because 6.x has a different API.

## Capabilities

- **Sources:** `--source web|snmp`.
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
- **Credentials:** never stored in the script. The web source takes `--user` and `--pass`; the snmp source takes `--community`.
  Missing credentials are looked up in this order: command-line flag, environment variable
  (`IDRAC_USER`, `IDRAC_PASS`, `IDRAC_COMMUNITY`), the OS keyring, and finally a password prompt.
  See [Saving credentials](#saving-credentials).
- **Charts:** `--chart vertical|horizontal|stacked|histogram|line|scatter` (default `vertical`).
  `line` and `scatter` need plotext.
- **Graphing module:** `--module plotext|termgraph` (default `plotext`).
- **Size:** `--width` (default 80) and `--height` (default 24) fit a standard terminal.
  termgraph ignores `--height`.
- **Time range:** `--last` takes a row count (default `10`) or a period such as `hour`, `day`, `week`, `month`, `year`, `6h`, `2d`.
  Periods are counted back from the newest sample in the data, not from the local clock.
- **Caching:** data is cached per host, source, metric and sensor, for example `<host>_snmp_fan-fan1a.csv`.
  - Location: `~/.cache/idrac` (or `$XDG_CACHE_HOME/idrac`), changed with `--cache-dir`.
  - Web data is reused until it is `--max-age` seconds old (default 3600); `--refresh` forces a fetch.
  - If a fetch fails and a cache file exists, the cache is used and a warning is printed.
  - The snmp source polls on every run.
- **Raw output:** `--raw` prints the CSV instead of a chart.
- **TLS:** the iDRAC certificate is not verified by default (they are usually self-signed); `--secure` turns verification on.

## Saving credentials

Credentials can be kept in the operating system's keyring (Secret Service on Linux, Keychain on macOS,
Credential Manager on Windows) through the `keyring` module, so they never appear on the command line or in shell history.

```
# log in once and save; credentials are only saved if the login succeeds
python3 idrac_temp.py --host 192.0.2.10 --user <user> --pass <password> --save-credentials --list

# afterwards no credentials are needed
python3 idrac_temp.py --host 192.0.2.10 --last week --chart line

# remove them again
python3 idrac_temp.py --host 192.0.2.10 --forget-credentials
```

- Entries are stored per host, under the service name `idrac-temp:<host>`.
- If you give `--user` without a password, you are prompted for it.
- On a machine without a keyring (a headless server or a cron job), use the `IDRAC_USER`, `IDRAC_PASS` and
  `IDRAC_COMMUNITY` environment variables instead, for example loaded from a file with mode 600.
- Cached data files in `~/.cache/idrac` contain sensor readings only, never credentials.

## Examples

List the sensors on an iDRAC:

```
python3 idrac_temp.py --host 192.0.2.10 --user <user> --pass <password> --list
python3 idrac_temp.py --host 192.0.2.20 --community <community> --list snmp
python3 idrac_temp.py --host 192.0.2.20 --community <community> --list fan
```

Line chart of the last week of inlet temperature from the web interface:

```
python3 idrac_temp.py --host 192.0.2.10 --user <user> --pass <password> --last week --chart line
```

Last 12 samples as horizontal bars using termgraph:

```
python3 idrac_temp.py --host 192.0.2.10 --user <user> --pass <password> --last 12 --module termgraph --chart horizontal
```

List the fan and power sensors, then chart them:

```
python3 idrac_temp.py --host 192.0.2.20 --source snmp --community <community> --metric fan --list
python3 idrac_temp.py --host 192.0.2.20 --source snmp --community <community> --metric fan --sensor fan1a --chart line
python3 idrac_temp.py --host 192.0.2.20 --source snmp --community <community> --metric power --sensor system-power --chart line
```

Take an SNMP reading of a CPU sensor and chart the readings collected so far:

```
python3 idrac_temp.py --host 192.0.2.20 --source snmp --community <community> --sensor cpu1 --chart line
```

To build SNMP history, run it periodically, for example from cron every 10 minutes with `--raw` so nothing is drawn:

```
*/10 * * * * python3 /path/to/idrac_temp.py --host 192.0.2.20 --source snmp --community <community> --sensor inlet --raw >/dev/null
```

Avoid leaving the password in your shell history: put a space before the command if your shell ignores
space-prefixed commands, or pass it from an environment variable, e.g. `--pass "$IDRAC_PASS"`.

## Notes

- Units are °C, RPM, watts and amps. The iDRAC reports temperatures and power supply currents in tenths, and these are converted.
- Each metric and sensor has its own cache file, so build up history with one cron entry per sensor you care about.
- The "Average" and "Peak" series are identical for SNMP, since each reading is a single value.
- termgraph does not scale a chart whose values are all identical (common for fans and power on an idle server), so for those it draws a full-width bar and shows the value in the label. plotext has no such limitation.
- The iDRAC web interface ignores unknown sensor names and returns inlet data, which is why sensor names are checked against the list first.
- Sources are registered in the `SOURCES` table in `idrac_temp.py`; adding another (for example Redfish) means providing a `list` and a `fetch` function.

## License

This work is licensed under the [Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International
License](https://creativecommons.org/licenses/by-nc-sa/4.0/) (CC BY-NC-SA 4.0). The full text is in [LICENSE](LICENSE).

In short: you may share and adapt it with attribution, not for commercial purposes, and you must distribute
your changes under the same license.

## Help Support Development

If you find this software useful and would like to support its development, please consider buying me a coffee:

https://ko-fi.com/richardatlateralblast
