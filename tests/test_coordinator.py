"""Unit tests for coordinator data validation without Home Assistant."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from ef_powerocean_tcpmodbus import const, models
from ef_powerocean_tcpmodbus import coordinator as coordinator_module
from ef_powerocean_tcpmodbus.energy_processor import EnergyProcessor


@pytest.fixture
def coordinator():
    instance = coordinator_module.EcoflowCoordinator.__new__(
        coordinator_module.EcoflowCoordinator
    )
    instance._last_checked_data = {}
    instance._last_checked_time = None
    instance._last_heartbeat_time = None
    instance._heartbeat_supported = None
    instance._heartbeat_enabled = True
    instance._control_intent = models.ControlIntent.AUTOMATIC
    instance._control_power = 0.0
    instance._power_saving = False
    instance._last_control_write_time = None
    instance._control_stale = False
    instance._client = Mock()
    instance._client_slave_id = 1
    instance._lock = asyncio.Lock()
    instance._status = None
    instance._store = None
    instance._ena_calc_solar_power = False
    instance.inverter_model = const.DEFAULT_INVERTER_MODEL
    instance.limits = {
        const.CONF_MAX_GRID_POWER: 15_000,
        const.CONF_MAX_SOLAR_POWER: 12_000,
        const.CONF_MAX_BATTERY_CHARGED_POWER: 5_000,
        const.CONF_MAX_BATTERY_DISCHARGED_POWER: 6_600,
    }
    instance._energy_processor = coordinator_module.EnergyProcessor(instance.limits)
    return instance


def validate_totals(
    coordinator,
    data: dict[str, float],
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, float]:
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: now)
    return coordinator._energy_processor.validate_totals(
        data, coordinator._last_checked_data, coordinator._last_checked_time
    )


def run_update(
    coordinator,
    raw_data: dict[str, float | None],
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, float]:
    """Run a full coordinator update cycle with a fixed frame and clock."""
    coordinator.async_get_raw_data = AsyncMock(return_value=dict(raw_data))
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: now)
    return asyncio.run(coordinator._async_update_data())


HEARTBEAT_START = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def send_heartbeat(
    coordinator,
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
    *,
    force: bool = False,
) -> bool:
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: now)
    # asyncio.run() builds a fresh loop per call, and a lock binds to the first one.
    coordinator._lock = asyncio.Lock()
    return asyncio.run(coordinator.async_send_heartbeat(force=force))


def heartbeat_response(coordinator, *, is_error: bool) -> AsyncMock:
    coordinator._client.write_register = AsyncMock(
        return_value=Mock(isError=Mock(return_value=is_error))
    )
    return coordinator._client.write_register


def test_heartbeat_is_sent_at_most_once_per_interval(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = heartbeat_response(coordinator, is_error=False)
    interval = const.HEARTBEAT_INTERVAL_S

    assert send_heartbeat(coordinator, HEARTBEAT_START, monkeypatch) is True
    assert coordinator.heartbeat_supported is True

    too_soon = HEARTBEAT_START + timedelta(seconds=interval - 1)
    assert send_heartbeat(coordinator, too_soon, monkeypatch) is True
    assert write.await_count == 1

    due = HEARTBEAT_START + timedelta(seconds=interval)
    assert send_heartbeat(coordinator, due, monkeypatch) is True
    assert write.await_count == 2


def test_forced_heartbeat_before_a_write_ignores_the_interval(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = heartbeat_response(coordinator, is_error=False)

    send_heartbeat(coordinator, HEARTBEAT_START, monkeypatch)
    send_heartbeat(coordinator, HEARTBEAT_START, monkeypatch, force=True)

    assert write.await_count == 2


def test_heartbeat_stops_once_the_device_rejects_the_register(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = heartbeat_response(coordinator, is_error=True)

    assert send_heartbeat(coordinator, HEARTBEAT_START, monkeypatch) is False
    assert coordinator.heartbeat_supported is False

    # A rejection latches it off for polling, but a user action still re-probes.
    later = HEARTBEAT_START + timedelta(minutes=5)
    assert send_heartbeat(coordinator, later, monkeypatch) is False
    assert write.await_count == 1

    assert send_heartbeat(coordinator, later, monkeypatch, force=True) is False
    assert write.await_count == 2


def test_transport_failure_retries_instead_of_disabling_the_heartbeat(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator._client.write_register = AsyncMock(
        side_effect=coordinator_module.ModbusException("connection reset")
    )

    assert send_heartbeat(coordinator, HEARTBEAT_START, monkeypatch) is False
    assert coordinator.heartbeat_supported is None
    assert coordinator.last_heartbeat_time is None

    write = heartbeat_response(coordinator, is_error=False)
    assert send_heartbeat(coordinator, HEARTBEAT_START, monkeypatch) is True
    assert write.await_count == 1


def test_disabling_the_heartbeat_stops_it_and_re_enabling_re_probes(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = heartbeat_response(coordinator, is_error=True)
    coordinator.async_update_listeners = Mock()
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: HEARTBEAT_START)

    # A rejection latches the heartbeat off until something re-probes it.
    send_heartbeat(coordinator, HEARTBEAT_START, monkeypatch)
    assert coordinator.heartbeat_supported is False

    coordinator._lock = asyncio.Lock()
    asyncio.run(coordinator.async_set_heartbeat_enabled(False))
    assert coordinator.heartbeat_enabled is False
    assert send_heartbeat(coordinator, HEARTBEAT_START, monkeypatch) is False
    assert write.await_count == 1

    write = heartbeat_response(coordinator, is_error=False)
    coordinator._lock = asyncio.Lock()
    asyncio.run(coordinator.async_set_heartbeat_enabled(True))

    assert coordinator.heartbeat_enabled is True
    assert coordinator.heartbeat_supported is True
    assert write.await_count == 1


def set_power_saving(coordinator, enabled: bool):
    coordinator._lock = asyncio.Lock()
    return asyncio.run(coordinator.async_set_power_saving(enabled))


def set_control_intent(coordinator, intent):
    coordinator._lock = asyncio.Lock()
    return asyncio.run(coordinator.async_set_control_intent(intent))


def set_control_power(coordinator, watts: float):
    coordinator._lock = asyncio.Lock()
    return asyncio.run(coordinator.async_set_control_power(watts))


def allow_writes(coordinator, monkeypatch: pytest.MonkeyPatch, *, is_error=False):
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: HEARTBEAT_START)
    coordinator._client.connected = True
    coordinator._client.write_register = AsyncMock(
        return_value=Mock(isError=Mock(return_value=False))
    )
    coordinator._client.write_registers = AsyncMock(
        return_value=Mock(isError=Mock(return_value=is_error))
    )
    coordinator.async_refresh = AsyncMock()
    coordinator.async_update_listeners = Mock()
    return coordinator._client.write_registers


def test_control_command_refuses_off_grid_and_shutdown_bits(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = allow_writes(coordinator, monkeypatch)
    # Nothing composable through the public API sets them, so bend the bit to reach
    # the guard: power saving now lands on BIT0, which takes the system off-grid.
    monkeypatch.setattr(coordinator_module, "CONTROL_COMMAND_POWER_SAVING_BIT", 0)

    with pytest.raises(coordinator_module.HomeAssistantError):
        set_power_saving(coordinator, True)

    write.assert_not_awaited()


def test_control_command_writes_both_words_high_word_first(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = allow_writes(coordinator, monkeypatch)

    set_power_saving(coordinator, True)

    # The device parses multi-register writes high word first, unlike its reads.
    write.assert_awaited_once_with(
        address=const.CONTROL_COMMAND_REGISTER,
        values=[0x0000, 0x0008],
        device_id=1,
    )
    coordinator.async_refresh.assert_awaited_once()
    assert coordinator.control_command == 0b1000


def test_intent_composes_the_method_nibble_without_losing_power_saving(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = allow_writes(coordinator, monkeypatch)
    coordinator._power_saving = True
    coordinator.data = {"battery_power": 0.0, "battery_charge_power_limit": 5000.0}

    set_control_intent(coordinator, models.ControlIntent.CHARGE_BATTERY)

    assert write.await_args_list[-1].kwargs == {
        "address": const.CONTROL_COMMAND_REGISTER,
        "values": [0x0000, 0x0038],
        "device_id": 1,
    }


def test_engaging_an_intent_seeds_the_setpoint_from_the_present_measurement(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entering a mode must not apply whatever the register held from last time."""
    write = allow_writes(coordinator, monkeypatch)
    coordinator.data = {"battery_power": 2500.0, "battery_charge_power_limit": 5000.0}

    set_control_intent(coordinator, models.ControlIntent.CHARGE_BATTERY)

    assert coordinator.control_power == 2500.0
    setpoint = const.REGISTERS_BY_KEY["battery_power_setpoint"].address
    assert write.await_args_list[0].kwargs == {
        "address": setpoint,
        "values": [0x0000, 0x09C4],
        "device_id": 1,
    }


