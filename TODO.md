# TODO

## Database performance and time series storage

### Measurements

Timings for the web source's full history (61,320 hourly samples), taken on 2026-10-06 with the SQLite
database from `--db`:

| Step | Time |
|---|---|
| Parse the CSV | 0.10 s |
| Convert timestamps (`strptime` on each row) | 0.54 s |
| Insert all rows into SQLite | 0.10 s |
| Re-insert the same rows (all duplicates) | 0.10 s |
| Query the last day from SQL | 0.003 s |
| Load every row | 0.05 s |
| Convert timestamps back for charting | 0.15 s |

SQLite accounts for about 0.25 s of the roughly 1.3 s a run takes with `--db`. Most of the rest is Python converting
timestamps for every row, even when only the last 10 rows are charted. A different database would not fix that.

### Planned changes (not yet done or timed)

- [x] Store `time` as a Unix timestamp integer instead of ISO text (done in 0.2.0, together with the move to UTC).
- [ ] Apply `--last` in SQL (`WHERE time >= ...`) instead of loading every row and trimming in Python.
- [ ] Insert only rows newer than the latest stored time for that sensor, so re-running the web source is nearly free.
- [ ] Downsample before charting. An 80-column terminal cannot show 720 hourly points for `--last month`, so average
  the readings into buckets (for example hourly or daily) with a `GROUP BY` on the stored time. This would help
  more than changing the database.

### Alternatives considered

Staying on SQLite is the recommendation. It comfortably handles hundreds of millions of rows, and 30 SNMP sensors
polled every 10 minutes add only about 1.5 million rows a year.

| Option | When it would be worth it |
|---|---|
| DuckDB (embedded, `pip install duckdb`) | Heavy analytical queries over millions of rows, such as averages per hour or day across many sensors and hosts |
| InfluxDB, TimescaleDB, VictoriaMetrics, Prometheus | Grafana dashboards, retention policies or many hosts. They are separate server processes, so they add an install and a service to maintain |
| Parquet files | Compact archiving. Loses SQL and easy appends |

## Time zones

- [x] The web source's CSV timestamps are assumed to use the iDRAC's wall clock (the same clock Redfish reports as
  `DateTime`). Confirmed on 192.168.8.240 (2026-10-06): its clock is correctly set to +11:00 and its newest sample,
  06:00:31 wall time, would be 1.4 hours in the future if the CSV used UTC. Still unconfirmed on 192.168.8.98, whose
  history stops on 2025-09-17.
