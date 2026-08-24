#!/usr/bin/env python3
"""Monitor known PowerOcean values and discover changing Modbus registers."""

from __future__ import annotations

import argparse
import importlib.util
import math
import struct
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ConnectionException

DEFAULT_PORT = 502
DEFAULT_DEVICE_ID = 1
DEFAULT_DISCOVERY_START = 40519
DEFAULT_DISCOVERY_END = 40628
MAX_READ_REGISTERS = 125
CONST_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "ef_powerocean_tcpmodbus"
    / "const.py"
)


def load_const_module() -> ModuleType:
    """Load const.py without importing the Home Assistant integration package."""
    module_name = "_ef_powerocean_tcpmodbus_const"
    spec = importlib.util.spec_from_file_location(module_name, CONST_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load integration constants from {CONST_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


const = load_const_module()


@dataclass(frozen=True)
class Register:
    key: str
    address: int
    label: str
    size: int
    unit: str = ""


def build_registers(inverter_model: Any) -> tuple[Register, ...]:
    """Build register metadata from the integration's canonical map."""
    sensor_definitions = {
        definition.key: definition
        for definition in (*const.SENSOR_MAP, *const.ENERGY_SENSOR_MAP)
    }
    registers = []
    for block in const.MOD_REGISTER_MAP["blocks"]:
        for definition in block.content:
            sensor = sensor_definitions.get(definition.key)
            label = (
                sensor.name
                if sensor is not None and sensor.name
                else definition.key.replace("_", " ").title()
            )
            unit = sensor.unit if sensor is not None and sensor.unit else ""
            registers.append(
                Register(
                    key=definition.key,
                    address=block.start_register
                    + definition.block_index_for(inverter_model),
                    label=label,
                    size=definition.size,
                    unit=unit,
                )
            )
    return tuple(sorted(registers, key=lambda register: register.address))


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


def read_mapped_registers(
    client: ModbusTcpClient,
    registers: tuple[Register, ...],
    device_id: int,
) -> dict[int, int]:
    """Read mapped registers without spanning large gaps between blocks."""
    snapshot: dict[int, int] = {}
    group_start = registers[0].address
    group_end = group_start + registers[0].size - 1

    for register in registers[1:]:
        register_end = register.address + register.size - 1
        if register_end - group_start + 1 > MAX_READ_REGISTERS:
            snapshot.update(read_range(client, group_start, group_end, device_id))
            group_start = register.address
        group_end = register_end

    snapshot.update(read_range(client, group_start, group_end, device_id))
    return snapshot


def decode_float32(low_word: int, high_word: int) -> float:
    """Decode a word-swapped, big-endian IEEE 754 float."""
    raw = struct.pack(">HH", high_word, low_word)
    return struct.unpack(">f", raw)[0]


def render_monitor(snapshot: dict[int, int], registers: tuple[Register, ...]) -> str:
    """Render known values from one raw register snapshot."""
    lines = ["Register  Value          Unit  Description", "-" * 62]
    for register in registers:
        if register.size == 1:
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


def decode_register(snapshot: dict[int, int], register: Register) -> int | float:
    """Decode one mapped value from a raw snapshot."""
    if register.size == 1:
        return snapshot[register.address]
    return decode_float32(snapshot[register.address], snapshot[register.address + 1])


def format_decoded_value(value: int | float) -> str:
    return f"{value:.3f}" if isinstance(value, float) else str(value)


def format_registers(start: int, size: int) -> str:
    return " + ".join(str(address) for address in range(start, start + size))


def signed_int16(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


def byte_swap16(value: int) -> int:
    return ((value & 0xFF) << 8) | (value >> 8)


def decode_float32_candidates(first_word: int, second_word: int) -> dict[str, float]:
    """Decode two raw words using the four common byte/word orders."""
    raw = struct.pack(">HH", first_word, second_word)
    byte_orders = {
        "ABCD": raw,
        "BADC": raw[1::-1] + raw[3:1:-1],
        "CDAB": raw[2:] + raw[:2],
        "DCBA": raw[::-1],
    }
    return {
        order: value
        for order, candidate in byte_orders.items()
        if math.isfinite(value := struct.unpack(">f", candidate)[0])
    }


def print_unknown_candidates(
    before: dict[int, int],
    after: dict[int, int],
    unknown_changes: list[int],
    mapped_addresses: set[int],
) -> None:
    print("Unknown 16-bit candidates")
    print("Register  Encoding          Before          After           Delta")
    print("-" * 73)
    for address in unknown_changes:
        old_value = before[address]
        new_value = after[address]
        interpretations = (
            ("UINT16", old_value, new_value),
            ("INT16", signed_int16(old_value), signed_int16(new_value)),
            ("UINT16 byte-swap", byte_swap16(old_value), byte_swap16(new_value)),
            (
                "INT16 byte-swap",
                signed_int16(byte_swap16(old_value)),
                signed_int16(byte_swap16(new_value)),
            ),
        )
        for index, (encoding, old_decoded, new_decoded) in enumerate(interpretations):
            address_text = str(address) if index == 0 else ""
            raw_hex = f" (0x{old_value:04X})" if index == 0 else ""
            print(
                f"{address_text:<9} {encoding:<17} {old_decoded:>7}{raw_hex:<9} "
                f"{new_decoded:>7}          {new_decoded - old_decoded:+d}"
            )

    available_unknowns = (before.keys() & after.keys()) - mapped_addresses
    pair_starts = sorted(
        {
            start
            for address in unknown_changes
            for start in (address - 1, address)
            if {start, start + 1} <= available_unknowns
        }
    )
    if not pair_starts:
        return

    print()
    print("Adjacent unknown 32-bit float candidates")
    print("Registers        Order  Before          After           Delta")
    print("-" * 71)
    for start in pair_starts:
        old_candidates = decode_float32_candidates(before[start], before[start + 1])
        new_candidates = decode_float32_candidates(after[start], after[start + 1])
        for index, order in enumerate(("ABCD", "BADC", "CDAB", "DCBA")):
            if order not in old_candidates or order not in new_candidates:
                continue
            old_value = old_candidates[order]
            new_value = new_candidates[order]
            registers = format_registers(start, 2) if index == 0 else ""
            print(
                f"{registers:<16} {order:<6} {old_value:>14.7g}  "
                f"{new_value:>14.7g}  {new_value - old_value:+.7g}"
            )


def print_changes(
    before: dict[int, int],
    after: dict[int, int],
    registers: tuple[Register, ...],
) -> None:
    changed_addresses = {
        address
        for address in before.keys() & after.keys()
        if before[address] != after[address]
    }
    if not changed_addresses:
        print("No register changes detected.")
        return

    mapped_changes = []
    mapped_addresses = set()
    for register in registers:
        addresses = set(range(register.address, register.address + register.size))
        if not addresses <= before.keys() or not addresses <= after.keys():
            continue
        mapped_addresses.update(addresses)
        if addresses & changed_addresses:
            mapped_changes.append(
                (
                    register,
                    decode_register(before, register),
                    decode_register(after, register),
                )
            )

    if mapped_changes:
        print(
            "Mapped value     Before          After           Delta       Description"
        )
        print("-" * 91)
        for register, old_value, new_value in mapped_changes:
            address = format_registers(register.address, register.size)
            delta = new_value - old_value
            print(
                f"{address:<16} {format_decoded_value(old_value):<15} "
                f"{format_decoded_value(new_value):<15} {delta:+11.3f}  "
                f"{register.unit:<5} {register.label}"
            )

    unknown_changes = sorted(changed_addresses - mapped_addresses)
    if not unknown_changes:
        return

    if mapped_changes:
        print()
    print_unknown_candidates(before, after, unknown_changes, mapped_addresses)


def monitor(client: ModbusTcpClient, args: argparse.Namespace) -> None:
    registers = build_registers(args.inverter_model)
    while True:
        snapshot = read_mapped_registers(client, registers, args.device_id)
        print("\033[2J\033[H", end="")
        print(render_monitor(snapshot, registers))
        if args.once:
            return
        time.sleep(args.interval)


def discover(client: ModbusTcpClient, args: argparse.Namespace) -> None:
    registers = build_registers(args.inverter_model)
    print(
        f"Reading holding registers {args.start}-{args.end}. "
        "This mode never writes to the device."
    )
    baseline = read_range(client, args.start, args.end, args.device_id)
    print(
        "Baseline captured. Change only the target setting in the EcoFlow app, "
        "then press Enter. Press Ctrl+C to stop."
    )

    comparison_number = 1
    while True:
        input(
            f"\nChange the setting, then press Enter to capture comparison "
            f"#{comparison_number}..."
        )
        current = read_range(client, args.start, args.end, args.device_id)
        captured_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        print()
        print("=" * 91)
        print(f"COMPARISON #{comparison_number}  |  {captured_at}")
        print("=" * 91)
        print_changes(baseline, current, registers)
        baseline = current
        print("-" * 91)
        print(f"Comparison #{comparison_number} complete; this is the new baseline.")
        comparison_number += 1


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only EcoFlow PowerOcean Modbus monitor and discovery tool."
    )
    parser.add_argument("host", help="PowerOcean IP address or hostname")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--device-id", type=int, default=DEFAULT_DEVICE_ID)
    parser.add_argument(
        "--inverter-model",
        type=const.InverterModel,
        choices=tuple(const.InverterModel),
        default=const.DEFAULT_INVERTER_MODEL,
        help=f"register-map model (default: {const.DEFAULT_INVERTER_MODEL.value})",
    )
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