def test_discharge_intent_sends_the_magnitude_as_a_negative_setpoint(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = allow_writes(coordinator, monkeypatch)
    coordinator.data = {"battery_power": 0.0, "battery_discharge_power_limit": 5000.0}
    set_control_intent(coordinator, models.ControlIntent.DISCHARGE_BATTERY)

    set_control_power(coordinator, 1500)

    assert coordinator.control_power == 1500.0
    # -1500 as INT32, high word first.
    assert write.await_args_list[-1].kwargs["values"] == [0xFFFF, 0xFA24]


def test_control_power_is_clamped_to_the_device_limit(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_writes(coordinator, monkeypatch)
    coordinator.data = {"battery_power": 0.0, "battery_charge_power_limit": 3000.0}
    set_control_intent(coordinator, models.ControlIntent.CHARGE_BATTERY)

    set_control_power(coordinator, 9999)

    assert coordinator.control_power == 3000.0


def test_control_power_is_refused_while_automatic(coordinator) -> None:
    with pytest.raises(coordinator_module.HomeAssistantError):
        set_control_power(coordinator, 1000)


def test_engaging_an_intent_takes_control_and_automatic_releases_it(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_writes(coordinator, monkeypatch)
    coordinator._heartbeat_enabled = False
    coordinator.data = {"battery_power": 0.0, "battery_charge_power_limit": 5000.0}

    set_control_intent(coordinator, models.ControlIntent.CHARGE_BATTERY)
    assert coordinator.heartbeat_enabled is True

    set_control_intent(coordinator, models.ControlIntent.AUTOMATIC)
    assert coordinator.heartbeat_enabled is False


def test_seeding_refuses_rather_than_commanding_zero(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0 W is a shutdown, not a no-op, so a missing measurement must not seed it."""
    write = allow_writes(coordinator, monkeypatch)
    coordinator.data = {"inverter_rated_power": 10000.0}

    with pytest.raises(coordinator_module.HomeAssistantError):
        set_control_intent(coordinator, models.ControlIntent.LIMIT_INVERTER_OUTPUT)

    write.assert_not_awaited()
    assert coordinator.control_intent is models.ControlIntent.AUTOMATIC


def test_returning_to_automatic_works_while_disconnected(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Releasing needs no write: the device reverts once the heartbeat stops."""
    allow_writes(coordinator, monkeypatch)
    coordinator.data = {"battery_power": 0.0, "battery_charge_power_limit": 5000.0}
    set_control_intent(coordinator, models.ControlIntent.CHARGE_BATTERY)

    coordinator._client.connected = False
    set_control_intent(coordinator, models.ControlIntent.AUTOMATIC)

    assert coordinator.control_intent is models.ControlIntent.AUTOMATIC
    assert coordinator.heartbeat_enabled is False


def test_a_failing_clear_still_releases_control(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_writes(coordinator, monkeypatch)
    coordinator.data = {"battery_power": 0.0, "battery_charge_power_limit": 5000.0}
    set_control_intent(coordinator, models.ControlIntent.CHARGE_BATTERY)

    coordinator._client.write_registers = AsyncMock(
        side_effect=coordinator_module.ModbusException("connection reset")
    )
    set_control_intent(coordinator, models.ControlIntent.AUTOMATIC)

    assert coordinator.control_intent is models.ControlIntent.AUTOMATIC
    assert coordinator.heartbeat_enabled is False


def test_re_selecting_automatic_writes_nothing(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = allow_writes(coordinator, monkeypatch)
    coordinator._heartbeat_enabled = False

    set_control_intent(coordinator, models.ControlIntent.AUTOMATIC)

    write.assert_not_awaited()
    assert coordinator.heartbeat_enabled is False


def test_disabling_the_heartbeat_always_succeeds(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape hatch cannot depend on a write the device may never answer."""
    allow_writes(coordinator, monkeypatch)
    coordinator.data = {"battery_power": 0.0, "battery_charge_power_limit": 5000.0}
    set_control_intent(coordinator, models.ControlIntent.CHARGE_BATTERY)
    coordinator._power_saving = True
    coordinator._client.connected = False

    coordinator._lock = asyncio.Lock()
    asyncio.run(coordinator.async_set_heartbeat_enabled(False))

    assert coordinator.heartbeat_enabled is False
    assert coordinator.control_intent is models.ControlIntent.AUTOMATIC
    assert coordinator.power_saving_commanded is False


def test_control_command_raises_when_the_device_rejects_it(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_writes(coordinator, monkeypatch, is_error=True)

    with pytest.raises(coordinator_module.HomeAssistantError):
        set_power_saving(coordinator, True)

    # The commanded state rolls back, so the UI does not claim a write that failed.
    assert coordinator.power_saving_commanded is False


def reconcile(coordinator, data: dict) -> None:
    coordinator._lock = asyncio.Lock()
    asyncio.run(coordinator._async_reconcile_control_command(data))


def test_control_word_is_re_sent_after_a_control_authority_lapse(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = allow_writes(coordinator, monkeypatch)
    coordinator._power_saving = True
    coordinator._control_stale = True

    reconcile(coordinator, {"system_state_2": 0})

    write.assert_awaited_once_with(
        address=const.CONTROL_COMMAND_REGISTER,
        values=[0x0000, 0x0008],
        device_id=1,
    )
    assert coordinator._control_stale is False


def test_a_lapse_re_sends_the_setpoint_before_the_control_word(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The device fell back to app settings, so the setpoint is gone too."""
    write = allow_writes(coordinator, monkeypatch)
    coordinator._control_intent = models.ControlIntent.CHARGE_BATTERY
    coordinator._control_power = 800.0
    coordinator._control_stale = True

    reconcile(coordinator, {})

    addresses = [call.kwargs["address"] for call in write.await_args_list]
    assert addresses == [
        const.REGISTERS_BY_KEY["battery_power_setpoint"].address,
        const.CONTROL_COMMAND_REGISTER,
    ]


def test_a_settled_command_is_not_re_sent_by_polling(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0x0213 reads 0 on a PowerOcean Plus; polling must never second-guess us."""
    write = allow_writes(coordinator, monkeypatch)
    coordinator._control_intent = models.ControlIntent.CHARGE_BATTERY
    coordinator._control_stale = False

    reconcile(coordinator, {"system_state_2": 0})

    assert coordinator.reported_control_method({"system_state_2": 0}) is None
    write.assert_not_awaited()


def test_re_send_failure_does_not_break_the_poll(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_writes(coordinator, monkeypatch)
    coordinator._client.write_registers = AsyncMock(
        side_effect=coordinator_module.ModbusException("connection reset")
    )
    coordinator._power_saving = True
    coordinator._control_stale = True

    reconcile(coordinator, {"system_state_2": 0})

    assert coordinator.control_command == 0b1000


@pytest.mark.parametrize(
    ("serial_number", "inverter_temperature", "expected"),
    (
        ("R123456789", 0, True),
        ("R123456789", 0.0, True),
        ("R123456789", 21.5, False),
        ("unknown", 0, False),
        ("", 0, False),
        (None, 0, False),
        ("R123456789", None, False),
    ),
)
def test_reports_modbus_disabled_from_current_telemetry(
    coordinator,
    serial_number: str | None,
    inverter_temperature: float | None,
    expected: bool,
) -> None:
    coordinator.serial_number = serial_number
    coordinator._last_inverter_temperature = inverter_temperature

    assert coordinator.is_modbus_disabled is expected


def test_seeds_baseline_from_legacy_persisted_state(coordinator) -> None:
    stored = {
        "last_checked_data": {"grid_import_total": 12.5},
        "last_checked_time": "2026-08-07T12:00:00+00:00",
    }
    coordinator._store = SimpleNamespace(async_load=AsyncMock(return_value=stored))

    asyncio.run(coordinator.async_load_persisted_state())

    assert coordinator._last_checked_data == {"grid_import_total": 12.5}
    assert coordinator._last_checked_time == datetime(
        2026, 8, 7, 12, 0, tzinfo=timezone.utc
    )
    assert coordinator._energy_processor.daily_snapshots == {}
    assert coordinator._energy_processor.last_rollover is None


def test_persisted_state_round_trips(coordinator) -> None:
    coordinator._last_checked_data = {"grid_import_total": 12.5}
    coordinator._last_checked_time = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    coordinator._energy_processor.accepted_at = {
        "grid_import_total": datetime(2026, 8, 7, 11, 0, tzinfo=timezone.utc)
    }
    coordinator._energy_processor.daily_snapshots = {"grid_import_today": 10.0}
    coordinator._energy_processor.last_rollover = datetime(
        2026, 8, 7, 0, 0, tzinfo=timezone.utc
    )

    stored = coordinator._persisted_state()

    coordinator._last_checked_data = {}
    coordinator._last_checked_time = None
    coordinator._energy_processor.accepted_at = {}
    coordinator._energy_processor.daily_snapshots = {}
    coordinator._energy_processor.last_rollover = None
    coordinator._store = SimpleNamespace(async_load=AsyncMock(return_value=stored))

    asyncio.run(coordinator.async_load_persisted_state())

    assert coordinator._last_checked_data == {"grid_import_total": 12.5}
    assert coordinator._energy_processor.accepted_at == {
        "grid_import_total": datetime(2026, 8, 7, 11, 0, tzinfo=timezone.utc)
    }
    assert coordinator._energy_processor.daily_snapshots == {"grid_import_today": 10.0}
    assert coordinator._energy_processor.last_rollover == datetime(
        2026, 8, 7, 0, 0, tzinfo=timezone.utc
    )


def test_accepted_update_publishes_successful_coordinator_status(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    coordinator.async_get_raw_data = AsyncMock(return_value={"grid_import_total": 10.0})
    coordinator._energy_processor.validate_totals = Mock(
        return_value={"grid_import_total": 10.0}
    )
    coordinator._energy_processor.derive_daily = Mock(
        side_effect=lambda data: (data, False)
    )
    coordinator._energy_processor.clamp_calculated = Mock(
        side_effect=lambda data, _prev, **_: data
    )
    coordinator._ena_calc_solar_power = False
    coordinator._store = None
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: now)
    monkeypatch.setattr(
        coordinator_module.TelemetryData,
        "from_mapping",
        Mock(return_value=object()),
    )
    monkeypatch.setattr(
        coordinator_module, "calculate_derived_values", Mock(return_value={})
    )

    asyncio.run(coordinator._async_update_data())

    assert coordinator.status == models.CoordinatorStatus.SUCCESS
    assert "coordinator_status" not in coordinator._last_checked_data


def test_read_failure_raises_to_show_gap(coordinator) -> None:
    coordinator._last_checked_data = {"grid_import_total": 10.0}
    coordinator.async_get_raw_data = AsyncMock(return_value=None)

    with pytest.raises(coordinator_module.UpdateFailed):
        asyncio.run(coordinator._async_update_data())

    assert coordinator.status == models.CoordinatorStatus.READ_FAILED
    # The stale frame is not republished; entities go unavailable instead.
    assert coordinator._last_checked_data == {"grid_import_total": 10.0}


def test_reconnect_failure_updates_coordinator_status(coordinator) -> None:
    coordinator.async_get_raw_data = AsyncMock(
        side_effect=coordinator_module.UpdateFailed("Reconnect failed")
    )

    with pytest.raises(coordinator_module.UpdateFailed):
        asyncio.run(coordinator._async_update_data())

    assert coordinator.status == models.CoordinatorStatus.RECONNECT_FAILED


def test_processing_failure_updates_coordinator_status(coordinator) -> None:
    coordinator.async_get_raw_data = AsyncMock(return_value={})
    coordinator._energy_processor.validate_totals = Mock(
        side_effect=ValueError("Invalid data")
    )

    result = asyncio.run(coordinator._async_update_data())

    assert result is None
    assert coordinator.status == models.CoordinatorStatus.PROCESSING_FAILED


def test_persisted_state_after_reload_clamps_total_reset(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = {
        "last_checked_data": {"grid_import_total": 10.0},
        "last_checked_time": "2026-08-07T11:59:30+00:00",
    }
    coordinator._store = SimpleNamespace(async_load=AsyncMock(return_value=stored))
    asyncio.run(coordinator.async_load_persisted_state())

    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    result = validate_totals(coordinator, {"grid_import_total": 0.0}, now, monkeypatch)

    assert result["grid_import_total"] == 10.0


def test_initial_daily_snapshot_uses_device_daily_value(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_update(
        coordinator,
        {"grid_import_total": 1000.0, "grid_import_today": 5.0},
        datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc),
        monkeypatch,
    )

    assert result["grid_import_today"] == 5.0
    assert result["grid_import_today_raw"] == 5.0
    assert coordinator._energy_processor.daily_snapshots["grid_import_today"] == 995.0


def test_rolls_daily_counters_at_local_midnight(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    totals = {
        "solar_total": 1000.0,
        "grid_import_total": 500.0,
        "grid_export_total": 300.0,
        "bat_charged_total": 200.0,
        "bat_discharged_total": 180.0,
    }
    # Initial observation seeds snapshot to current total; today starts at 0.0
    before = run_update(
        coordinator,
        totals,
        datetime(2026, 8, 27, 16, 0, 0),
        monkeypatch,
    )
    # Energy grows on same date (8 kWh over 7 hours)
    during = run_update(
        coordinator,
        {**totals, "solar_total": 1008.0},
        datetime(2026, 8, 27, 23, 59, 55),
        monkeypatch,
    )
    # Local date boundary crossed at 00:00:05 -> daily reset rolls
    after = run_update(
        coordinator,
        {**totals, "solar_total": 1008.0},
        datetime(2026, 8, 28, 0, 0, 5),
        monkeypatch,
    )
    grown = run_update(
        coordinator,
        {**totals, "solar_total": 1008.01},
        datetime(2026, 8, 28, 0, 0, 10),
        monkeypatch,
    )

    assert before["solar_today"] == 0.0
    assert during["solar_today"] == 8.0
    assert after["solar_today"] == 0.0
    assert grown["solar_today"] == 0.01
    assert coordinator._energy_processor.last_rollover == datetime(2026, 8, 28, 0, 0, 5)


def test_ignores_bogus_zero_device_registers(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay: the daily device registers read a bogus 0 mid-day.

    Derived daily values depend strictly on validated lifetime totals minus
    midnight snapshots, so bogus device registers have no impact.
    """
    totals = {"solar_total": 1000.0, "bat_discharged_total": 180.0}
    coordinator._energy_processor.last_rollover = datetime(2026, 8, 27, 0, 0, 0)
    coordinator._energy_processor.daily_snapshots = {
        "solar_today": 992.0,
        "bat_discharged_today": 177.5,
    }

    result = run_update(
        coordinator,
        {**totals, "solar_today": 0.0, "bat_discharged_today": 0.0},
        datetime(2026, 8, 27, 12, 0, 5),
        monkeypatch,
    )

    assert result["solar_today"] == 8.0
    assert result["bat_discharged_today"] == 2.5
    assert result["solar_today_raw"] == 0.0


def test_publishes_raw_device_daily_as_diagnostic(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The device's own register is published unmodified under a *_raw diagnostic key.
    coordinator._energy_processor.last_rollover = datetime(2026, 8, 27, 0, 0, 0)
    coordinator._energy_processor.daily_snapshots = {"solar_today": 990.0}

    result = run_update(
        coordinator,
        {"solar_total": 1000.0, "solar_today": 9.5},
        datetime(2026, 8, 27, 12, 0, 0),
        monkeypatch,
    )

    assert result["solar_today"] == 10.0
    assert result["solar_today_raw"] == 9.5


def test_clamps_derived_house_energy_rounding_jitter(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 21, 8, 24, 19, tzinfo=timezone.utc)
    previous = {
        "solar_total": 104.0,
        "grid_import_total": 102.0,
        "bat_discharged_total": 101.0,
        "grid_export_total": 100.5,
        "bat_charged_total": 101.35,
        "solar_today": 4.0,
        "grid_import_today": 2.0,
        "bat_discharged_today": 1.0,
        "grid_export_today": 0.5,
        "bat_charged_today": 1.35,
        "house_energy_today": 5.15,
    }
    coordinator._last_checked_time = now - timedelta(seconds=5)
    coordinator._last_checked_data = previous
    coordinator._ena_calc_solar_power = False
    coordinator._energy_processor.daily_snapshots = {
        "solar_today": 100.0,
        "grid_import_today": 100.0,
        "bat_discharged_today": 100.0,
        "grid_export_today": 100.0,
        "bat_charged_today": 100.0,
    }
    coordinator.async_get_raw_data = AsyncMock(
        return_value={
            **previous,
            "bat_charged_total": 101.36,
            "bat_charged_today": 1.36,
        }
    )
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: now)

    result = asyncio.run(coordinator._async_update_data())

    assert result["house_energy_today"] == 5.15


def test_clamps_derived_house_energy_jitter_during_raw_counter_reset(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 21, 9, 10, 54, tzinfo=timezone.utc)
    previous = {
        "solar_total": 105.63,
        "grid_import_total": 100.0,
        "bat_discharged_total": 100.0,
        "grid_export_total": 100.0,
        "bat_charged_total": 100.0,
        "solar_today": 5.63,
        "grid_import_today": 0.0,
        "bat_discharged_today": 0.0,
        "grid_export_today": 0.0,
        "bat_charged_today": 0.0,
        "house_energy_today": 5.63,
    }
    coordinator._last_checked_time = now - timedelta(seconds=5)
    coordinator._last_checked_data = previous
    coordinator._ena_calc_solar_power = False
    coordinator._energy_processor.daily_snapshots = {
        "solar_today": 100.0,
        "grid_import_today": 100.0,
        "bat_discharged_today": 100.0,
        "grid_export_today": 100.0,
        "bat_charged_today": 100.0,
    }
    coordinator.async_get_raw_data = AsyncMock(
        return_value={**previous, "solar_today": 5.62}
    )
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: now)

    result = asyncio.run(coordinator._async_update_data())

    assert result["house_energy_today"] == 5.63


def test_replays_daily_register_flap_without_spike(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay: the device reset its daily registers at local
    midnight, but around 00:00 UTC (02:00 CEST) a ghost of yesterday's values
    flaps onto the daily registers for several polls. The published daily
    sensors derive from the lifetime counters and must not follow the ghost.
    """
    totals = {
        "solar_total": 12408.0,
        "grid_import_total": 820.55,
        "grid_export_total": 9640.02,
        "bat_charged_total": 3120.4,
        "bat_discharged_total": 2980.11,
    }
    ghost_of_yesterday = {
        "solar_today": 62.09,
        "grid_import_today": 0.03,
        "grid_export_today": 42.58,
        "bat_charged_today": 7.35,
        "bat_discharged_today": 6.43,
    }
    new_day = {
        "solar_today": 0.0,
        "grid_import_today": 0.0,
        "grid_export_today": 0.01,
        "bat_charged_today": 0.0,
        "bat_discharged_today": 0.51,
    }

    # The midnight reset occurred two hours earlier at local midnight (2026-08-28 00:00:02)
    coordinator._energy_processor.last_rollover = datetime(2026, 8, 28, 0, 0, 2)
    coordinator._energy_processor.daily_snapshots = {
        "solar_today": 12408.0,
        "grid_import_today": 820.55,
        "grid_export_today": 9640.02,
        "bat_charged_today": 3120.4,
        "bat_discharged_today": 2979.60,
    }
    now = datetime(2026, 8, 28, 1, 59, 57)
    previous = run_update(coordinator, {**totals, **new_day}, now, monkeypatch)
    assert previous["solar_today"] == 0.0
    assert previous["bat_discharged_today"] == 0.51

    flap = [ghost_of_yesterday] * 3 + [new_day] * 3 + [ghost_of_yesterday] * 3
    for raw_dailies in flap:
        now += timedelta(seconds=2)
        # The battery keeps discharging slowly through the night.
        totals["bat_discharged_total"] = round(totals["bat_discharged_total"] + 0.01, 2)
        published = run_update(coordinator, {**totals, **raw_dailies}, now, monkeypatch)

        for key in new_day:
            delta = published[key] - previous[key]
            assert 0 <= round(delta, 2) <= 0.02, f"{key} moved by {delta}"
        assert published["house_energy_today"] >= previous["house_energy_today"] >= 0
        previous = published

    # The ghost never made it through: still the new day's small values.
    assert previous["solar_today"] == 0.0
    assert previous["bat_discharged_today"] == 0.6


def test_replays_hours_long_total_read_gap_with_clean_recovery(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay of the reported statistics gap: solar_total reads failed for
    ~9 h overnight. Published values must hold the last validated value while
    the device's reset cannot be confirmed, then roll and accept the first
    genuine reading the next morning.
    """
    coordinator._energy_processor.last_rollover = datetime(2026, 8, 27, 0, 0, 0)
    coordinator._energy_processor.daily_snapshots = {"solar_today": 12388.0}

    seeded = run_update(
        coordinator,
        {"solar_total": 12408.0, "solar_today": 20.0},
        datetime(2026, 8, 27, 21, 0, 0),
        monkeypatch,
    )
    assert seeded["solar_today"] == 20.0

    for hour in (22, 23):
        held = run_update(
            coordinator,
            {"solar_total": None, "solar_today": None},
            datetime(2026, 8, 27, hour, 0, 0),
            monkeypatch,
        )
        assert held["solar_total"] == 12408.0
        assert held["solar_today"] == 20.0

    past_midnight = run_update(
        coordinator,
        {"solar_total": None, "solar_today": None},
        datetime(2026, 8, 28, 0, 30, 0),
        monkeypatch,
    )
    assert past_midnight["solar_total"] == 12408.0
    assert past_midnight["solar_today"] == 0.0

    recovered = run_update(
        coordinator,
        {"solar_total": 12408.01, "solar_today": 0.01},
        datetime(2026, 8, 28, 6, 20, 0),
        monkeypatch,
    )
    assert recovered["solar_total"] == 12408.01
    assert recovered["solar_today"] == 0.01


def test_floors_derived_house_energy_at_zero_without_baseline(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 26, 12, 0, 4, tzinfo=timezone.utc)
    raw = {
        "solar_total": 100.0,
        "grid_import_total": 100.04,
        "bat_discharged_total": 100.76,
        "grid_export_total": 100.01,
        "bat_charged_total": 107.75,
        "solar_today": 0.0,
        "grid_import_today": 0.04,
        "bat_discharged_today": 0.76,
        "grid_export_today": 0.01,
        "bat_charged_today": 7.75,
    }
    coordinator._last_checked_time = None
    coordinator._last_checked_data = {}
    coordinator._ena_calc_solar_power = False
    coordinator._energy_processor.daily_snapshots = {
        "solar_today": 100.0,
        "grid_import_today": 100.0,
        "bat_discharged_today": 100.0,
        "grid_export_today": 100.0,
        "bat_charged_today": 100.0,
    }
    coordinator.async_get_raw_data = AsyncMock(return_value=dict(raw))
    monkeypatch.setattr(coordinator_module.dt, "now", lambda: now)

    result = asyncio.run(coordinator._async_update_data())

    assert result["house_energy_today"] == 0


@pytest.mark.parametrize("is_error", (False, True), ids=("success", "modbus-error"))
def test_reads_register_block(coordinator, is_error: bool) -> None:
    response = SimpleNamespace(
        isError=Mock(return_value=is_error),
        exception_code=2,
        registers=[11, 22],
    )
    coordinator._client = SimpleNamespace(
        read_holding_registers=AsyncMock(return_value=response)
    )
    coordinator._client_slave_id = const.DEFAULT_SLAVE

    async def read_block():
        coordinator._lock = asyncio.Lock()
        return await coordinator.async_read_block(100, 2)

    if is_error:
        with pytest.raises(coordinator_module.ModbusException):
            asyncio.run(read_block())
    else:
        assert asyncio.run(read_block()) == [11, 22]

    coordinator._client.read_holding_registers.assert_awaited_once_with(
        address=100,
        count=2,
        device_id=const.DEFAULT_SLAVE,
    )


def test_gets_and_decodes_raw_data(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = models.RegisterBlock(
        (
            models.RegisterDef("battery_count", 100, models.RegisterType.UINT16),
            models.RegisterDef("grid_power", 101, models.RegisterType.UINT16),
        )
    )
    monkeypatch.setattr(coordinator_module, "REGISTER_BLOCKS", (block,))
    decode_register = Mock(side_effect=(2.0, 42.0))
    monkeypatch.setattr(coordinator_module, "decode_register", decode_register)
    coordinator._client = SimpleNamespace(connected=True)
    coordinator.async_send_heartbeat = AsyncMock(return_value=True)
    coordinator.async_read_block = AsyncMock(return_value=[2, 42])
    coordinator.limits[const.CONF_BATTERY_COUNT] = 2

    result = asyncio.run(coordinator.async_get_raw_data())

    assert result == {"battery_count": 2.0, "grid_power": 42.0}
    coordinator.async_read_block.assert_awaited_once_with(100, 2)


def test_captures_disabled_state_when_battery_count_guard_drops_frame(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = models.RegisterBlock(
        (
            models.RegisterDef("battery_count", 100, models.RegisterType.UINT16),
            models.RegisterDef("inverter_temperature", 101, models.RegisterType.UINT16),
        )
    )
    monkeypatch.setattr(coordinator_module, "REGISTER_BLOCKS", (block,))
    decode_register = Mock(side_effect=(0.0, 0.0))
    monkeypatch.setattr(coordinator_module, "decode_register", decode_register)
    monkeypatch.setattr(coordinator_module.asyncio, "sleep", AsyncMock())
    coordinator._client = SimpleNamespace(connected=True)
    coordinator.async_send_heartbeat = AsyncMock(return_value=True)
    coordinator.async_read_block = AsyncMock(return_value=[0, 0])
    coordinator.limits[const.CONF_BATTERY_COUNT] = 2
    coordinator.serial_number = "R123456789"

    result = asyncio.run(coordinator.async_get_raw_data())

    assert result is None
    assert coordinator._last_inverter_temperature == 0.0
    assert coordinator.is_modbus_disabled is True


def test_modbus_disabled_recovers_when_telemetry_returns(
    coordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    block = models.RegisterBlock(
        (
            models.RegisterDef("battery_count", 100, models.RegisterType.UINT16),
            models.RegisterDef("inverter_temperature", 101, models.RegisterType.UINT16),
        )
    )
    monkeypatch.setattr(coordinator_module, "REGISTER_BLOCKS", (block,))
    # Provide values for two polls, first with all zeroes and second with values
    decode_register = Mock(side_effect=(0.0, 0.0, 2.0, 21.5))
    monkeypatch.setattr(coordinator_module, "decode_register", decode_register)
    monkeypatch.setattr(coordinator_module.asyncio, "sleep", AsyncMock())
    coordinator._client = SimpleNamespace(connected=True)
    coordinator.async_send_heartbeat = AsyncMock(return_value=True)
    coordinator.async_read_block = AsyncMock(return_value=[0, 0])
    coordinator.limits[const.CONF_BATTERY_COUNT] = 2
    coordinator.serial_number = "R123456789"

    # Run first poll, returning zeros to simulate a Modbus-disabled state.
    assert asyncio.run(coordinator.async_get_raw_data()) is None
    assert coordinator.is_modbus_disabled is True

    # Run second poll, returning valid telemetry to simulate recovery.
    assert asyncio.run(coordinator.async_get_raw_data()) == {
        "battery_count": 2.0,
        "inverter_temperature": 21.5,
    }
    assert coordinator.is_modbus_disabled is False


@pytest.mark.parametrize(
    "registers",
    (const.MODBUS_REGISTERS, const.DEVICE_INFO_BLOCK.registers),
    ids=("polled", "device-info"),
)
def test_registers_do_not_overlap(registers: tuple[models.RegisterDef, ...]) -> None:
    """A multi-word register must not extend into the next register's address."""
    ordered = sorted(registers, key=lambda register: register.address)

    for register, following in zip(ordered, ordered[1:]):
        assert register.end <= following.address, (
            f"{register.key} at {register.address} spans {register.size} words "
            f"and overlaps {following.key} at {following.address}"
        )


def test_blocks_cover_every_register_word_they_map() -> None:
    """Every register must decode from inside the block that was read for it."""
    mapped = [
        register for block in const.REGISTER_BLOCKS for register in block.registers
    ]

    assert sorted(register.key for register in mapped) == sorted(
        register.key for register in const.MODBUS_REGISTERS
    )
    for block in const.REGISTER_BLOCKS:
        for register in block.registers:
            index = block.index_of(register)

            assert index >= 0, (
                f"{register.key} at {register.address} sits before the start of "
                f"its block at {block.start}"
            )
            assert index + register.size <= block.count, (
                f"{register.key} needs words {index}-{index + register.size - 1} "
                f"but the block at {block.start} only reads {block.count}"
            )


def test_writable_numbers_write_to_the_register_they_read() -> None:
    for number in const.WRITABLE_NUMBERS_MAP:
        expected = const.REGISTERS_BY_KEY[number.read_key].address

        assert number.register == expected, (
            f"{number.key} writes to {number.register} but reads "
            f"{number.read_key} from {expected}"
        )


def test_raw_daily_sensors_exist_only_for_device_read_values() -> None:
    """The processor only echoes dailies backed by a total, so entities must match."""
    produced = EnergyProcessor.raw_daily_values(
        {energy_sensor.key: 1.0 for energy_sensor in const.ENERGY_SENSOR_MAP}
    )

    assert {sensor.key for sensor in const.DAILY_ENERGY_SENSORS_DEVICE_RAW} == set(
        produced
    )


def test_enum_sensors_declare_their_options() -> None:
    for sensor in const.SENSOR_MAP:
        if sensor.device_class == "enum":
            assert sensor.options, f"{sensor.key} is an enum without options"


def _device_info_registers(
    serial: str = "R371ZD1AZH3X0450",
    product_number: int = 3,
    product_category: int = 1,
    firmware: int = 0x03001313,
) -> list[int]:
    registers = [0] * const.DEVICE_INFO_BLOCK.count
    registers[const.DEVICE_INFO_BLOCK.index_of(const.PRODUCT_CATEGORY)] = (
        product_category
    )
    registers[const.DEVICE_INFO_BLOCK.index_of(const.PRODUCT_NUMBER)] = product_number
    serial_index = const.DEVICE_INFO_BLOCK.index_of(const.SERIAL_NUMBER)
    for offset in range(const.SERIAL_NUMBER.size):
        high, low = serial[offset * 2], serial[offset * 2 + 1]
        registers[serial_index + offset] = (ord(high) << 8) | ord(low)
    firmware_index = const.DEVICE_INFO_BLOCK.index_of(const.FIRMWARE_VERSION)
    registers[firmware_index] = firmware & 0xFFFF
    registers[firmware_index + 1] = firmware >> 16
    return registers


def test_reads_device_info_in_a_single_request(coordinator) -> None:
    coordinator.firmware_version = None
    coordinator.detected_model = None
    coordinator.inverter_model = models.InverterModel.POWEROCEAN_PLUS
    coordinator.async_read_block = AsyncMock(return_value=_device_info_registers())

    asyncio.run(coordinator.async_read_device_info())

    assert coordinator.serial_number == "R371ZD1AZH3X0450"
    assert coordinator.firmware_version == "3.0.19.19"
    assert coordinator.detected_model == models.InverterModel.POWEROCEAN_PLUS
    coordinator.async_read_block.assert_awaited_once_with(40002, 12)


def test_device_info_read_failure_closes_connection(coordinator) -> None:
    coordinator.firmware_version = None
    coordinator.detected_model = None
    coordinator._client = SimpleNamespace(close=Mock())
    coordinator.async_read_block = AsyncMock(
        side_effect=coordinator_module.ModbusException("boom")
    )

    asyncio.run(coordinator.async_read_device_info())

    assert coordinator.serial_number == "unknown"
    coordinator._client.close.assert_called_once()


def test_read_plan_is_not_split_more_than_necessary() -> None:
    """Neighbouring blocks must be unmergeable, so no poll wastes a round trip."""
    for block, following in zip(const.REGISTER_BLOCKS, const.REGISTER_BLOCKS[1:]):
        gap = following.start - (block.start + block.count)
        merged = following.start + following.count - block.start

        assert (
            gap > models.MAX_REGISTER_GAP or merged > models.MAX_REGISTERS_PER_READ
        ), (
            f"blocks at {block.start} and {following.start} are only {gap} words "
            f"apart and would merge into {merged} words, so they should be one read"
        )


def test_block_rejects_more_registers_than_a_modbus_read_allows() -> None:
    with pytest.raises(ValueError, match="more than the 125"):
        models.RegisterBlock(
            (
                models.RegisterDef("first", 40000, models.RegisterType.UINT16),
                models.RegisterDef("last", 40200, models.RegisterType.UINT16),
            )
        )


@pytest.mark.parametrize(
    ("product_number", "product_category", "expected"),
    (
        (1, 1, models.InverterModel.POWEROCEAN_THREE_PHASE),
        (1, 2, models.InverterModel.POWEROCEAN_SINGLE_PHASE),
        (2, 2, models.InverterModel.POWEROCEAN_SINGLE_PHASE),
        (3, 1, models.InverterModel.POWEROCEAN_PLUS),
        (0, 1, None),
        (None, None, None),
    ),
)
def test_detects_inverter_model_from_product_info(
    product_number: int | None,
    product_category: int | None,
    expected: models.InverterModel | None,
) -> None:
    assert (
        models.InverterModel.from_product_info(product_number, product_category)
        == expected
    )


def test_raw_data_raises_when_reconnect_fails(coordinator) -> None:
    coordinator._client = SimpleNamespace(connected=False)
    coordinator.async_reconnect = AsyncMock(return_value=False)

    with pytest.raises(coordinator_module.UpdateFailed, match="Reconnect failed"):
        asyncio.run(coordinator.async_get_raw_data())
