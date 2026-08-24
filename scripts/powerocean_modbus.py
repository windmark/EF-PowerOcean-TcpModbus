#!/usr/bin/env python3
"""Monitor known PowerOcean values and discover changing Modbus registers."""

from __future__ import annotations

import argparse
import struct
import time
from dataclasses import dataclass
from typing import Any

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ConnectionException

DEFAULT_PORT = 502
DEFAULT_DEVICE_ID = 1
DEFAULT_DISCOVERY_START = 40519
DEFAULT_DISCOVERY_END = 40628
MAX_READ_REGISTERS = 125


@dataclass(frozen=True)
class Register:
    address: int
    label: str
    data_type: str = "float32"
    unit: str = ""


MONITOR_REGISTERS = (
    Register(40519, "House power", unit="W"),
    Register(40521, "Grid power", unit="W"),
    Register(40523, "Solar power", unit="W"),
    Register(40525, "Battery power", unit="W"),
    Register(40527, "Battery SOC", "uint16", "%"),
    Register(40528, "Inverter rated power", "uint16", "W"),
    Register(40530, "System modes", "uint16"),
    Register(40536, "Minimum SOC limit (candidate)", "uint16", "%"),
    Register(40540, "Battery temperature warning", "uint16", "C"),
    Register(40541, "Device LED brightness", "uint16", "%"),
    Register(40546, "Inverter power limit", "uint16", "W"),
    Register(40548, "Maximum inverter power", "uint16", "W"),
    Register(40552, "Battery capacity", "uint16", "Wh"),
    Register(40556, "Battery charge power limit", "uint16", "W"),
    Register(40574, "Battery voltage", unit="V"),
    Register(40576, "Battery current", unit="A"),
    Register(40578, "Battery temperature", unit="C"),
    Register(40580, "Voltage L1", unit="V"),
    Register(40582, "Voltage L2", unit="V"),
    Register(40584, "Voltage L3", unit="V"),
    Register(40586, "Current L1", unit="A"),
    Register(40588, "Current L2", unit="A"),
    Register(40590, "Current L3", unit="A"),
    Register(40592, "Frequency", unit="Hz"),
    Register(40596, "PV1 voltage", unit="V"),
    Register(40598, "PV2 voltage", unit="V"),
    Register(40600, "PV3 voltage", unit="V"),
    Register(40602, "PV1 current", unit="A"),
    Register(40604, "PV2 current", unit="A"),
    Register(40606, "PV3 current", unit="A"),
)
REGISTER_LABELS = {register.address: register.label for register in MONITOR_REGISTERS}


def read_holding_registers(
    client: ModbusTcpClient, address: int, count: int, device_id: int
) -> list[int]:
    """Read holding registers, reconnecting once after a closed TCP socket."""
    try:
        return _read_holding_registers(client, address, count, device_id)
    except ConnectionException:
        print("Modbus connection closed by device; reconnecting...")
        client.close()
        if not client.connect():
            raise RuntimeError("Could not reconnect to the Modbus device") from None
        try:
            return _read_holding_registers(client, address, count, device_id)
        except ConnectionException as error:
            raise RuntimeError(
                f"Modbus connection closed again while reading register {address}"
            ) from error


def _read_holding_registers(
    client: ModbusTcpClient, address: int, count: int, device_id: int
) -> list[int]:
    """Perform one read across supported pymodbus keyword versions."""
    try:
        response = client.read_holding_registers(
            address=address, count=count, device_id=device_id
        )
    except TypeError:
        response = client.read_holding_registers(
            address=address, count=count, slave=device_id
        )

    if response.isError():
        raise RuntimeError(
            f"Modbus read failed at {address} ({count} registers): {response}"
        )
    return response.registers


def read_range(
    client: ModbusTcpClient, start: int, end: int, device_id: int
) -> dict[int, int]:
    """Read an inclusive register range in protocol-sized chunks."""
    values: dict[int, int] = {}
    address = start
    while address <= end:
        count = min(MAX_READ_REGISTERS, end - address + 1)
        registers = read_holding_registers(client, address, count, device_id)
        values.update(
            (address + offset, value) for offset, value in enumerate(registers)
        )
        address += count
    return values


