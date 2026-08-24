# EcoFlow PowerOcean Plus – Modbus TCP Interface

> Community-discovered Modbus TCP register map and Python monitor for the EcoFlow PowerOcean Plus home battery system.

---

## Table of Contents

- [Overview](#overview)
- [Requirements](#requirements)
- [Connection](#connection)
- [Register Map](#register-map)
  - [Device Info](#device-info)
  - [Status & Battery SOC](#status--battery-soc)
  - [Live Measurements](#live-measurements)
  - [Energy Counters – Today](#energy-counters--today)
  - [Energy Counters – Lifetime](#energy-counters--lifetime)
  - [Configuration Registers](#configuration-registers)
- [Data Types & Decoding](#data-types--decoding)
- [Python Monitor Script](#python-monitor-script)
- [Known Limitations](#known-limitations)
- [Tested Hardware](#tested-hardware)
- [Contributing](#contributing)
- [Disclaimer](#disclaimer)
- [License](#license)

---

## Overview

EcoFlow does **not publish an official Modbus register map** for the PowerOcean Plus.
This documentation was created by scanning the device and cross-referencing values with the official EcoFlow Home Assistant integration (Cloud API).

All registers were discovered empirically on firmware version unknown, hardware serial `R371ZDH4ZGAW0028`.

---

## Requirements

```
Python >= 3.8
pymodbus == 3.6.9
```

Install:

```bash
pip install pymodbus==3.6.9
```

> **Note:** pymodbus 3.12.x has a breaking API change. Version 3.6.9 is recommended for stability.

---

## Connection

| Parameter          | Value                                       |
| ------------------ | ------------------------------------------- |
| Protocol           | Modbus TCP                                  |
| Default Port       | 502                                         |
| Default Slave ID   | 1 (device responds to all IDs 1–250)        |
| Data Encoding      | Big-endian IEEE 754 float, **word-swapped** |
| Register Numbering | 1-based (Modbus standard)                   |

```python
from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient("192.168.x.x", port=502, timeout=3)
client.connect()
```

---

## Register Map

### Device Info

| Register    | Type             | Value                   | Description                                           |
| ----------- | ---------------- | ----------------------- | ----------------------------------------------------- |
| 40001       | UINT16           | 1                       | Device type identifier                                |
| 40002       | UINT16           | 1                       | Unknown                                               |
| 40003       | UINT16           | 3                       | Unknown                                               |
| 40004–40011 | ASCII (8×UINT16) | e.g. `R371ZDH4ZGAW0028` | Serial number (2 chars per register, high byte first) |

**Reading the serial number:**

```python
r = client.read_holding_registers(40004, count=8, slave=1)
chars = []
for val in r.registers:
    chars.append(chr((val >> 8) & 0xFF))
    chars.append(chr(val & 0xFF))
serial = ''.join(c for c in chars if 32 <= ord(c) <= 126)
# → 'R371ZDH4ZGAW0028'
```

---

### Status & Battery SOC

| Register | Type   | Unit | Scale | Description                                |
| -------- | ------ | ---- | ----- | ------------------------------------------ |
| 42081    | UINT16 | —    | —     | System status: `1` = Online, `0` = Offline |
| 42082    | UINT16 | %    | ×1    | Battery State of Charge (SOC)              |

---

### Live Measurements

All values in this section are **32-bit IEEE 754 floats** stored in **2 consecutive registers**, **word-swapped** (low word first, high word second).

#### Decoding example

```python
import struct

def read_float(client, addr, scale=1):
    r = client.read_holding_registers(addr, count=2, slave=1)
    if r.isError():
        return None
    # Word-swapped: registers[1] is high word, registers[0] is low word
    raw = struct.pack('>HH', r.registers[1], r.registers[0])
    return round(struct.unpack('>f', raw)[0] * scale, 3)
```

#### Solar

| Register    | Unit | Scale | Description         |
| ----------- | ---- | ----- | ------------------- |
| 40574/40575 | W    | ×100  | Total PV power      |
| 40602/40603 | A    | ×1    | PV string 1 current |
| 40604/40605 | A    | ×1    | PV string 2 current |
| 40606/40607 | A    | ×1    | PV string 3 current |

#### Battery

| Register    | Unit | Scale | Description                                                 |
| ----------- | ---- | ----- | ----------------------------------------------------------- |
| 40576/40577 | W    | ×1000 | Battery power (positive = charging, negative = discharging) |
| 40578/40579 | °C   | ×1    | Battery ambient temperature                                 |

#### AC Grid

| Register    | Unit | Scale | Description                                                                    |
| ----------- | ---- | ----- | ------------------------------------------------------------------------------ |
| 40580/40581 | V    | ×1    | Grid voltage phase L1                                                          |
| 40582/40583 | V    | ×1    | Grid voltage phase L2                                                          |
| 40584/40585 | V    | ×1    | Grid voltage phase L3                                                          |
| 40586/40587 | A    | ×1    | Grid current phase L1                                                          |
| 40588/40589 | A    | ×1    | Grid current phase L2                                                          |
| 40590/40591 | A    | ×1    | Grid current phase L3                                                          |
| 40592/40593 | Hz   | ×1    | Grid frequency (measurement 1)                                                 |
| 40594/40595 | Hz   | ×1    | Grid frequency (measurement 2)                                                 |
| 40596/40597 | W    | ×10   | Active power / grid feed-in power (positive = feed-in, negative = consumption) |
| 40598/40599 | VA   | ×10   | Apparent power                                                                 |

#### Inverter

| Register    | Unit | Scale | Description          |
| ----------- | ---- | ----- | -------------------- |
| 40600/40601 | °C   | ×1    | Inverter temperature |

---

### Energy Counters – Today

Values reset at midnight.

| Register    | Unit | Scale | Description              |
| ----------- | ---- | ----- | ------------------------ |
| 42163/42164 | kWh  | ×1    | Grid import today        |
| 42179/42180 | kWh  | ×1    | Grid export today        |
| 42195/42196 | kWh  | ×1    | PV string 1 yield today  |
| 42211/42212 | kWh  | ×1    | PV string 2 yield today  |
| 42243/42244 | kWh  | ×1    | Battery charged today    |
| 42145/42146 | kWh  | ×1    | Battery discharged today |

---

### Energy Counters – Lifetime

| Register    | Unit | Scale | Description                                   |
| ----------- | ---- | ----- | --------------------------------------------- |
| 42113/42114 | kWh  | ×1    | Battery net energy (charged minus discharged) |
| 42161/42162 | Wh   | ×1    | Grid import lifetime                          |
| 42177/42178 | Wh   | ×1    | Grid export lifetime                          |
| 42193/42194 | kWh  | ×1    | PV string 1 total yield                       |
| 42209/42210 | kWh  | ×1    | PV string 2 total yield                       |
| 42225/42226 | kWh  | ×1    | Battery total charged (lifetime)              |
| 42227/42228 | kWh  | ×1    | Battery remaining energy (current)            |
| 42241/42242 | kWh  | ×1    | Battery total discharged (lifetime)           |
| 42257/42258 | kWh  | ×1    | Total system energy                           |

---

### Configuration Registers

These registers appear to hold device configuration. Their exact meaning is not fully confirmed.
**Do not write to these registers unless you know what you are doing.**

| Register | Value (observed) | Notes                                           |
| -------- | ---------------- | ----------------------------------------------- |
| 40527    | 100              | Unknown – possibly max SOC limit (%)            |
| 40528    | 15000            | Unknown – possibly power limit (W×10 = 1500 W?) |
| 40536    | 11               | Unknown                                         |
| 40537    | 1                | Unknown                                         |
| 40538    | 15000            | Unknown                                         |
| 40540    | 32               | Unknown                                         |
| 40541    | 20               | Unknown – possibly min cell temperature         |
| 40546    | 15000            | Unknown                                         |
| 40548    | 15000            | Unknown                                         |
| 40552    | 5000             | Unknown                                         |
| 40615    | 10000            | Unknown                                         |
| 40616    | 10000            | Unknown                                         |
| 40617    | 6000             | Unknown                                         |
| 40618    | 6000             | Unknown                                         |
| 40625    | 800              | Unknown                                         |
| 40626    | 800              | Unknown                                         |
| 40627    | 10000            | Unknown                                         |
| 40628    | 10000            | Unknown                                         |

---

## Data Types & Decoding

### UINT16 (single register)

```python
def read_uint16(client, addr):
    r = client.read_holding_registers(addr, count=1, slave=1)
    return r.registers[0] if not r.isError() else None
```

### IEEE 754 Float – word-swapped (2 registers)

The PowerOcean Plus stores all floating point values as **32-bit IEEE 754**, but with the **two 16-bit words in reverse order** (little-endian word order, big-endian byte order within each word).

```python
import struct

def read_float(client, addr, scale=1):
    r = client.read_holding_registers(addr, count=2, slave=1)
    if r.isError():
        return None
    # registers[0] = LOW word, registers[1] = HIGH word
    raw = struct.pack('>HH', r.registers[1], r.registers[0])
    value = struct.unpack('>f', raw)[0]
    return round(value * scale, 3)
```

### ASCII String (multiple registers)

```python
def read_ascii(client, addr, count):
    r = client.read_holding_registers(addr, count=count, slave=1)
    if r.isError():
        return None
    chars = []
    for val in r.registers:
        hi = (val >> 8) & 0xFF
        lo = val & 0xFF
        if 32 <= hi <= 126:
            chars.append(chr(hi))
        if 32 <= lo <= 126:
            chars.append(chr(lo))
    return ''.join(chars).strip()
```

---

## Python Monitor Script

The read-only [`scripts/powerocean_modbus.py`](scripts/powerocean_modbus.py)
utility provides a live monitor based on the integration's current register map:

```bash
uv run --with 'pymodbus>=3.6,<4' python scripts/powerocean_modbus.py \
    192.168.x.x monitor
```

Use `--once` for a single sample or `--interval SECONDS` to change the refresh
interval.

### Discovering Unknown Registers

The `discover` command compares snapshots and reports every changed value.
Mapped registers use their definition from `const.py`. Changed unknown words are
shown as unsigned, signed, and byte-swapped 16-bit candidates. Adjacent unknown
pairs are also decoded as IEEE 754 floats using the common `ABCD`, `BADC`,
`CDAB`, and `DCBA` byte/word orders. It reads registers `40519–40628` by default
and never writes to the device.

```bash
uv run --with 'pymodbus>=3.6,<4' python scripts/powerocean_modbus.py \
    192.168.x.x discover
```

To investigate the minimum SOC register:

1. Start `discover` and wait for the baseline confirmation.
2. Change only the minimum SOC setting in the EcoFlow app.
3. Press Enter and note registers whose delta matches the setting change.
4. Repeat with a different minimum SOC value.

The baseline is updated after each comparison, which helps distinguish the
setting from changing telemetry. Candidate decoding is not proof by itself: the
same address, encoding, and two-register boundary should reproduce several app
values and deltas before updating the integration's register map.

Use `--start` and `--end` after `discover` to scan a different inclusive holding
register range. Use `--port` or `--device-id` before the command when the device
does not use the defaults.

---

## Known Limitations

The following values are **not available via Modbus TCP** and can only be retrieved through the EcoFlow Cloud API:

| Parameter                | Cloud API Sensor     | Notes        |
| ------------------------ | -------------------- | ------------ |
| State of Health          | `bpSoh`              | e.g. 98%     |
| Battery voltage          | `bpVol`              | e.g. 54 V    |
| Max cell temperature     | `bpMaxCellTemp`      | e.g. 22°C    |
| Min cell temperature     | `bpMinCellTemp`      | e.g. 20°C    |
| Battery current (A)      | `bpAmp`              | e.g. -0.05 A |
| Accumulated charge       | `bpAccuChgEnergy`    | lifetime Wh  |
| Accumulated discharge    | `bpAccuDsgEnergy`    | lifetime Wh  |
| Individual MPPT voltages | `mpptPv1_vol`        | e.g. 423 V   |
| Individual MPPT power    | `mpptPv1_pwr`        | e.g. 690 W   |
| Per-phase reactive power | `pcsAPhase_reactPwr` | VAr          |

> Scanned address range: 40001–44096 and 42081–43500. No additional registers were found beyond those documented above.

---

## Tested Hardware

| Device                  | Firmware | Result                      |
| ----------------------- | -------- | --------------------------- |
| EcoFlow PowerOcean Plus | Unknown  | Full register map confirmed |

If you have tested on other EcoFlow devices (Power Ocean DC, Power Ocean Connect 5kWh etc.), please open an issue or PR with your findings.

---

## Contributing

Contributions are very welcome! Especially:

- Testing on other firmware versions
- Testing on related devices (PowerOcean DC, PowerOcean Connect)
- Identifying the remaining unknown configuration registers (40527–40628)
- Home Assistant custom integration based on this register map
- Node-RED / ioBroker / openHAB integration examples

Please open an issue before submitting large PRs.

---

## Disclaimer

This register map was discovered through community reverse engineering.
**EcoFlow does not officially support or document this Modbus interface.**

- Use at your own risk
- Do not write to any registers unless you fully understand the consequences
- This documentation may become inaccurate after firmware updates
- The authors are not responsible for any damage to your system

---

## License

MIT License – free to use, modify, and distribute with attribution.

---

_Discovered and documented by community reverse engineering. Not affiliated with EcoFlow Technology Co., Ltd._
