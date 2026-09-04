"""The intent map is the only thing standing between the user and a wrong register."""

from __future__ import annotations

import pytest
from ef_powerocean_tcpmodbus import const
from ef_powerocean_tcpmodbus.models import ControlIntent, ControlMode
from ef_powerocean_tcpmodbus.telemetry import TelemetryData, calculate_derived_values


def derived_keys() -> set[str]:
    """Return every key the coordinator adds on top of the raw registers."""
    return set(
        calculate_derived_values(
            TelemetryData.from_mapping({"house_power": 1000.0, "grid_power": -200.0}),
            calculate_solar_power=True,
            startup_voltage=250,
        )
    )


def test_every_intent_is_defined() -> None:
    assert set(const.CONTROL_INTENTS) == set(ControlIntent)
    assert tuple(const.CONTROL_INTENT_SELECT.options) == tuple(ControlIntent)


def test_only_automatic_leaves_the_device_to_itself() -> None:
    automatic = const.CONTROL_INTENTS[ControlIntent.AUTOMATIC]
    assert automatic.method is ControlMode.DEFAULT
    assert not automatic.controls_power

    for intent, definition in const.CONTROL_INTENTS.items():
        if intent is ControlIntent.AUTOMATIC:
            continue
        assert definition.controls_power
        assert definition.method is not ControlMode.DEFAULT
        assert definition.sign in (1, -1)


@pytest.mark.parametrize("intent", tuple(ControlIntent))
def test_intent_keys_all_resolve(intent: ControlIntent) -> None:
    """A typo here would silently write to nothing or bound against nothing."""
    definition = const.CONTROL_INTENTS[intent]
    known = set(const.REGISTERS_BY_KEY) | derived_keys()

    for key in (definition.setpoint_key, definition.limit_key, definition.seed_key):
        if key is not None:
            assert key in known, key

    if definition.setpoint_key is not None:
        assert definition.setpoint_key in const.REGISTERS_BY_KEY


def test_opposing_intents_share_a_register_but_not_a_sign() -> None:
    """Charge and discharge are one register; the sign is what tells them apart."""
    pairs = (
        (ControlIntent.CHARGE_BATTERY, ControlIntent.DISCHARGE_BATTERY),
        (ControlIntent.IMPORT_FROM_GRID, ControlIntent.EXPORT_TO_GRID),
    )
    for positive, negative in pairs:
        first = const.CONTROL_INTENTS[positive]
        second = const.CONTROL_INTENTS[negative]
        assert first.setpoint_key == second.setpoint_key
        assert first.method is second.method
        assert first.sign == 1
        assert second.sign == -1