- [ ] 61,320 CSV rows from 192.168.8.98 become 61,289 database rows: 31 samples share a timestamp with another
  sample (likely a clock change in the iDRAC's history) and the primary key keeps only the first.
- [ ] Offer `--tz-offset` as a saved per-host setting instead of repeating it.

## Data quality

- [x] The web history from 192.168.8.240 has 23,237 of 48,984 rows (47%) with a reading of `-128`, the iDRAC's marker
  for "no reading". Skipped everywhere since 0.2.4. Possible follow-up: an option to keep them.
- [ ] The web history on 192.168.8.240 lags real time and seems to be written in batches. On 2026-10-06 its newest sample
  stayed at 19:00:31Z (05 Oct) for about 12 hours, then twelve hourly samples up to 07:00:30Z appeared together. Two
  observations are not enough to say it is a 12-hour cycle. Consider showing the age of the newest sample on the chart,
  or using SNMP for current values.

## More SNMP telemetry

The first five items below were implemented in 0.3.1. Still open: amperage units from the probe type, the event log, and
the follow-ups at the end of this section.

Found by walking the Dell tree (`1.3.6.1.4.1.674.10892.5`) on 192.168.8.240, an R630 with iDRAC8, on 2026-10-06, and
**confirmed against the Dell iDRAC MIB** (`IDRAC-MIB-SMIv2`, header version 4.3) the same day. The MIB was obtained
from the LibreNMS repository's copy of it, not directly from Dell, and every OID and enumeration below was resolved
with `snmptranslate`. The probe tables share one layout: column 5 status, 6 reading, 7 type, 8 name, 10 upper critical
threshold, 11 upper non-critical, 12 lower non-critical, 13 lower critical.

- [x] **`voltage` metric.** Table `4.600.20` (`voltageProbeTable`). The reading is in millivolts. Probe type 16 is
  `voltageProbeTypeIsDiscrete` (the 30 "PG" power-good checks, which return no reading) and type 18 is
  `voltageProbeTypeIsPSVoltage`, so only rows 31 and 32, `PS1 Voltage 1` and `PS2 Voltage 2`, are measured: 238000 mV =
  238 V. Add a `METRICS` entry with divisor 1000 and unit `V`, restricted to rows whose type is not discrete. The power
  supply table also has the input voltage in whole volts (`4.600.12.1.16`, `powerSupplyCurrentInputVoltage`, read
  240 and 234 V at different times), which is another way to get the same figure.
- [x] **Threshold limit lines on charts.** Confirmed column order: 10 `UpperCriticalThreshold`, 11
  `UpperNonCriticalThreshold`, 12 `LowerNonCriticalThreshold`, 13 `LowerCriticalThreshold`, in the same unit as the
  reading. The inlet probe reads 470, 420, 30 and -70, so 47.0 °C, 42.0 °C, 3.0 °C and -7.0 °C. Draw them as horizontal
  lines on `line` charts, and consider highlighting readings outside them. Some probes have no thresholds, so handle
  missing values.
- [x] **`health` metric.** Two kinds of column, with different numbering:
  - *Status* columns use `other(1) unknown(2) ok(3) nonCritical(4) critical(5) nonRecoverable(6)`. They are
    `4.1100.32.1.5` (CPUs), `4.1100.50.1.5` (memory), `4.600.12.1.5` (power supplies), `4.300.70.1.5` (chassis
    intrusion), `4.600.50.1.5` (system batteries), `5.1.20.130.4.1.24` (physical disks), `5.1.20.140.1.1.20` (virtual
    disks), `5.1.20.130.15.1.6` (RAID battery), `5.1.20.130.1.1.38` (RAID controller) and `2.1` (overall system status).
    The redundancy tables `4.700.10.1.5` (fans) and `4.600.10.1.5` (power) use the same enumeration.
  - The probe status columns (temperature, voltage, amperage, fans) use a ten-value enumeration with upper and lower
    variants (`ok` is still 3; `nonCriticalUpper(4) criticalUpper(5) nonRecoverableUpper(6) nonCriticalLower(7)
    criticalLower(8) nonRecoverableLower(9) failed(10)`).
  - *State* columns are numbered differently and must not be read as status: physical disk `5.1.20.130.4.1.4`
    (`online` is 3), virtual disk `5.1.20.140.1.1.4` (`online` is 2, `degraded` 4) and RAID battery `5.1.20.130.15.1.4`
    (`ready` is 2, `charging` 6).
  Store the code over time so state changes can be charted or alerted on, and decide how to name the sensors.
- [x] **`network` metric.** Confirmed in IF-MIB: `1.3.6.1.2.1.31.1.1.1.6` is `ifHCInOctets` and `.10` is
  `ifHCOutOctets`, both 64-bit counters. The iDRAC's own interface is index 6 (`bond0`). Counters only grow, so ragdoll
  must store the difference between polls and divide by the time between them to chart a rate, and cope with a counter
  reset or wrap. This is management-port traffic only, not the server's own network ports.
- [x] **Energy and peak power.** Table `4.600.60` (`powerUsageTable`), row 1. My first reading of these columns was
  partly wrong. Confirmed meanings and units:

  | Column | Name | Unit | Seen |
  |---|---|---|---|
  | 7 | `powerUsageCumulativeWattage` | watt-hours since the start date in column 8 | 280,433 Wh (280 kWh) since 2021-07-02 |
  | 9 | `powerUsagePeakWatts` | watts | 573 W, recorded 2026-09-18 15:10:30 +10:00 |
  | 12 | `powerUsagePeakAmps` | tenths of an amp | 23, so 2.3 A |
  | 15 | `powerUsageIdlePower` | watts | 320 W (the minimum the hardware can use) |
  | 16 | `powerUsageMaxPotentialPower` | watts | 550 W |
  | 20, 21 | `powerUsageInstantaneousHeadroom`, `powerUsagePeakHeadroom` | watts | 1677 and 1227 |

  The earlier note that "peak was 320" was wrong: 320 W is the idle figure. The date columns use the CIM date-time
  form, where the `+600` suffix is the offset in minutes (+10:00). The power supply table (`4.600.12`) adds the maximum
  output (column 6, `7500` in tenths of a watt = 750 W) and rated input (column 14, `9000` = 900 W).
- [ ] **Choose amperage units from the probe type, not the name.** The MIB defines the unit by `amperageProbeType`
  (column 7): types 23 (`PowerSupplyAmps`) and 25 (`SystemAmps`) are tenths of an amp, 24 (`PowerSupplyWatts`) and 26
  (`SystemWatts`) are watts, and any other type is milliamps. ragdoll currently picks the unit from the sensor name
  (names ending in `-current` are amps). That is correct on this iDRAC, where the probes are types 23 and 26, but it
  could be wrong on other hardware.
- [ ] **System event log.** Table `4.300.40` is `eventLogTable`, the iDRAC's event log, with a text record per event
  (for example about the chassis being open or closed). It is a source of events, not readings, and could feed a
  `log` output. (I first mislabelled it as chassis intrusion; intrusion is `4.300.70`.)

### Follow-ups from 0.3.1

- [ ] Highlight readings outside the limits (for example a column in the table output), not only draw the lines.
- [ ] `--limits` for bar charts and termgraph, and limits for `health` (a flat line at 3 has no limits).
- [ ] `ps-redundancy` reads 1 (`other`) on 192.168.8.240 even with two working power supplies, so check whether the iDRAC
  there reports redundancy at all before relying on it.
- [ ] The `health` metric polls every health table to read one sensor (1.8 s). Reading only the table that holds the
  chosen sensor would be faster for cron jobs.
- [ ] Add a poll of several sensors at once, so one SNMP walk can record many sensors (for example every fan).

### What SNMP on this iDRAC does not provide

- Host CPU load, memory use and disk usage. The standard host-resources tables answer "No Such Object", so that needs
  an agent inside the operating system.
- Disk temperatures. No temperature column was found in the disk tables, only sizes and states.
- Traffic counters for the server's four network ports. The Dell tables list the ports and their status but no counters.

### Inventory seen (static, not telemetry)

PowerEdge R630, iDRAC8 firmware 2.86.86.86, Lifecycle Controller 3, BIOS dated 12/12/2023 (`systemBIOSTable`
`4.300.50` and `firmwareTable` `4.300.60`); two Intel Broadwell CPUs (`processorDeviceTable` `4.1100.30`); 16 x 32 GB
Samsung DIMMs at 2133 MT/s (`memoryDeviceTable` `4.1100.50`; `4.2000.10` is the field-replaceable unit table, `fruTable`,
which lists the same DIMMs); four Intel X520/I350 network ports (`networkDeviceTable` `4.1100.90`) and PCI devices
(`pCIDeviceTable` `4.1100.80`); 10 Toshiba SSDs of about 894 GiB behind a PERC H730P Mini (`physicalDevices`
`5.1.20.130`) with two virtual disks (`logicalDevices` `5.1.20.140`): `data` about 6.1 TiB, and `boot`. Listed by `--list inventory`
(0.3.6). Not yet covered: the field-replaceable unit table (`4.2000.10`), system slots (`4.1200.10`) and the event log.

## Redfish

The `redfish` source (0.4.1) provides the hardware inventory only. Ideas for more, from what the iDRAC8's Redfish
offered on 2026-10-06:

- [ ] Redfish metrics under `--source redfish`: temperatures and fans from `/Chassis/System.Embedded.1/Thermal`, and
  power from `.../Power`, as an alternative where SNMP is off. Each Redfish request is slow without a session (5 to 9 s),
  so a poll would cost the 10 s login every time; a longer-lived session, or a cache of the token, would help cron jobs.
- [ ] Speed up the full inventory (43 s, 10 of them the login). The iDRAC seems to answer requests one at a time, so more
  parallelism may not help; fetching expanded resources (`?$expand=*`) could cut the number of requests.
- [ ] PCI devices over Redfish are named by bus position (`0-3`) and there are 12 of them against 23 over SNMP; the
  `PCIeFunctions` resources (16) carry vendor and device IDs that could name them better.
- [ ] The RAID battery is not in the standard Redfish storage resources; look for it in the Dell OEM resources.
- [ ] A Redfish volume reports `StripedWithParity`, which is RAID 5 or RAID 6; the Dell OEM data may say which.

## Polling

- [ ] `--tail` follows one sensor. Reading a whole table already costs one SNMP walk, so one poll could record every
  sensor of a metric (for example all 14 fans) in the same pass.
- [ ] A ready-made systemd unit and an example for running `--tail --output db` as a service.
- [ ] Rotate or prune old rows from the database (it only ever grows).
- [ ] Optionally also add `--tail` readings to the CSV cache, so `--no-fetch` without `--db` can see them.

## lm-sensors

- [ ] Use the `PySensors` module (it wraps libsensors) instead of running `sensors -j`, which would drop the dependency on
  the command and its output format. Needs a decision on whether to add a pip dependency that itself needs libsensors.
- [ ] Other local sources of telemetry that lm-sensors lacks: battery state and charge from `/sys/class/power_supply`,
  disk SMART temperatures, and network counters from `/proc/net/dev` (which would be a rate like the iDRAC `network`
  metric). lm-sensors also has humidity and energy readings, which are not mapped to a metric.
- [ ] Fan, voltage and power sensors were not tested against real hardware, because the test computer has only
  temperature sensors; the parser handles them from the lm-sensors JSON format (`fan1_input`, `in0_input`,
  `power1_input` or `_average`, `curr1_input`).
- [x] Over ssh, reuse one connection for `--tail`. Done in 0.4.5 with paramiko.
- [ ] Let an identity file or port be given on the command line (`--ssh-option`?) rather than only through
  `~/.ssh/config`, which paramiko now reads.
- [ ] Apply `ProxyJump` through paramiko (it is not read from `~/.ssh/config` automatically) so those hosts do not need the
  `ssh` command fallback and keep the reused connection.
- [ ] Test fan and voltage sensors over ssh; the remote test machine had temperatures and an ACPI power meter only.

## racadm

- [ ] racadm can also give telemetry (`racadm getsensorinfo`, `racadm sensorstats`) and the power supply and fan blocks
  that `hwinventory` already returns (`PowerSupply`, `Fan`, `Enclosure`); none of that is used yet. A racadm metrics
  source would be an alternative where SNMP is off.
- [ ] `hwinventory` takes about 25 s and only `nic` filtering is supported by the iDRAC, so a single category still costs the
  full run. Cache the parsed inventory for a short time so repeated `--category` lookups are quick.
- [ ] The local `racadm -r ... -p` fallback shows the password in the process list. Dell's tool has no other way to take
  it, so it stays a last resort.
- [ ] Put the same per-iDRAC lock around Redfish and the web login, which also create sessions on an iDRAC (Redfish already
  uses one session per run, with four requests at a time inside it). It was added for racadm only.

### Python modules for racadm (looked at 2026-10-06)

- There is no Python module that wraps racadm. The PyPI package `racadm` (0.0.1, "A Python package for racadm CLI") is an
  empty placeholder: its wheel holds only metadata and no code. Other likely names (`pyracadm`, `python-racadm`, `py-racadm`,
  `dracclient`, `dell-racadm`) do not exist.
- The ssh route that ragdoll already uses needs no racadm on this computer, so a module is not needed for that.
- `python-dracclient` 8.0.0 (OpenStack, Apache-2, needs `lxml`, `requests`, `pbr`) talks WS-Man over HTTPS to the iDRAC and has
  `list_cpus`, `list_memory`, `list_nics`, `list_physical_disks`, `list_virtual_disks`, `list_raid_controllers`, `get_system`
  and `list_bios_settings`. It would be a third way to read the inventory for an iDRAC with ssh turned off, but it is a
  different protocol and not racadm, and it returns less (no firmware list). It installs on this Python (dry run).
- `omsdk` 1.2.519 (Dell's OpenManage Python SDK, Apache) covers WS-Man, Redfish and SNMP but is heavy and old
  (`pysnmp_mibs`, `future`), so it would likely clash with the current `pysnmp`.
- [x] Add `python-dracclient` as its own `wsman` source (done in 0.4.9). Not used as a racadm fallback: it is a different
  protocol and returns less.

## wsman

- [ ] python-dracclient has more than the inventory uses: `list_bios_settings` (BIOS attributes, not a version),
  `list_idrac_settings`, `list_lifecycle_settings`, `list_system_settings`, `list_boot_devices` and `list_jobs`. None are mapped.
- [ ] Its disks and memory modules carry no serial numbers here (a SATA SSD behind the PERC reports none, and memory has only
  the manufacturer), unlike SNMP and Redfish for memory.
- [ ] The module's own `ready_retries` waits (up to 16 minutes by default) are turned down to one attempt; if an iDRAC's
  Lifecycle Controller is busy, a call fails at once instead of waiting. Consider an option to wait.