def decode_float32(low_word: int, high_word: int) -> float:
    """Decode a word-swapped, big-endian IEEE 754 float."""
    raw = struct.pack(">HH", high_word, low_word)
    return struct.unpack(">f", raw)[0]


def render_monitor(snapshot: dict[int, int]) -> str:
    """Render known values from one raw register snapshot."""
    lines = ["Register  Value          Unit  Description", "-" * 62]
    for register in MONITOR_REGISTERS:
        if register.data_type == "uint16":
            value: int | float = snapshot[register.address]
        else:
            value = decode_float32(
                snapshot[register.address], snapshot[register.address + 1]
            )
        formatted = f"{value:.3f}" if isinstance(value, float) else str(value)
        lines.append(
            f"{register.address:<9} {formatted:<14} {register.unit:<5} {register.label}"
        )
    return "\n".join(lines)


def changed_registers(
    before: dict[int, int], after: dict[int, int]
) -> list[tuple[int, int, int]]:
    """Return address, old value, and new value for changed registers."""
    return [
        (address, before[address], after[address])
        for address in sorted(before.keys() & after.keys())
        if before[address] != after[address]
    ]


def print_changes(changes: list[tuple[int, int, int]]) -> None:
    if not changes:
        print("No register changes detected.")
        return

    print("Register  Before          After           Delta   Description")
    print("-" * 84)
    for address, before, after in changes:
        print(
            f"{address:<9} {before:>5} (0x{before:04X})  "
            f"{after:>5} (0x{after:04X})  {after - before:+6d}  "
            f"{REGISTER_LABELS.get(address, 'Unknown')}"
        )


def monitor(client: ModbusTcpClient, args: argparse.Namespace) -> None:
    start = min(register.address for register in MONITOR_REGISTERS)
    end = max(
        register.address + (1 if register.data_type == "float32" else 0)
        for register in MONITOR_REGISTERS
    )
    while True:
        snapshot = read_range(client, start, end, args.device_id)
        print("\033[2J\033[H", end="")
        print(render_monitor(snapshot))
        if args.once:
            return
        time.sleep(args.interval)


def discover(client: ModbusTcpClient, args: argparse.Namespace) -> None:
    print(
        f"Reading holding registers {args.start}-{args.end}. "
        "This mode never writes to the device."
    )
    baseline = read_range(client, args.start, args.end, args.device_id)
    print(
        "Baseline captured. Change only the target setting in the EcoFlow app, "
        "then press Enter. Press Ctrl+C to stop."
    )

    while True:
        input()
        current = read_range(client, args.start, args.end, args.device_id)
        print_changes(changed_registers(baseline, current))
        baseline = current
        print("Baseline updated. Make another app change, then press Enter.")


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only EcoFlow PowerOcean Modbus monitor and discovery tool."
    )
    parser.add_argument("host", help="PowerOcean IP address or hostname")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--device-id", type=int, default=DEFAULT_DEVICE_ID)
    subparsers = parser.add_subparsers(dest="command", required=True)

    monitor_parser = subparsers.add_parser("monitor", help="Monitor known registers")
    monitor_parser.add_argument("--interval", type=float, default=1.0)
    monitor_parser.add_argument("--once", action="store_true")
    monitor_parser.set_defaults(handler=monitor)

    discover_parser = subparsers.add_parser(
        "discover", help="Compare snapshots to find registers changed in the app"
    )
    discover_parser.add_argument("--start", type=int, default=DEFAULT_DISCOVERY_START)
    discover_parser.add_argument("--end", type=int, default=DEFAULT_DISCOVERY_END)
    discover_parser.set_defaults(handler=discover)
    return parser


def main() -> int:
    args = create_parser().parse_args()
    if args.command == "discover" and args.start > args.end:
        raise SystemExit("--start must be less than or equal to --end")

    client: Any = ModbusTcpClient(args.host, port=args.port, timeout=3)
    if not client.connect():
        print(f"Could not connect to {args.host}:{args.port}")
        return 1

    try:
        args.handler(client, args)
    except KeyboardInterrupt:
        print("\nStopped.")
    except (OSError, RuntimeError) as error:
        print(f"Error: {error}")
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
