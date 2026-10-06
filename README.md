![ragdoll](ragdoll.jpg)

# ragdoll

Redfish/API/GUI/DRAC/Other Log Linter - Converts iDRAC Telemetry and other information into more useful formats.

ragdoll reads telemetry from a Dell iDRAC and turns it into something you can use: a chart in the terminal, a table,
CSV, or an Excel spreadsheet. It currently reads temperatures, fan speeds, power, voltage, component health and
management-network traffic, through the iDRAC's web interface, SNMP or Redfish. It can also read the computer it
is running on, through lm-sensors.

- [Install](#install) and [quick start](#quick-start)
- [Sources and metrics](#sources-and-metrics): what can be read, and the SNMP-only extras
- [Listing sensors](#listing-sensors) and [getting a current value](#getting-a-current-value)
- [Charts](#charts) and [other outputs](#other-outputs): table, CSV, spreadsheets
- [Time zones](#time-zones)
- [Storage](#storage): cache, SQLite database, offline use
- [This computer's sensors](#this-computers-sensors-lm-sensors), [credentials](#credentials), [running regularly](#running-regularly) and [polling](#polling-with---tail)
- [Option reference](#option-reference), [example output](#example-output) and [notes](#notes)

## Install

```
pip install -r requirements.txt
```

This installs `requests` (web source), `pysnmp` (snmp source), `paramiko` (ssh, for `--source lmsensors --host` and `--source racadm`), `python-dracclient` and `six` (`--source wsman`; python-dracclient imports `six` without listing it), `keyring` (saved credentials), `terminaltables` (table
output), `XlsxWriter` and `xlwt` (`.xlsx` and `.xls` output), and the two graphing modules, `plotext` and `termgraph`.
`plotext` is pinned to 5.3.2 because 6.x has a different API.

The `lmsensors` source also needs the `lm-sensors` package, which is a system package and not a Python one (for example
`sudo apt install lm-sensors`, then `sudo sensors-detect` once to find the hardware). Nothing else needs it.

ragdoll is a single script, `ragdoll.py`. It was written and tested with Python 3.14 and uses the standard `zoneinfo`
module, so it needs at least Python 3.9; older versions have not been tried.

## Quick start

```
# what can this iDRAC tell me? (web credentials are only needed for the web source)
python3 ragdoll.py --host 192.0.2.20 --list

# the current inlet temperature, read over SNMP
python3 ragdoll.py --host 192.0.2.20 --get

# a line chart of the last day of inlet temperature from the web interface
python3 ragdoll.py --host 192.0.2.10 --user <user> --pass <password> --last day --chart line
```

Credentials do not have to be typed each time: see [Credentials](#credentials).

## Sources and metrics

ragdoll reads from six sources, chosen with `--source` (`gui` is accepted as another name for `web`):

| Source | Metrics | How it works | History |
|---|---|---|---|
| `web` (default; also `gui`) | temperature | Logs in to the iDRAC web interface and downloads the temperature statistics CSV | Hourly history held by the iDRAC |
| `lmsensors` | temperature, fan, power, voltage | Reads the sensors of the computer ragdoll runs on, through `sensors -j` from lm-sensors. No `--host` or login needed | Only the current value; each run appends a reading to the local cache, as with snmp |
| `wsman` | none: the [hardware inventory](#hardware-inventory) only | Reads part of the inventory over WS-Man (HTTPS) with the `python-dracclient` module: no ssh and no racadm | The inventory is a snapshot, so there is no history |
| `racadm` | none: the [hardware inventory](#hardware-inventory) only | Runs Dell's `racadm` commands (`hwinventory`, `swinventory`, `getsysinfo`) in the iDRAC's ssh shell | The inventory is a snapshot, so there is no history |
| `redfish` | none: the [hardware inventory](#hardware-inventory) only | Reads the inventory from the iDRAC's Redfish API over HTTPS | The inventory is a snapshot, so there is no history |
| `snmp` | temperature, fan, power, voltage, health, network | Reads the current value from the Dell probe tables (and the standard interface counters) over SNMP v2c | Only the current value; each run appends a reading to the local cache, so history builds up over time |

`--metric` chooses what to read (default `temperature`) and `--sensor` picks one sensor of that metric. Every metric
except `temperature` needs `--source snmp`. The sensors the iDRAC offers are shown by [`--list`](#listing-sensors);
unknown names are rejected with the ones that exist.

| Metric | Unit | Default sensor | Example sensors (snmp) |
|---|---|---|---|
| `temperature` | °C | `inlet` | `inlet`, `exhaust`, `cpu1`, `cpu2` (the web source offers `inlet` only) |
| `fan` | RPM | `fan1a` | `fan1a`, `fan1b`, `fan2a` ... `fan7b` |
| `power` | W, A or Wh | `system-power` | `system-power`, `ps1-current`, `ps2-current`, `energy`, `peak-power`, `idle-power` ... |
| `voltage` | V | `ps1-voltage` | `ps1-voltage`, `ps2-voltage` |
| `health` | status code | `system` | `system`, `cpu1`, `dimm-a1`, `ps1`, `disk-0`, `vdisk-data`, `raid-battery` ... (41 on the test server) |
| `network` | bytes per second | `bond0-in` | `bond0-in`, `bond0-out` |

All the SNMP details below were checked against Dell's iDRAC MIB.

### Power: energy and peaks

Besides the supply currents (`ps1-current`, `ps2-current`, in amps) and `system-power` (watts), `power` reads the power
usage table as extra sensors: `energy` (cumulative watt-hours since the date the iDRAC began counting), `peak-power`
(watts) and `peak-current` (amps), the highest values seen, `idle-power` (the least the hardware can use),
`max-power` (the most it can use), and `headroom` and `peak-headroom` (watts left under the power supply's limit, now
and at the peak).

### Voltage

`voltage` reads the power supply input voltages (`ps1-voltage`, `ps2-voltage`). The iDRAC also has about 30 on/off
power-good checks that return no number; they are not listed.

### Health

`health` records a status code per component, so changes can be charted or noticed over time: the whole system
(`system`), CPUs, each memory module (`dimm-a1` ...), power supplies, chassis intrusion, batteries, the RAID
controller and its battery, each disk (`disk-0` ...) and virtual disk (`vdisk-data`), and the fan and power
redundancy (`fan-redundancy`, `ps-redundancy`).

| Code | Component status | Redundancy (`fan-redundancy`, `ps-redundancy`) |
|---|---|---|
| 1 | other | other |
| 2 | unknown | unknown |
| 3 | **ok** | **full** |
| 4 | non-critical | degraded |
| 5 | critical | lost |
| 6 | non-recoverable | not redundant |
| 7 | | redundancy offline |

A chart of a healthy component is a flat line at 3. The iDRAC's disk, virtual disk and battery *state* values use
different numbering and are not used; the status of each of those is.

### Network traffic

`network` reads the byte counters of the iDRAC's own network interfaces (`bond0-in`, `bond0-out`; loopback is left
out). A counter only ever grows, so ragdoll stores the counter and shows the **rate in bytes per second** between
consecutive readings. That needs at least two readings, so the first run explains what to do, and later runs (or a
cron job) fill it in. A reading lower than the one before, which happens when the iDRAC restarts, is skipped. This is
traffic on the management port, not on the server's own network ports. The `--raw` flag shows the stored counter.

### Limits

Temperature, fan and some power probes have warning and critical limits. Every poll over SNMP saves them next to the
cache. They are shown by `--list`, and `--limits` draws them on a [chart](#charts). The limits belong to the host and
sensor, not the source, so a chart from the web source of the same sensor can use limits saved by an SNMP poll.

## Listing sensors

`--list` prints every sensor of every source and metric (`--list sensors` is the same thing, spelt out). The credentials
of a source are used if you have them; a source that cannot be reached or needs credentials you haven't given is
reported on stderr and skipped. A source or metric after `--list` narrows it (`--list snmp`, `--list fan`), and so do
`--source` and `--metric`.

```
$ python3 ragdoll.py --host 192.0.2.20 --list temperature
SOURCE  METRIC       SENSOR   UNIT  VALUE  LIMITS                  KEY
snmp    temperature  cpu1     °C       23  warn 8..82 crit 3..87   1.3.6.1.4.1.674.10892.5.4.700.20.1.6.1.3
snmp    temperature  cpu2     °C       26  warn 8..82 crit 3..87   1.3.6.1.4.1.674.10892.5.4.700.20.1.6.1.4
snmp    temperature  exhaust  °C       26  warn 0..70 crit 0..75   1.3.6.1.4.1.674.10892.5.4.700.20.1.6.1.2
snmp    temperature  inlet    °C       16  warn 3..42 crit -7..47  1.3.6.1.4.1.674.10892.5.4.700.20.1.6.1.1
web     temperature  inlet    °C       14  warn 3..42 crit -7..47  iDRAC.Embedded.1#Inlet.1#ThermalHistory
```

- **UNIT** is what the values are measured in: `°C`, `RPM`, `V`, `W`, `A`, `Wh`, `B/s`, or `code` for a `health`
  status.
- **VALUE** is the current reading from SNMP. The web source cannot give one without fetching its whole history, so
  its row shows the newest reading already in the cache (or `-` if nothing is cached). `network` counters show `-`,
  because they only mean something as a rate between two readings.
- **LIMITS** are `lower..upper` pairs of warning and critical limits, with `-` where a limit is missing (a fan has only
  lower limits). Sensors without limits show `-`. The web row uses limits saved by an earlier SNMP poll of the same
  sensor.
- **KEY** is the iDRAC's own identifier for the sensor.

`--output` changes the format, so the listing can be exported. The default is plain `text`, as above.

| `--output` | What you get |
|---|---|
| `text` | the aligned plain text above (the default) |
| `table` | a bordered table, using `terminaltables` |
| `csv` | CSV, one row per sensor |
| `json` | a JSON array, one object per sensor (see [JSON](#json)) |
| `xlsx`, `xls` | a spreadsheet with one `Sensors` sheet, named with `--file` or, without it, `<host>_sensors[_<source>][_<metric>]` in the current directory |

`chart` and `raw` are for readings and are rejected with `--list`, and `--output text` is only for `--list`.

CSV and the spreadsheets are meant for other programs, so they keep numbers as numbers and give the four limits columns
of their own (`lower_critical`, `lower_warning`, `upper_warning`, `upper_critical`) instead of the `warn ..` text.
A missing value or limit is an empty cell.

```
$ python3 ragdoll.py --host 192.0.2.20 --list temperature --output csv
source,metric,sensor,unit,value,lower_critical,lower_warning,upper_warning,upper_critical,key
snmp,temperature,cpu1,°C,23,3,8,82,87,1.3.6.1.4.1.674.10892.5.4.700.20.1.6.1.3
snmp,temperature,cpu2,°C,26,3,8,82,87,1.3.6.1.4.1.674.10892.5.4.700.20.1.6.1.4
snmp,temperature,exhaust,°C,26,0,0,70,75,1.3.6.1.4.1.674.10892.5.4.700.20.1.6.1.2
snmp,temperature,inlet,°C,16,-7,3,42,47,1.3.6.1.4.1.674.10892.5.4.700.20.1.6.1.1
web,temperature,inlet,°C,14,-7,3,42,47,iDRAC.Embedded.1#Inlet.1#ThermalHistory

$ python3 ragdoll.py --host 192.0.2.20 --source snmp --metric fan --list --output xlsx
Wrote 14 sensors to 192.0.2.20_sensors_snmp_fan.xlsx
```

### Hardware inventory

`--list inventory` lists what the server is made of, over SNMP. Unlike a sensor, none of it changes between polls, so it
is a snapshot rather than a metric. It needs `--source snmp` (the default for this listing); `--source web` and
`--metric` are rejected.

| Category | What it shows |
|---|---|
| `system`, `idrac` | the server's model, name and service tag; the iDRAC's product, firmware and manufacturer |
| `bios`, `firmware` | BIOS version and date; firmware versions (the iDRAC, the Lifecycle Controller) |
| `cpu` | manufacturer, brand, model, cores, threads, maximum and current speed, status |
| `memory` | each module's size, speed, manufacturer, part number, serial number and status |
| `nic`, `pci` | network ports (product, vendor, MAC address, link state) and PCI devices |
| `controller`, `disk`, `virtual-disk`, `raid-battery` | the storage: controller firmware and cache; each disk's model, serial number, firmware, size, bus, media, state and status; each virtual disk's size, RAID layout and state; the RAID battery |

Status values are `ok`, `non-critical`, `critical` and so on, as in the health metric. Sizes are in GiB and speeds in
MHz. Items are in category order and, within a category, in natural order (`DIMM.Socket.A2` before `A10`).

```
$ python3 ragdoll.py --host 192.0.2.20 --list inventory
CATEGORY      NAME                                     DETAILS
system        system                                   model=PowerEdge R630; name=r630xp1; service-tag=XXXXXXX
idrac         idrac                                    product=iDRAC8; firmware=2.86.86.86; manufacturer=Dell Inc.
bios          bios                                     version=2.19.0; released=12/12/2023; manufacturer=Dell Inc.; status=ok
firmware      iDRAC8                                   version=2.86.86.86; status=ok
firmware      Lifecycle Controller 3                   version=2.86.86.86; status=ok
memory        DIMM.Socket.A1                           size=32 GiB; speed=2133 MHz; manufacturer=Samsung; part-number=M386A4G40DM0-CPB; serial=XXXXXXXX; status=ok
controller    PCIe Extender 1 (PCI Slot 1)             firmware=1; status=ok
controller    PERC H730P Mini (Embedded)               firmware=25.5.9.0001; cache=2048 MB; status=ok
disk          Solid State Disk 0:1:0                   manufacturer=ATA; model=TOSHIBA THNSN89; firmware=8EET6101; size=893.75 GiB; bus=sata; media=ssd; state=online; status=ok
virtual-disk  boot                                     size=893.75 GiB; layout=RAID 1; media=SSD; state=online; status=ok
virtual-disk  data                                     size=6256.25 GiB; layout=RAID 5; media=SSD; state=online; status=ok
raid-battery  Battery on Integrated RAID Controller 1  state=ready; status=ok
```

(Only some of the 66 rows are shown. The service tag, serial numbers and MAC addresses are replaced here with
`XXXX`; the real listing includes them, so take care when sharing it.)

`--get inventory` produces exactly the same listing as `--list inventory`, in every format.

**Over Redfish.** The inventory can also be read from the iDRAC's Redfish API, which is useful where SNMP is turned off
or blocked. Use `--source redfish` (or `--source web`, the iDRAC's web interface, which is where Redfish lives); the
default for the inventory stays `snmp`. It needs the web credentials (`--user` and `--pass`, or the keyring) instead
of the SNMP community.

```
python3 ragdoll.py --host 192.0.2.20 --source redfish --get inventory --category memory --name DIMM.Socket.A1
```

Everything else works the same: `--category`, `--name`, `--detail`, `--field` and every `--output`. The two sources
describe the hardware a little differently:

| | `snmp` | `redfish` |
|---|---|---|
| `firmware` | 2 items (the iDRAC and Lifecycle Controller) | every installed firmware: BIOS aside, 11 items on the test server (PERC, backplane, NIC, power supply, CPLD ...) |
| `controller` | 2 | 4, including the SATA controllers |
| `pci` | 23 devices, named like `Video.Embedded.1-1` | 12 devices, named by bus position like `0-3` |
| `raid-battery` | yes | not available over Redfish |
| virtual disk `layout` | `RAID 5` | `RAID 5 or RAID 6`, because Redfish reports only "striped with parity" |
| other differences | CPU `model`, `enabled-cores`, current `speed`; NIC `product`, `link`; disk `state` | `system` `power` and `manufacturer`; memory `type`; NIC `speed` and `description` |

The names of the memory modules (`DIMM.Socket.A1`), network ports, processors, disks and virtual disks are the same in
both, so the same `--name` works with either source.

**Over racadm.** `--source racadm` gets the same inventory from Dell's `racadm` commands, which on an iDRAC8 is the
most complete source: every category, with exact RAID layouts (`RAID 5`, not `RAID 5 or RAID 6`), all 15 installed
firmware items and the four storage controllers.

```
python3 ragdoll.py --host 192.0.2.20 --source racadm --get inventory --category memory --name DIMM.Socket.A1
```
```
CATEGORY  NAME            DETAILS
memory    DIMM.Socket.A1  size=32 GiB; speed=2133 MHz; type=DDR-4; manufacturer=Samsung; part-number=M386A4G40DM0-CPB; serial=XXXXXXXX; status=ok
```

- **No racadm needed on this computer.** The commands run on the iDRAC itself, in its ssh shell, so a computer without
  Dell's `racadm` installed works as well (tested with `racadm` removed from the search path). There is no Python module that
  wraps racadm: the one called `racadm` on PyPI is an empty placeholder.
- **How it connects:** ragdoll logs in to the iDRAC's own ssh shell with paramiko, using the web credentials (`--user` and
  `--pass`, the keyring or the environment), and runs `racadm getsysinfo -s`, `racadm swinventory` and
  `racadm hwinventory` there. The password is sent over the ssh connection, so it never appears in a process list, and
  the iDRAC's host key is checked against `~/.ssh/known_hosts` like any other (ssh to the iDRAC once first). The iDRAC's
  ssh service must be enabled, which it is by default.
- **Fallback:** if ssh to the iDRAC cannot be reached at all (or paramiko is not installed) and `racadm` is installed on
  this computer, ragdoll runs `racadm -r <host> -u <user> -p <password> --nocertwarn ...` instead. That works, but the
  password is then visible to other users in the process list while it runs. A refused login is not retried this way.
- **Never in parallel:** an iDRAC allows few ssh and racadm sessions and can lock an account out, so ragdoll runs one
  command at a time over a single ssh login, and never two ragdoll processes at once on the same iDRAC. A second ragdoll
  (a cron job overlapping a manual run, say) waits for the first, using a lock file per host in the cache directory
  (`<host>.idrac.lock`, shared with the `wsman` source), and carries on when it finishes; it gives up with a message after ten minutes. The lock is
  released if the first process crashes. One login is made per run, and a refused login is never retried. If the
  iDRAC says it has no free racadm sessions (someone else is using them), ragdoll waits a few seconds and tries again,
  twice at most.
- **Speed:** each command takes 10 seconds or more on an iDRAC8, and `hwinventory` about 25, so the whole inventory takes
  about 40 seconds and `--category system` or `--category bios` about 11. A category such as `memory` needs the full
  `hwinventory`, about 25 seconds. Running the commands in parallel would not be faster on the test iDRAC (40 seconds
  either way), so nothing is lost by running them one after another.
- **Names** match the other sources for memory modules (`DIMM.Socket.A1`), network ports, processors and PCI devices
  (23, as with SNMP). Disks are named by their full location (`Disk.Bay.0:Enclosure.Internal.0-1:RAID.Integrated.1-1`)
  and controllers by their location (`RAID.Integrated.1-1`), and the firmware items include the network adapters, whose
  names contain their MAC addresses.
- It provides the inventory only, like `redfish`: `--source racadm` with a chart, `--list` of sensors, `--get` of a
  sensor or `--tail` is rejected with a message pointing to the inventory.

**Over WS-Man.** `--source wsman` reads the inventory over WS-Man (the SOAP-over-HTTPS interface that the iDRAC's own
tools use) with the `python-dracclient` module, so it needs neither ssh nor racadm. It is the quickest of the inventory
sources, and one category takes only a few seconds, but it covers less.

```
python3 ragdoll.py --host 192.0.2.20 --source wsman --get inventory --category cpu
```

- **Coverage:** `system` (model, service tag, power state), `cpu`, `memory`, `nic`, `controller`, `disk` and `virtual-disk`
  match the other sources. `pci` has only the video controller, and `firmware` has only the Lifecycle Controller version. There
  is no `bios`, `idrac` or `raid-battery`, because the module does not return them. Use `snmp`, `redfish` or `racadm` for those.
- **Speed:** 31 seconds for everything on the test iDRAC, and 2 to 6 seconds for one category.
- **Credentials:** the web credentials. A refused login stops after one attempt. Requests are made one at a time, under the
  same per-iDRAC lock as racadm.
- **TLS:** the module hard-codes "do not verify certificates" and has no timeout. ragdoll replaces both, so `--secure` works
  (it fails on a self-signed certificate, as you would expect) and a request is given up after 60 seconds.
- **Install:** `pip install python-dracclient six`. The module imports `six` without declaring it, so it must be installed
  as well. Without the module the source says so and how to install it.

**Redfish is slow on an iDRAC8.** With a password, every request takes 5 to 9 seconds, because the iDRAC checks it
each time. ragdoll therefore logs in once with a session token (about 10 seconds), after which each request takes under
a second, and logs out at the end; it makes a few requests at a time. A full inventory took 43 seconds, and one
category such as `--category cpu` about 12. SNMP takes 5 seconds for everything.

**Choosing part of the inventory.** `--category` limits it to one category (`system`, `idrac`, `bios`, `firmware`,
`cpu`, `memory`, `nic`, `pci`, `controller`, `disk`, `virtual-disk` or `raid-battery`), and `--name` limits it to the
items with that name. They can be used together, and they work with `--list inventory` and `--get inventory`:

```
$ python3 ragdoll.py --host 192.0.2.20 --get inventory --category memory --name DIMM.Socket.A1
CATEGORY  NAME            DETAILS
memory    DIMM.Socket.A1  size=32 GiB; speed=2133 MHz; manufacturer=Samsung; part-number=M386A4G40DM0-CPB; serial=XXXXXXXX; status=ok
```

- `--category memory` alone lists every memory module. Only that category's tables are read, so it is quicker than the
  whole inventory (2 seconds against 5 on the test server).
- `--name` is not case-sensitive, and `*` and `?` match any characters: `--name 'DIMM.Socket.B*'` is every module in
  bank B. Without `--category` it looks in every category, so `--name NIC.Integrated.1-1-1` finds both the network port
  and the PCI device of that name.
- A name that matches nothing is rejected with the names that do exist. An unknown category is rejected with the valid
  ones. Both flags are rejected without `--list inventory` or `--get inventory`.
- A spreadsheet is named after the filters: `<host>_inventory_memory.xlsx`,
  `<host>_inventory_memory_DIMM.Socket.A1.xlsx` (a `*` in the name becomes `_`).

**Getting a single detail.** Each item's details are `attribute=value` pairs. `--detail` returns just the value of one
attribute, for example the BIOS version:

```
$ python3 ragdoll.py --host 192.0.2.20 --get inventory --name bios --detail version
2.19.0
$ python3 ragdoll.py --host 192.0.2.20 --get inventory --category virtual-disk --detail layout
RAID 1
RAID 5
$ python3 ragdoll.py --host 192.0.2.20 --get inventory --category disk --detail firmware | sort | uniq -c
      9 8EET6101
      1 8EET6103
```

- The attribute name is not case-sensitive. The attributes of each category are the names shown in the details, for
  example `version`, `released`, `manufacturer` and `status` for the BIOS, or `size`, `speed`, `part-number`, `serial`
  and `status` for memory.
- In plain text the values are bare, one per line with no heading, so they can go into a script:
  `bios=$(python3 ragdoll.py --host 192.0.2.20 --get inventory --name bios --detail version)`. With several items the
  names are not printed; add `--output table` to see them (`CATEGORY`, `NAME` and the detail as its own column), or
  `--output csv`, `xlsx` or `xls`, which give the usual `category,name,attribute,value` rows for just that attribute.
- Items that do not have the detail are left out (`--detail size` skips the CPUs, for instance). If no item has it,
  the error lists the details the items do have.
- `--detail` cannot be combined with `--field`, which picks a column of the output instead.

`--output` works as for sensors, with one difference: `text` and `table` show one row per item, with its details
joined as `attribute=value`, while `csv`, `xlsx` and `xls` are tidy, one row per attribute (`category`, `name`,
`attribute`, `value`), which suits filtering and pivot tables. A spreadsheet is named `<host>_inventory.xlsx` (or
`.xls`) unless `--file` is given.

## Getting a current value

`--get` polls one SNMP sensor now and prints its current value and unit, which is handy for a quick look or a script.
It uses `--source snmp` unless told otherwise, and `--metric` and `--sensor` choose the sensor (the defaults are
`temperature` and `inlet`, or the default sensor of the metric).

```
$ python3 ragdoll.py --host 192.0.2.20 --get
16 °C
$ python3 ragdoll.py --host 192.0.2.20 --metric fan --sensor fan1a --get
3840 RPM
$ python3 ragdoll.py --host 192.0.2.20 --metric power --sensor system-power --get
112 W
$ python3 ragdoll.py --host 192.0.2.20 --metric health --sensor system --get
3 code
```

- **Nothing is stored.** `--get` does not touch the cache or a database, even with `--db`. To record readings, poll
  the sensor normally.
- **In a script:** `--field value` prints just the number (see [Picking one field](#picking-one-field)):
  `temp=$(python3 ragdoll.py --host 192.0.2.20 --get --field value)`. Without it the output is the number, a space and
  the unit. A failure prints a message on stderr and exits with status 1.
- **Counters:** a network counter has no value of its own, so `--metric network --get` reads it twice, 2 seconds apart,
  and prints the rate in bytes per second (`2184.523 B/s`).
- **Other formats:** `--output table|csv|xlsx|xls` shows the same one sensor as a row, with its unit, limits and key, as
  `--list` would; `--file` names a spreadsheet. `--output text` is the default.
- **Only SNMP:** the web source cannot give a live value without fetching its whole history, so `--source web` is
  rejected. `--get` cannot be combined with `--list` or `--no-fetch`.
- **Inventory:** `--get inventory` prints the [hardware inventory](#hardware-inventory), the same as
  `--list inventory`. It takes no `--metric` or `--sensor`, and any other word after `--get` is rejected.

## Picking one field

`--field` returns just one field (column) of what `--list` or `--get` would print. It is not case-sensitive.

```
$ python3 ragdoll.py --host 192.0.2.20 --get inventory --category system --field details
model=PowerEdge R630; name=r630xp1; service-tag=XXXXXXX
$ python3 ragdoll.py --host 192.0.2.20 --get --field value
16
$ python3 ragdoll.py --host 192.0.2.20 --list fan --field sensor
fan1a
fan1b
fan2a
...
```

- In plain text, a single field is printed as bare values, one per line, with no heading, so it can go straight into a
  script or a pipe. In `table`, `csv`, `xlsx` and `xls` the one column keeps its heading.
- The fields are the columns of the chosen `--output`, so they differ a little between formats. A wrong name is
  rejected with the fields that exist.

| | `text` and `table` | `csv`, `xlsx` and `xls` |
|---|---|---|
| Sensors (`--list`, `--get`) | `source`, `metric`, `sensor`, `unit`, `value`, `limits`, `key` | `source`, `metric`, `sensor`, `unit`, `value`, `lower_critical`, `lower_warning`, `upper_warning`, `upper_critical`, `key` |
| Inventory | `category`, `name`, `details` | `category`, `name`, `attribute`, `value` |

For example `--get --field unit` gives `°C`, `--get --field limits` gives `warn 3..42 crit -7..47`, and
`--get --field key` gives the sensor's OID. `--field` only applies with `--list` or `--get`.

## Charts

`--output chart` is the default. A chart shows the selected readings, and the title names the host, the sensor and the
time zone.

- `--chart vertical|horizontal|stacked|histogram|line|scatter` (default `vertical`). `line` and `scatter` need plotext.
  A line chart suits trends best because its scale fits the data; bar charts start at zero, so a small change between
  readings is hard to see.
- `--module plotext|termgraph` (default `plotext`). termgraph has no `line` or `scatter` chart, ignores `--height`, and
  its `vertical` chart ignores `--width`.
- `--width` (default 80) and `--height` (default 24) fit a standard terminal. One row is left free for the shell prompt.
  plotext never draws wider than the terminal.
- `--last` is how much to show: a row count (the default is `10`; `0` is everything) or a period such as `hour`, `day`,
  `week`, `month`, `year`, `6h`, `2d` or `3 weeks`. A period is counted back from the newest reading in the data, not
  from the local clock, and `month` is 30 days and `year` 365.
- `--limits` draws the sensor's warning and critical limits on a plotext `line` or `scatter` chart, named in the legend.
  The chart's scale widens to include them, so a reading well inside the limits looks flat. If no limits are saved
  (poll the sensor over SNMP first), or the chart is not a plotext line or scatter chart, it says so on stderr.

```
python3 ragdoll.py --host 192.0.2.20 --source snmp --sensor inlet --last day --chart line --limits
```

## Other outputs

`--output` chooses what is produced from the selected readings (`--last` chooses how many; the default is 10 rows).
`--module`, `--chart`, `--width` and `--height` apply to `chart` only.

| `--output` | What you get |
|---|---|
| `chart` | a terminal chart (the default) |
| `table` | a table, oldest first |
| `csv` | CSV for spreadsheets, databases and scripts |
| `json` | a JSON array, one object per reading |
| `raw` | CSV in the form ragdoll stores |
| `xlsx`, `xls` | a spreadsheet, named with `--file` |

### Table

`--output table` uses the plain ASCII style of `terminaltables`, so it also works when piped or redirected. The time
column is in local time, or the zone given with `--tz` or `--utc`, and the zone name is in the header.

### CSV

- **`--output csv`** gives each row everything that identifies it, so files from different hosts or sensors can be
  joined: `time,host,source,metric,sensor,average,peak,unit`. The time is ISO 8601 with its UTC offset, in local time
  or the zone from `--tz` or `--utc`.
- **`--output raw`** is the form ragdoll itself stores: `Average,Peak,Time`, with UTC times ending in `Z`. It is what
  is kept in the cache.

`--output raw` is not the same as the older `--raw` flag. `--raw` prints every stored reading, ignores `--last`, and
exits before any other output is chosen; it is kept so existing scripts and cron jobs keep working. `--output raw`
prints only the selected readings.

### JSON

`--output json` prints JSON, for scripts and programs that would rather not parse CSV. It works with readings, with
`--list` and `--get`, and with `--tail`. Numbers are JSON numbers, a missing value is `null`, and strings are not
escaped (`"°C"` stays `"°C"`). Times are ISO 8601 with their UTC offset, in local time or the zone from `--tz` or `--utc`,
as in the CSV.

```
$ python3 ragdoll.py --host 192.0.2.20 --source gui --last 2 --output json
[
  {
    "time": "2026-10-06T17:00:30+11:00",
    "host": "192.0.2.20",
    "source": "web",
    "metric": "temperature",
    "sensor": "inlet",
    "average": 16,
    "peak": 16,
    "unit": "°C"
  },
  ...
]
```

- **Readings** are an array of objects with the same fields as `--output csv`.
- **`--list`** is an array of objects with the fields of the text listing (`source`, `metric`, `sensor`, `unit`, `value`,
  `limits`, `key`), where `limits` is an object with `lower_critical`, `lower_warning`, `upper_warning` and
  `upper_critical`, or `null` for a sensor without limits.
- **`--get`** is a single object, not an array. `--field` keeps one key: `--get --field value --output json` prints
  `{"value": 15}`.
- **The inventory** is an array of `{"category", "name", "details"}`, where `details` is an object of attribute and value.
  The values are strings, as in the text listing (`"32 GiB"`, `"16"`), because they carry units and a serial number can
  look like a number. `--detail` leaves just that attribute in `details`, and `--field details` keeps only that key.
- **`--tail`** prints [JSON Lines](https://jsonlines.org): one compact object per line, one per poll, so it can be read as
  it arrives (`... --tail --output json | jq .average`).

```
$ python3 ragdoll.py --host 192.0.2.20 --get --output json
{
  "source": "snmp",
  "metric": "temperature",
  "sensor": "inlet",
  "unit": "°C",
  "value": 15,
  "limits": {
    "lower_critical": -7,
    "lower_warning": 3,
    "upper_warning": 42,
    "upper_critical": 47
  },
  "key": "1.3.6.1.4.1.674.10892.5.4.700.20.1.6.1.1"
}
```

### Spreadsheets

`--output xlsx` writes an Excel workbook, and `--output xls` writes the older Excel 97-2003 format.

```
$ python3 ragdoll.py --host 192.0.2.20 --source gui --last week --output xlsx
Wrote 104 readings to 192.0.2.20_web_temperature-inlet_last-1w.xlsx

$ python3 ragdoll.py --host 192.0.2.20 --source gui --last 3 --output xls --file inlet.xls
Wrote 3 readings to inlet.xls
```

- **File name:** `--file report` becomes `report.xlsx` (or `.xls`) if it has no extension. A name ending in the other
  spreadsheet extension is rejected, and `--file` with any other `--output` is an error. An existing file is
  overwritten.
- **Automatic name:** without `--file`, `<host>_<source>_<metric>-<sensor>_last-<period>.<ext>` in the current
  directory, where the period describes `--last`: `last-10rows`, `last-all` (for `--last 0`), `last-2h`, `last-1d`,
  `last-1w`, `last-1mo` or `last-1y`. The metric and sensor are included so different sensors do not overwrite each
  other. Add `*.xlsx` and `*.xls` to `.gitignore` if you run it inside a repository.
- **Contents:** one sheet, `Readings`, with a bold heading row (frozen in `.xlsx`) and four columns: `Time (<zone>)`
  in the zone from `--tz`, `--utc` or local time, `Time (UTC)`, `Average (<unit>)` and `Peak (<unit>)`. Times are real
  Excel dates formatted `yyyy-mm-dd hh:mm:ss`, so they can be charted and sorted. Spreadsheets have no time zone
  type, which is why both times are given.
- **`.xls` limit:** the old format allows 65,535 readings. More is refused with a message; use `.xlsx` or a smaller
  `--last`. `.xlsx` has no such limit in practice (the 25,747-row history of one iDRAC is a 490 KB file).

## Time zones

Times from the two sources are in different clocks, so ragdoll converts everything to **UTC** when it reads or
polls, and stores it that way: as ISO 8601 text ending in `Z` in the cache and `--output raw`, and as Unix
timestamps in the database. Charts and tables convert back to your computer's local time, and the time zone name is
shown in the title (for example `[AEDT]`).

- `--tz ZONE` shows any named time zone instead, using the IANA names from your system's time zone database, for
  example `--tz Australia/Sydney`, `--tz America/New_York` or `--tz Asia/Tokyo`. Daylight saving is applied, so the
  same sample can show as `[EDT]` in summer and `[EST]` in winter.
- `--utc` is the same as `--tz UTC`. The two cannot be combined.
- `--tz` only changes how times are displayed. It is unrelated to `--tz-offset`, which says what time the iDRAC's own
  clock shows, so that its CSV can be converted to UTC.

Where the times come from:

- **snmp:** the iDRAC does not report a time over SNMP, so each reading is stamped with this computer's clock at the
  moment it was polled. Keep this computer's clock correct (for example with NTP).
- **web:** the iDRAC's CSV uses the iDRAC's own wall clock with no time zone. ragdoll converts it to UTC using the
  clock's offset from UTC, which it measures by comparing the iDRAC's clock (read through Redfish) with this
  computer's clock, rounded to the nearest quarter hour. It does not trust the time zone configured on the iDRAC,
  because an iDRAC can show UTC while set to another time zone. If Redfish is unavailable, or you want to override
  the measurement, pass `--tz-offset` (for example `--tz-offset UTC`, `+10:00` or `-05:00`). Conversion happens when
  the data is fetched, so the cached copy and the database are already in UTC.
- Cache files and databases written by versions before 0.2.0 stored local or iDRAC times with no zone. They are not
  read: an old cache is refetched or ignored, and an old database is refused with a message. Delete the database and
  run again to rebuild it.

## Storage

### Cache

Data is cached per host, source, metric and sensor, for example `<host>_snmp_fan-fan1a.csv`.

- The location is `~/.cache/ragdoll` (or `$XDG_CACHE_HOME/ragdoll`), changed with `--cachedir` (`--cache-dir` also
  works).
- Web data is reused until it is `--max-age` seconds old (default 3600); `--refresh` forces a fetch.
- If a fetch fails and a cache file exists, the cache is used and a warning is printed.
- The snmp source polls on every run, and each poll adds one reading to its cache file.
- Cache files hold sensor readings only, never credentials.

### SQLite database

`--db` keeps every reading in a SQLite database, using Python's built-in `sqlite3` module (nothing extra to install).
Charts are then drawn from the database, so they include readings from every earlier run and from both the web source
and snmp.

```
python3 ragdoll.py --host 192.0.2.20 --source snmp --sensor cpu1 --db --chart line --last week
python3 ragdoll.py --host 192.0.2.20 --source snmp --sensor cpu1 --db /path/to/readings.db --raw
```

- `--db` on its own uses `~/.local/share/ragdoll/ragdoll.db` (or `$XDG_DATA_HOME/ragdoll/ragdoll.db`); `--db PATH`
  uses a database of your choosing. It is created on first use. Without `--db`, nothing is written to a database.
- Storing is repeatable: a reading already in the database is skipped, so each run can safely pass its whole history.
  The web source's full history (about 61,000 hourly samples) is stored on the first run and takes around a second.
- With `--raw`, readings are stored and the CSV is printed without drawing a chart, which suits a cron job.

One table, `readings`, with one row per sample:

| Column | Meaning |
|---|---|
| `host`, `source`, `metric`, `sensor` | what was read, for example `192.0.2.20`, `snmp`, `temperature`, `cpu1` |
| `time` | Unix time of the sample (integer seconds since 1970-01-01 UTC) |
| `average`, `peak` | the reading in the unit for that metric (°C, RPM, W, A, Wh, V or a status code); identical for snmp. For `network` the raw byte counter is stored, and rates are worked out when it is shown |

The primary key is `(host, source, metric, sensor, time)`. Query it with any SQLite tool, for example:

```
sqlite3 ~/.local/share/ragdoll/ragdoll.db \
  "SELECT time, average FROM readings WHERE sensor = 'cpu1' ORDER BY time DESC LIMIT 10"
```

### Charting without contacting the iDRAC

`--no-fetch` skips the fetch or SNMP poll and shows what is already stored. No credentials are needed and nothing is
added to the cache or the database.

```
# from the database (with --db)
python3 ragdoll.py --host 192.0.2.20 --source snmp --sensor cpu1 --db --no-fetch --chart line --last week

# from the cache, however old it is (without --db)
python3 ragdoll.py --host 192.0.2.10 --no-fetch --chart line --last month
```

- With `--db` the readings come from the database; without it, from the cache file, ignoring `--max-age`.
- If nothing is stored for that host, source, metric and sensor, it says so and asks you to run once without
  `--no-fetch`.
- It cannot be combined with `--list`, `--get`, `--save-credentials` or `--refresh`, which all need to contact the
  iDRAC. Sensor names cannot be checked against the iDRAC in this mode, so a misspelt sensor is reported as having no
  stored readings.

## This computer's sensors (lm-sensors)

`--source lmsensors` reads the sensors of a computer with `sensors -j` from lm-sensors, so a desktop, server or
laptop can be charted and logged the same way as an iDRAC. Without `--host` it reads the computer ragdoll is running
on, which needs no login and no network, and the computer's own hostname names it in the cache and the database. With
`--host` it logs in to that computer over ssh and runs `sensors -j` there (see below).

```
$ python3 ragdoll.py --source lmsensors --list
SOURCE     METRIC       SENSOR                          UNIT  VALUE  LIMITS                           KEY
lmsensors  temperature  acpitz-acpi-0-temp1             °C     27.8  -                                acpitz-acpi-0/temp1
lmsensors  temperature  coretemp-isa-0000-core-0        °C       59  warn -..86 crit -..100           coretemp-isa-0000/Core 0
lmsensors  temperature  coretemp-isa-0000-package-id-0  °C       64  warn -..86 crit -..100           coretemp-isa-0000/Package id 0
lmsensors  temperature  nvme-pci-0100-composite         °C    39.85  warn -0.15..81.85 crit -..85.85  nvme-pci-0100/Composite
```

- **Sensors:** each is named `<chip>-<label>`, in lower case with dashes, for example
  `coretemp-isa-0000-package-id-0` (the CPU package) or `nvme-pci-0100-composite` (an NVMe drive). `--list` shows what
  this computer has; it varies, and a machine without a fan or voltage sensor lists none.
- **Metrics:** `temperature` (°C), `fan` (RPM), `voltage` (V) and `power` (W; a current is named `...-current` and shown
  in A). `--metric` chooses, and the default is temperature. `health` and `network` are not available.
- **Default sensor:** with no `--sensor`, ragdoll picks the CPU package, `tctl`, a drive's `composite`, or a `cpu`
  sensor if there is one, and otherwise the first alphabetically.
- **Limits:** lm-sensors' `crit`, `max`, `min` and `lcrit` become the upper critical, upper warning, lower warning and
  lower critical limits, so `--list` shows them and `--limits` draws them.
- **Everything else works as for SNMP:** `--get`, `--tail`, `--output`, `--db` and the other options, and each run
  appends a reading to the cache, so history builds up (use `--tail` or cron). It is not included in a plain `--list`
  of a host's sensors: ask for it by name, `--list lmsensors`.

```
$ python3 ragdoll.py --source lmsensors --get
65 °C
$ python3 ragdoll.py --source lmsensors --get --sensor nvme-pci-0100-composite --field limits
warn -0.15..81.85 crit -..85.85
$ python3 ragdoll.py --source lmsensors --tail --poll 60 --output db      # log it every minute
```

### Another computer, over ssh

Give `--host` (and `--user`, if the login name is not yours) and ragdoll runs `sensors -j` on that computer over ssh.
The readings, the cache and the database are then named after that host.

```
python3 ragdoll.py --source lmsensors --host 192.0.2.30 --user <user> --list
python3 ragdoll.py --source lmsensors --host 192.0.2.30 --user <user> --get
python3 ragdoll.py --source lmsensors --host 192.0.2.30 --user <user> --tail --poll 60 --output db
```

- **How it connects:** ragdoll uses the `paramiko` ssh module and keeps one connection open for the whole run, so a
  `--tail` or a listing of every metric does not log in again for each read (about a second per poll with the `ssh`
  command, a fraction of that now).
- **Login:** your keys and the ssh agent are used, and never a password. `--pass` is rejected, and if no key is accepted
  you get a message saying so with a hint to check `ssh <user>@<host>` by hand.
- **Host keys are checked strictly** against `~/.ssh/known_hosts`. A host that is not there is refused, not accepted
  automatically: ssh to it once yourself and check the key. A key that has changed is refused with a warning. ragdoll
  never writes to `known_hosts`.
- **`~/.ssh/config` is read** for the host's `HostName`, `User`, `Port`, `IdentityFile` and `ProxyCommand`, so an alias
  such as `--host labbox` works. `--user` overrides the `User` in the config.
- **The `ssh` command is the fallback** when paramiko is not installed or the config for the host uses `ProxyJump`,
  which paramiko does not apply. It is run as `ssh -o BatchMode=yes -o ConnectTimeout=10 -o LogLevel=ERROR -- <user>@<host>
  'sensors -j'`, with `--` so that a host name cannot be taken as an ssh option; a `--host` or `--user` that starts with
  `-` or contains a space is rejected either way.
- **On the other computer:** it needs `lm-sensors` installed (otherwise the error says so) and the login needs
  permission to run `sensors`. Nothing is installed or changed there.

## Credentials

No credential is stored in the script. The web source takes `--user` and `--pass` (also spelt `--username` and
`--password`); the snmp source takes `--community`. A missing credential is looked up in this order: command-line
flag, environment variable (`IDRAC_USER`, `IDRAC_PASS`, `IDRAC_COMMUNITY`), the OS keyring, and finally a password
prompt (web only, when `--user` is known). With no community found, the standard read-only SNMP default is used.

They can be kept in the operating system's keyring (Secret Service on Linux, Keychain on macOS, Credential Manager on
Windows) through the `keyring` module, so they never appear on the command line or in shell history:

```
# log in once and save; credentials are only saved if the login succeeds
python3 ragdoll.py --host 192.0.2.10 --user <user> --pass <password> --save-credentials --list

# afterwards no credentials are needed
python3 ragdoll.py --host 192.0.2.10 --last week --chart line

# remove them again
python3 ragdoll.py --host 192.0.2.10 --forget-credentials
```

- Entries are stored per host, under the service name `ragdoll:<host>`. The web and redfish sources use the same
  user name and password. `--save-credentials` saves for `--source`, or
  for the source named after `--list`, otherwise for `web`.
- On a machine without a keyring (a headless server or a cron job), use the `IDRAC_USER`, `IDRAC_PASS` and
  `IDRAC_COMMUNITY` environment variables instead, for example loaded from a file with mode 600.
- A password given with `--pass` is visible to other users in the process list and lands in your shell history. Put a
  space before the command if your shell ignores space-prefixed commands, pass it from an environment variable
  (`--pass "$IDRAC_PASS"`), or use the keyring.
- The iDRAC certificate is not verified by default, because they are usually self-signed; `--secure` turns
  verification on.

## Running regularly

SNMP gives only the current value, so history builds up as readings are collected. Run ragdoll periodically with
`--raw`, which takes a reading without drawing a chart, for example from cron every 10 minutes (one entry per sensor
you care about, since each metric and sensor has its own cache file):

```
*/10 * * * * python3 /path/to/ragdoll.py --host 192.0.2.20 --source snmp --sensor cpu1 --raw >/dev/null
```

Add `--db` to keep the readings in a SQLite database as well. Once there are enough readings, a chart shows how the
sensor changed:

```
python3 ragdoll.py --host 192.0.2.20 --source snmp --sensor cpu1 --chart line --last day
```

The first run shows a single point. To keep a process running instead of using cron, see [Polling with --tail](#polling-with---tail). For a quick look at the latest readings as bars, use `--last 12 --chart
horizontal`; for just the current value, use [`--get`](#getting-a-current-value).

## Polling with --tail

`--tail` keeps ragdoll running and polls the source for a sensor, like `tail -f`, until you press Ctrl-C. `--poll`
sets the seconds between polls (at least 1; the default is 30, about how often an iDRAC refreshes its sensors, so
polling faster only repeats values). Giving `--poll` starts `--tail` too. It defaults to `--source snmp`, and
`--metric` and `--sensor` choose the sensor, as elsewhere.

`--output` says where each reading goes:

| `--output` | What each poll does |
|---|---|
| `text` (default) | prints a line: `2026-10-06 18:03:58  2880 RPM` |
| `csv` | prints CSV rows, with a heading once: `time,host,source,metric,sensor,average,peak,unit` |
| `json` | prints [JSON Lines](https://jsonlines.org): one compact JSON object per reading |
| `raw` | prints rows in the stored cache format, `Average,Peak,Time` with UTC times |
| `db` (or `database`) | stores the reading in the SQLite database and prints nothing |

```
$ python3 ragdoll.py --host 192.0.2.20 --tail --poll 2 --metric fan --sensor fan4b
2026-10-06 18:03:58  2880 RPM
2026-10-06 18:04:00  2880 RPM
2026-10-06 18:04:03  2880 RPM
^C
Stopped after 3 polls.
```

```
# log the inlet temperature every minute to the default database, quietly
python3 ragdoll.py --host 192.0.2.20 --tail --poll 60 --output db
```

- **The database:** `--output db` uses `--db PATH`, or the default database (`~/.local/share/ragdoll/ragdoll.db`) if
  `--db` is not given. Each reading goes in as it is polled, in the same table as everything else (see
  [SQLite database](#sqlite-database)), so charts and `--no-fetch` can use it. `--db` with `--output text` shows each
  reading and stores it too.
- **`--output db` also works for a single run** without `--tail`: it stores the fetched readings and says how many are in
  the database.
- **Counters:** for `network`, the text output shows a rate in bytes per second between polls, and the database keeps
  the raw counter, as it does everywhere.
- **Web source:** `--tail --source web` works but is heavy: each poll downloads the whole hourly history (about 20
  seconds). The first poll stores all of it in the database and shows only the newest sample; later polls show the
  samples that are new. The history only gains a sample an hour, so use a long `--poll` such as 3600.
- **Stopping and errors:** Ctrl-C and SIGTERM stop it cleanly and print a summary (polls made, readings added, polls that
  failed). A first poll that fails stops it with an error (a wrong sensor name, say); a later one that fails prints a
  warning and polling carries on, so a short outage does not end a long run.
- **Not written:** `--tail` does not add to the CSV cache; use `--db` or `--output db` to keep what it reads. It saves the
  sensor's limits, so `--limits` still works.
- `--tail` cannot be combined with `--list`, `--get`, `--raw` or `--no-fetch`, and `table`, `chart`, `xlsx` and `xls` are
  rejected as outputs, because a stream has no table to draw.

A systemd service or a terminal multiplexer is a good place to run it. For one reading every few minutes, cron with
`--raw` (above) is simpler.

## Option reference

| Option | What it does |
|---|---|
| `--host HOST` | the iDRAC's address (required, except with `--source lmsensors`: without it this computer is read, with it that computer over ssh) |
| `--source web\|snmp\|redfish\|racadm\|wsman\|lmsensors` | where the data comes from (`gui` = `web`); default `web`, or `snmp` with `--get`, `--tail` and for the inventory. `redfish` (and `web`) read the inventory over Redfish, `racadm` with racadm, `wsman` over WS-Man; `lmsensors` reads this computer |
| `--metric M` | `temperature`, `fan`, `power`, `voltage`, `health` or `network`; default `temperature` |
| `--sensor S` | which sensor of the metric; the default depends on the metric |
| `--list [sensors\|inventory\|SOURCE\|METRIC]` | list the sensors, or the hardware inventory, and exit |
| `--get [inventory]` | print one sensor's current value over SNMP, or with `inventory` the hardware inventory, and exit |
| `--category C`, `--name N` | with `--list inventory` or `--get inventory`: only that category, or only the items with that name (wildcards allowed) |
| `--detail D` | with `--list inventory` or `--get inventory`: only the value of that attribute of each item |
| `--field F` | with `--list` or `--get`: print only that field (column) of the output |
| `--user`/`--username`, `--pass`/`--password` | web credentials; with `--source lmsensors --host`, `--user` is the ssh login name and `--pass` is not allowed |
| `--community` | SNMP v2c community string |
| `--save-credentials`, `--forget-credentials` | keep or remove this host's credentials in the OS keyring |
| `--secure` | verify the iDRAC's TLS certificate |
| `--output O` | `chart`, `table`, `raw`, `csv`, `json`, `xlsx`, `xls` or `db`; with `--list` or `--get`, `text`, `table`, `csv`, `json`, `xlsx` or `xls`; with `--tail`, `text`, `csv`, `json`, `raw` or `db` |
| `--file FILE` | the spreadsheet to write for `xlsx` and `xls` |
| `--chart C`, `--module M`, `--width W`, `--height H`, `--limits` | chart type, graphing module, size, and limit lines |
| `--last N\|PERIOD` | how many readings to show (default 10 rows) |
| `--tz ZONE`, `--utc` | the time zone for displayed times |
| `--tz-offset OFFSET` | the web source's clock offset from UTC, instead of measuring it |
| `--cachedir DIR`, `--max-age SECONDS`, `--refresh` | the cache directory, how long web data stays fresh, and forcing a fetch |
| `--db [PATH]` | also store readings in a SQLite database and use it for charts |
| `--tail`, `--poll SECONDS` | keep polling the source every SECONDS (default 30) and send each reading to `--output` (`text`, `csv`, `raw` or `db`) |
| `--no-fetch` | show what is already stored without contacting the iDRAC |
| `--raw` | print every stored reading as CSV and exit (the older flag; see [CSV](#csv)) |
| `--version`, `--help` | version and full option help |

## Example output

Real output, with the iDRAC's address replaced by a documentation address. Times are in the machine's local time
zone (AEDT here), which is named in each title.

### Line chart

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

termgraph's `horizontal` bars show the timestamp and value on each line:

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

### CSV

```
$ python3 ragdoll.py --host 192.0.2.20 --source gui --last 3 --output csv
time,host,source,metric,sensor,average,peak,unit
2026-10-06T04:00:31+11:00,192.0.2.20,web,temperature,inlet,14,14,°C
2026-10-06T05:00:31+11:00,192.0.2.20,web,temperature,inlet,14,14,°C
2026-10-06T06:00:31+11:00,192.0.2.20,web,temperature,inlet,14,14,°C

$ python3 ragdoll.py --host 192.0.2.20 --source gui --last 3 --output raw
Average,Peak,Time
14,14,2026-10-05T17:00:31Z
14,14,2026-10-05T18:00:31Z
14,14,2026-10-05T19:00:31Z

$ python3 ragdoll.py --host 192.0.2.20 --raw | head -4
Average,Peak,Time
22,23,2016-10-14T15:59:16Z
20,21,2016-10-17T13:59:19Z
20,21,2016-10-17T14:59:19Z
```

The last command is the older `--raw` flag, which prints every stored reading; only the first rows are shown.

### Sensor listing as a table

```
$ python3 ragdoll.py --host 192.0.2.20 --list temperature --output table
+--------+-------------+---------+------+-------+------------------------+------------------------------------------+
| SOURCE | METRIC      | SENSOR  | UNIT | VALUE | LIMITS                 | KEY                                      |
+--------+-------------+---------+------+-------+------------------------+------------------------------------------+
| snmp   | temperature | cpu1    | °C   |    23 | warn 8..82 crit 3..87  | 1.3.6.1.4.1.674.10892.5.4.700.20.1.6.1.3 |
| snmp   | temperature | cpu2    | °C   |    26 | warn 8..82 crit 3..87  | 1.3.6.1.4.1.674.10892.5.4.700.20.1.6.1.4 |
| snmp   | temperature | exhaust | °C   |    26 | warn 0..70 crit 0..75  | 1.3.6.1.4.1.674.10892.5.4.700.20.1.6.1.2 |
| snmp   | temperature | inlet   | °C   |    16 | warn 3..42 crit -7..47 | 1.3.6.1.4.1.674.10892.5.4.700.20.1.6.1.1 |
| web    | temperature | inlet   | °C   |    14 | warn 3..42 crit -7..47 | iDRAC.Embedded.1#Inlet.1#ThermalHistory  |
+--------+-------------+---------+------+-------+------------------------+------------------------------------------+
```

## Notes

- The iDRAC writes `-128` for both Average and Peak when a sample has no reading (on one iDRAC, nearly half of its
  history). These rows are skipped everywhere: they are not charted, printed, cached or stored, and a database written
  by an earlier version that holds them is cleaned the next time `--db` stores readings: the rows are deleted and the
  file is compacted, which frees the space they used.
- The iDRAC reports temperatures and power supply currents in tenths, and voltages in millivolts; ragdoll converts
  them, so values are always in the units shown (°C, RPM, W, A, Wh, V, B/s or a status code).
- The "Average" and "Peak" series are identical for SNMP, since each reading is a single value.
- The web history can lag well behind real time. On one iDRAC the newest sample stayed the same for about 12 hours and
  then twelve hourly samples appeared at once, so it seems to be written in batches (about every 12 hours there). Use
  SNMP for current values.
- termgraph does not scale a chart whose values are all identical (common for fans and power on an idle server), so for
  those it draws a full-width bar and shows the value in the label. plotext has no such limitation.
- The iDRAC web interface ignores unknown sensor names and returns inlet data, which is why sensor names are checked
  against the list first.
- Sources are registered in the `SOURCES` table in `ragdoll.py`, and outputs in `OUTPUTS`; adding a source (for
  example Redfish) means providing a `list` and a `fetch` function. See `CLAUDE.md` and `TODO.md` for the design notes
  and planned work.

## Version

Current version: **0.4.9**. Print it with `python3 ragdoll.py --version`.

Versions are `MAJOR.MINOR.PATCH` with no number above 9: when one would pass 9 it rolls over into the next, so 0.0.9
is followed by 0.1.0. See [CHANGELOG.md](CHANGELOG.md) for what changed in each version.

## License

This work is licensed under the [Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International
License](https://creativecommons.org/licenses/by-nc-sa/4.0/) (CC BY-NC-SA 4.0). The full text is in [LICENSE](LICENSE).

In short: you may share and adapt it with attribution, not for commercial purposes, and you must distribute your
changes under the same license.

## Help Support Development

If you find this software useful and would like to support its development, please consider buying me a coffee:

https://ko-fi.com/richardatlateralblast
