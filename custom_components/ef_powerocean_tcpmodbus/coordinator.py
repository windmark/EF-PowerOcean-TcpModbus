"""DataUpdateCoordinator for EcoFlow PowerOcean Plus."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt
from pymodbus import __version__ as pyModbusVersion
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .const import (
    CONF_BATTERY_COUNT,
    CONF_CALC_SOLAR_POWER,
    CONF_HOST,
    CONF_INVERTER_MODEL,
    CONF_MAX_BATTERY_CHARGED_POWER,
    CONF_MAX_BATTERY_DISCHARGED_POWER,
    CONF_MAX_GRID_POWER,
    CONF_MAX_SOLAR_POWER,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONTROL_COMMAND_METHOD_MASK,
    CONTROL_COMMAND_METHOD_SHIFT,
    CONTROL_COMMAND_POWER_SAVING_BIT,
    CONTROL_COMMAND_REASSERT_INTERVAL_S,
    CONTROL_COMMAND_REGISTER,
    CONTROL_COMMAND_UNSAFE_BITS,
    DEFAULT_BATTERY_COUNT,
    DEFAULT_INVERTER_MODEL,
    DEFAULT_MAX_GRID_POWER,
    DEFAULT_MAX_SOLAR_POWER,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL_S,
    DEFAULT_SLAVE,
    DEVICE_INFO_BLOCK,
    DOMAIN,
    FIRMWARE_VERSION,
    HEARTBEAT_INTERVAL_S,
    HEARTBEAT_LAPSE_S,
    HEARTBEAT_REGISTER,
    HEARTBEAT_VALUE,
    MAX_BATTERY_CHARGED_POWER,
    MAX_BATTERY_DISCHARGED_POWER,
    PRODUCT_CATEGORY,
    PRODUCT_NUMBER,
    REGISTER_BLOCKS,
    SERIAL_NUMBER,
    SLEEP_TIME_AFTER_BATTERY_CHECK_FAILED_S,
    SLEEP_TIME_AFTER_RECONNECT_S,
    STATE_SAVE_DELAY_S,
    STORAGE_VERSION,
    SYSTEM_STATE_2_CONTROL_MODE_MASK,
    SYSTEM_STATE_2_CONTROL_MODE_SHIFT,
    SYSTEM_STATUS_LOW_POWER_BIT,
)
from .energy_processor import EnergyProcessor
from .models import (
    ControlMode,
    CoordinatorStatus,
    InverterModel,
    NumberWritableDef,
    encode_register,
)
from .telemetry import (
    TelemetryData,
    calculate_derived_values,
    decode_firmware_version,
    decode_register,
    decode_serial_number,
    is_modbus_disabled,
)
from .util import parse_datetime

_LOGGER = logging.getLogger(__name__)


class EcoflowCoordinator(DataUpdateCoordinator):
    """Fetches data from EcoFlow PowerOcean Plus via Modbus TCP."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
    ) -> None:
        self.host = config_entry.data.get(CONF_HOST)
        self.port = config_entry.data.get(CONF_PORT, DEFAULT_PORT)
        self.scan_interval = config_entry.data.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_S
        )
        self.limits = {
            CONF_BATTERY_COUNT: config_entry.data.get(
                CONF_BATTERY_COUNT, DEFAULT_BATTERY_COUNT
            ),
            CONF_MAX_GRID_POWER: config_entry.data.get(
                CONF_MAX_GRID_POWER, DEFAULT_MAX_GRID_POWER
            ),
            CONF_MAX_SOLAR_POWER: config_entry.data.get(
                CONF_MAX_SOLAR_POWER, DEFAULT_MAX_SOLAR_POWER
            ),
            CONF_MAX_BATTERY_CHARGED_POWER: config_entry.data.get(
                CONF_MAX_BATTERY_CHARGED_POWER, MAX_BATTERY_CHARGED_POWER
            )
            * config_entry.data.get(CONF_BATTERY_COUNT, DEFAULT_BATTERY_COUNT),
            CONF_MAX_BATTERY_DISCHARGED_POWER: config_entry.data.get(
                CONF_MAX_BATTERY_DISCHARGED_POWER, MAX_BATTERY_DISCHARGED_POWER
            )
            * config_entry.data.get(CONF_BATTERY_COUNT, DEFAULT_BATTERY_COUNT),
        }
        self._ena_calc_solar_power = config_entry.data.get(CONF_CALC_SOLAR_POWER, False)
        self.inverter_model = InverterModel(
            config_entry.data.get(CONF_INVERTER_MODEL, DEFAULT_INVERTER_MODEL)
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=self.scan_interval),
        )

        self.serial_number: str | None = None
        self.firmware_version: str | None = None
        self.detected_model: InverterModel | None = None
        self._last_inverter_temperature: float | None = None
        self._client: AsyncModbusTcpClient = AsyncModbusTcpClient(
            host=self.host, port=self.port, timeout=20, reconnect_delay=0, retries=0
        )
        self._client_slave_id = DEFAULT_SLAVE
        self._lock = asyncio.Lock()
        self._last_checked_data: dict[str, Any] = {}
        self._last_checked_time: datetime | None = None
        self._last_heartbeat_time: datetime | None = None
        self._heartbeat_enabled = True
        # None until the device has answered once, so an unsupported model is logged once.
        self._heartbeat_supported: bool | None = None

        # The write-only control word is composed from these two pieces of state, so
        # changing one never clobbers the other. Nothing is written until the user
        # commands something; the first read adopts whatever method the device is in.
        self._control_method = ControlMode.DEFAULT
        self._control_method_adopted = False
        self._power_saving = False
        self._last_control_write_time: datetime | None = None
        self._control_mismatch_logged = False
        # Set when control authority may have lapsed; the next poll re-sends the word.
        self._control_stale = False

        self._energy_processor = EnergyProcessor(self.limits)
        self._status: CoordinatorStatus | None = None
        self._store: Store[dict[str, Any]] | None = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{config_entry.entry_id}.state"
        )

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._client.connected

    @property
    def status(self) -> CoordinatorStatus | None:
        return self._status

    @property
    def is_modbus_disabled(self) -> bool:
        """Return whether the last telemetry read indicates Modbus is disabled."""
        return is_modbus_disabled(
            self.serial_number,
            self._last_inverter_temperature,
        )

    @property
    def heartbeat_supported(self) -> bool | None:
        """Return whether the device accepts the heartbeat, or None if untested."""
        return self._heartbeat_supported

    @property
    def heartbeat_enabled(self) -> bool:
        return self._heartbeat_enabled

    @property
    def last_heartbeat_time(self) -> datetime | None:
        return self._last_heartbeat_time

    @property
    def control_method(self) -> ControlMode:
        """Return the control method being commanded."""
        return self._control_method

    @property
    def power_saving_commanded(self) -> bool:
        """Return whether power saving is being commanded."""
        return self._power_saving

    @property
    def control_command(self) -> int:
        """Return the control command word that the commanded state composes to."""
        return self._compose_control_command()

    @property
    def last_control_write_time(self) -> datetime | None:
        return self._last_control_write_time

    def reported_control_method(
        self, data: dict[str, Any] | None = None
    ) -> ControlMode | None:
        """Return the control method the device reports in System State 2, if read."""
        source = data if data is not None else self.data
        state = (source or {}).get("system_state_2")
        if state is None:
            return None
        return ControlMode.from_command_value(
            (int(state) >> SYSTEM_STATE_2_CONTROL_MODE_SHIFT)
            & SYSTEM_STATE_2_CONTROL_MODE_MASK
        )

    def reported_low_power(self, data: dict[str, Any] | None = None) -> bool | None:
        """Return whether the device reports low-power mode as engaged, if read.

        This is a status bit: it only goes high once the inverter has actually gone
        idle, so it cannot confirm or deny the commanded power-saving switch.
        """
        source = data if data is not None else self.data
        state = (source or {}).get("system_modes")
        if state is None:
            return None
        return bool((int(state) >> SYSTEM_STATUS_LOW_POWER_BIT) & 1)

    def get_pymodbus_version(self) -> str:
        return pyModbusVersion

    # ── Persistence ───────────────────────────────────────────────────────────

    def _persisted_state(self) -> dict[str, Any]:
        """Return the state in a JSON-serializable form."""
        return {
            "last_checked_data": self._last_checked_data,
            "last_checked_time": self._last_checked_time.isoformat()
            if self._last_checked_time is not None
            else None,
            **self._energy_processor.dump_state(),
        }

    async def async_load_persisted_state(self) -> None:
        """Seed the state from disk so the first poll is validated."""
        if self._store is None or (stored := await self._store.async_load()) is None:
            return

        self._last_checked_data = stored.get("last_checked_data") or {}
        self._last_checked_time = parse_datetime(stored.get("last_checked_time"))
        self._energy_processor.load_state(stored)

    # ── Connection ────────────────────────────────────────────────────────────

    async def async_client_shutdown(self) -> None:
        """Integration-Shutdown, closing connection"""
        _LOGGER.info("PowerOcean Shutdown. Closing Connection!")
        if self._store is not None:
            await self._store.async_save(self._persisted_state())
        async with self._lock:
            self._client.close()
        await super().async_shutdown()

    async def async_connect_client(self) -> None:
        """First Client-Connect"""
        await self._client.connect()

        if not self._client.connected:
            _LOGGER.error(f"Modbus TCP not connected to {self.host}:{self.port}")
            return

        await self.async_read_device_info()
        _LOGGER.info(
            f"Modbus TCP is connected to {self.host}:{self.port} (SN: {self.serial_number})"
        )

    async def async_read_device_info(self) -> None:
        """Populate the serial number, firmware and detected model from the device."""
        self.serial_number = "unknown"

        try:
            raw = await self.async_read_block(
                DEVICE_INFO_BLOCK.start, DEVICE_INFO_BLOCK.count
            )
        except ModbusException as err:
            _LOGGER.error(f"Can not read device information. {err.string}.")
            self._client.close()
            return

        if not raw or len(raw) < DEVICE_INFO_BLOCK.count:
            return

        registers_for = partial(DEVICE_INFO_BLOCK.registers_for, raw)

        self.serial_number = (
            decode_serial_number(registers_for(SERIAL_NUMBER)) or "unknown"
        )

        if firmware := decode_firmware_version(registers_for(FIRMWARE_VERSION)):
            self.firmware_version = firmware

        self.detected_model = InverterModel.from_product_info(
            registers_for(PRODUCT_NUMBER)[0], registers_for(PRODUCT_CATEGORY)[0]
        )
        if self.detected_model and self.detected_model != self.inverter_model:
            _LOGGER.warning(
                "Inverter reports %s but %s is configured. Update the integration "
                "options if this is wrong; the model affects PV startup voltage.",
                self.detected_model.display_name,
                self.inverter_model.display_name,
            )

    async def async_reconnect(self) -> bool:
        """Client-Reconnect"""
        delays = [0, 5, 30, 120]
        _LOGGER.debug(
            f"PowerOcean (SN: {self.serial_number}) is not connected. Start reconnect!"
        )

        for i, delay in enumerate(delays):
            async with self._lock:
                if delay > 0:
                    _LOGGER.debug(
                        f"Reconnect failed! Wait {delay}s until next attempt."
                    )
                    await asyncio.sleep(delay)

                _LOGGER.debug(f"Modbus TCP reconnect (Attempt {i + 1}/4)...")
                if await self._client.connect() and self._client.connected:
                    _LOGGER.debug(
                        f"Reconnect successful! (SN: {self.serial_number}) Atempts: {i + 1}/4"
                    )
                    # The outage may have outlasted the device's 60 s window, so send
                    # the next heartbeat at once and let the read-back re-assert the
                    # command if it was dropped.
                    self._last_heartbeat_time = None
                    self._control_stale = True
                    await asyncio.sleep(SLEEP_TIME_AFTER_RECONNECT_S)
                    return True
                self._client.close()

        _LOGGER.error(
            "EF-Modbus-TCP: All reconnect attempts failed! – will retry next poll"
        )
        return False

    async def async_read_block(self, addr: int, count: int) -> list[int] | None:
        """Read *count* holding registers starting at *addr*.  Returns None on error."""
        async with self._lock:
            res = await self._client.read_holding_registers(
                address=addr, count=count, device_id=self._client_slave_id
            )
            if res.isError():
                # Modbus error response – connection may be stale
                raise ModbusException(
                    f"Modbus error response at 0x{addr:04X} with Exception-Code {res.exception_code}"
                )
            return res.registers

    # ── Heartbeat ─────────────────────────────────────────────────────────────

    async def async_send_heartbeat(self, *, force: bool = False) -> bool:
        """Refresh Modbus control authority. Never raises; a miss only costs authority.

        With *force* the register is written even if a previous attempt was rejected,
        so a user action always gets a fresh verdict from the device.
        """
        if not self._heartbeat_enabled:
            return False
        if self._heartbeat_supported is False and not force:
            return False

        now = dt.now()
        if self._last_heartbeat_time is not None:
            since_last = (now - self._last_heartbeat_time).total_seconds()
            if not force and since_last < HEARTBEAT_INTERVAL_S:
                return True
            if since_last > HEARTBEAT_LAPSE_S:
                _LOGGER.debug(
                    "Heartbeat gap of %.0fs exceeded the device window; the control "
                    "word will be re-sent",
                    since_last,
                )
                self._control_stale = True

        try:
            async with self._lock:
                response = await self._client.write_register(
                    address=HEARTBEAT_REGISTER,
                    value=HEARTBEAT_VALUE,
                    device_id=self._client_slave_id,
                )
        except (ModbusException, ConnectionError, asyncio.TimeoutError) as err:
            # Transport trouble, not a verdict on the register: retry next poll.
            _LOGGER.debug(f"Heartbeat write failed: {err!r}")
            return False

        if response.isError():
            if self._heartbeat_supported is not False:
                _LOGGER.warning(
                    "Heartbeat register %s rejected by the device (%s). Writes will "
                    "be acknowledged but may never take effect on this model.",
                    HEARTBEAT_REGISTER,
                    response,
                )
            self._heartbeat_supported = False
            return False

        if self._heartbeat_supported is not True:
            _LOGGER.info(
                "Heartbeat register %s accepted; Modbus control authority is being "
                "refreshed every %ss.",
                HEARTBEAT_REGISTER,
                HEARTBEAT_INTERVAL_S,
            )
        self._heartbeat_supported = True
        self._last_heartbeat_time = now
        return True

    async def async_set_heartbeat_enabled(self, enabled: bool) -> None:
        """Turn the heartbeat on or off at runtime, without reloading the entry."""
        if enabled == self._heartbeat_enabled:
            return

        self._heartbeat_enabled = enabled
        self._last_heartbeat_time = None
        if enabled:
            # Re-probe, so an earlier rejection does not survive a manual retry.
            self._heartbeat_supported = None
            # The device released control while the heartbeat was off.
            self._control_stale = True
            await self.async_send_heartbeat(force=True)

        _LOGGER.info("Modbus heartbeat %s", "enabled" if enabled else "disabled")
        self.async_update_listeners()

    async def _async_require_control_authority(self) -> None:
        """Make sure the device will act on the write that follows.

        The device stores every write, but only acts on them while the heartbeat is
        current, so a write without authority would look successful and do nothing.
        """
        if not self._heartbeat_enabled:
            _LOGGER.warning(
                "Heartbeat is disabled: the device will store this write but is "
                "unlikely to act on it. Enable the heartbeat switch for control."
            )
            return

        if not await self.async_send_heartbeat(force=True):
            raise HomeAssistantError(
                f"Heartbeat write to register {HEARTBEAT_REGISTER} failed or was "
                "rejected, so the device would ignore the command. Nothing written."
            )

    # ── System control command (0x0215) ───────────────────────────────────────

    def _compose_control_command(self) -> int:
        """Build the control word from the commanded method and power-saving state."""
        method = self._control_method.command_value
        if method is None:
            method = ControlMode.DEFAULT.command_value
        word = (method & CONTROL_COMMAND_METHOD_MASK) << CONTROL_COMMAND_METHOD_SHIFT
        if self._power_saving:
            word |= 1 << CONTROL_COMMAND_POWER_SAVING_BIT
        return word

    async def async_set_control_method(self, method: ControlMode) -> None:
        """Command a control method. Setpoints only take effect while theirs is active."""
        if method.command_value is None:
            raise HomeAssistantError(f"Control method {method} cannot be commanded")

        # Re-selecting the current method is a deliberate re-send, so no early return.
        previous = self._control_method
        self._control_method = method
        self._control_method_adopted = True
        try:
            await self._async_apply_control_command()
        except HomeAssistantError:
            self._control_method = previous
            raise

    async def async_set_power_saving(self, enabled: bool) -> None:
        """Command power-saving mode without disturbing the control method."""
        previous = self._power_saving
        self._power_saving = enabled
        try:
            await self._async_apply_control_command()
        except HomeAssistantError:
            self._power_saving = previous
            raise

    async def _async_apply_control_command(self) -> None:
        """Write the composed control word once and refresh so the read-back shows it."""
        value = self._compose_control_command()
        if value & CONTROL_COMMAND_UNSAFE_BITS:
            raise HomeAssistantError(
                f"Refusing control command 0x{value:08X}: it would take the system "
                "off-grid or shut it down."
            )
        if not self.connected:
            raise HomeAssistantError("Modbus client is not connected")

        await self._async_require_control_authority()
        await self._async_write_control_word(value)
        self._last_control_write_time = dt.now()
        self._control_mismatch_logged = False
        self._control_stale = False
        self.async_update_listeners()

        # The register itself cannot be read; System State 2 is the read-back. The
        # poll does not re-send the word unless the device disagrees for a while, so
        # this refresh cannot turn into a second write.
        await self.async_refresh()

    async def _async_write_control_word(self, value: int) -> None:
        _LOGGER.debug(
            "Sending Modbus write command [FC16]: 0x%08X to address %s (Device ID: %s)",
            value,
            CONTROL_COMMAND_REGISTER,
            self._client_slave_id,
        )

        try:
            async with self._lock:
                response = await self._client.write_registers(
                    address=CONTROL_COMMAND_REGISTER,
                    values=[value & 0xFFFF, (value >> 16) & 0xFFFF],
                    device_id=self._client_slave_id,
                )
        except (ModbusException, ConnectionError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(
                f"Control command 0x{value:08X} could not be sent: {err!r}"
            ) from err

        if response.isError():
            raise HomeAssistantError(
                f"Modbus rejected control command 0x{value:08X}: {response}"
            )

    def _adopt_reported_state(self, data: dict[str, Any]) -> None:
        """On the first read, take over what the device is already doing.

        After a restart the device is still running the last commanded method, so
        starting from Default would misreport the state and would never restore it.
        Power saving is seeded from the low-power status bit, the only hint there
        is; it is a best guess because that bit is only set once the inverter idles.
        """
        if self._control_method_adopted:
            return
        reported = self.reported_control_method(data)
        if reported is None or reported.command_value is None:
            return
        self._control_method = reported
        self._control_method_adopted = True
        if (low_power := self.reported_low_power(data)) is not None:
            self._power_saving = low_power
        if reported is not ControlMode.DEFAULT or self._power_saving:
            _LOGGER.info(
                "Adopted device state: control method %s, power saving %s",
                reported,
                self._power_saving,
            )

    async def _async_reconcile_control_command(self, data: dict[str, Any]) -> None:
        """Keep the device on the commanded word without blindly re-writing it.

        Two cases warrant a write: control authority lapsed (the device then falls
        back to the app settings, so anything commanded, including power saving, is
        gone), or the device reports a control method other than the commanded one.
        Blindly re-writing every poll forced the method back to Default around any
        manual change and, if bit 3 were edge-triggered, toggled power saving.
        """
        if not self._heartbeat_enabled or self._heartbeat_supported is False:
            # Without authority the device will not follow anyway; don't fight it.
            return
        if not self._control_method_adopted:
            return

        commanding_something = (
            self._control_method is not ControlMode.DEFAULT or self._power_saving
        )

        if self._control_stale:
            if not commanding_something:
                self._control_stale = False
                return
            word = self._compose_control_command()
            _LOGGER.info(
                "Re-sending control word 0x%08X after a control-authority lapse", word
            )
            try:
                await self._async_write_control_word(word)
            except HomeAssistantError as err:
                _LOGGER.debug(f"Control command re-send failed: {err!r}")
                return
            self._last_control_write_time = dt.now()
            self._control_stale = False
            return

        if self._control_method is ControlMode.DEFAULT:
            # Nothing that needs holding; power saving alone has no reliable read-back.
            return

        reported = self.reported_control_method(data)
        if reported is None:
            return
        if reported is self._control_method:
            if self._control_mismatch_logged:
                _LOGGER.info(
                    "Device now reports control method %s as commanded", reported
                )
                self._control_mismatch_logged = False
            return

        now = dt.now()
        if (
            self._last_control_write_time is not None
            and (now - self._last_control_write_time).total_seconds()
            < CONTROL_COMMAND_REASSERT_INTERVAL_S
        ):
            return

        word = self._compose_control_command()
        log = _LOGGER.debug if self._control_mismatch_logged else _LOGGER.warning
        log(
            "Device reports control method %s while %s is commanded; re-sending "
            "0x%08X. If this repeats, the device is not accepting the control word "
            "(check heartbeat, Pro-app Modbus control, word order, unit ID).",
            reported,
            self._control_method,
            word,
        )
        self._control_mismatch_logged = True

        try:
            await self._async_write_control_word(word)
            self._last_control_write_time = now
        except HomeAssistantError as err:
            _LOGGER.debug(f"Control command re-assert failed: {err!r}")

    def _log_state_word_changes(self, data: dict[str, Any]) -> None:
        """Trace the raw status words, so any reaction to a command is visible."""
        for key in ("system_modes", "system_state_2"):
            new_value = data.get(key)
            previous = self._last_checked_data.get(key)
            if new_value is None or new_value == previous:
                continue
            _LOGGER.debug(
                "%s changed 0x%08X -> 0x%08X (commanding 0x%08X)",
                key,
                int(previous or 0),
                int(new_value),
                self._compose_control_command(),
            )

    # ── Polling ───────────────────────────────────────────────────────────────

    async def async_get_raw_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {}

        # ── Check Connection, if not -> start reconnection ──
        if not self._client.connected and not await self.async_reconnect():
            raise UpdateFailed("Reconnect failed!")

        await self.async_send_heartbeat()

        try:
            # Read all register blocks
            for register_block in REGISTER_BLOCKS:
                raw = await self.async_read_block(
                    register_block.start, register_block.count
                )
                for register in register_block.registers:
                    data[register.key] = decode_register(
                        register_block.registers_for(raw, register),
                        register.data_type,
                    )

            # Store the inverter temperature used for the modbus tcp disabled check, before we do any data validations.
            self._last_inverter_temperature = data.get("inverter_temperature")

            if data["battery_count"] != self.limits[CONF_BATTERY_COUNT]:
                _LOGGER.debug(
                    f"Read battery count {data['battery_count']} is unequal -> Skip data! Wait {SLEEP_TIME_AFTER_BATTERY_CHECK_FAILED_S}s."
                )
                await asyncio.sleep(SLEEP_TIME_AFTER_BATTERY_CHECK_FAILED_S)
                return None

            self._adopt_reported_state(data)
            await self._async_reconcile_control_command(data)

            return data
        except ModbusException as err:
            _LOGGER.debug(f"{err.string}. Connection closing...")
            self._client.close()
            return None
        except Exception as err:
            _LOGGER.error(f"Unexpected error during data fetch: {repr(err)}")
            return data

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            raw_data = await self.async_get_raw_data()
        except UpdateFailed:
            self._status = CoordinatorStatus.RECONNECT_FAILED
            raise UpdateFailed(
                "Reconnect attempts failed! Integration stopped. Retry after 120s.",
                retry_after=120,
            )

        if raw_data is None:
            self._status = CoordinatorStatus.READ_FAILED
            raise UpdateFailed(
                "Read failed; entities stay unavailable until the next successful read."
            )

        try:
            result = self._energy_processor.validate_totals(
                raw_data, self._last_checked_data, self._last_checked_time
            )
            result.update(self._energy_processor.raw_daily_values(raw_data))
            result, is_daily_reset = self._energy_processor.derive_daily(result)
            calculated_results = calculate_derived_values(
                TelemetryData.from_mapping(result),
                calculate_solar_power=self._ena_calc_solar_power,
                startup_voltage=self.inverter_model.startup_voltage,
            )
            result.update(calculated_results)
            result = self._energy_processor.clamp_calculated(
                result, self._last_checked_data, is_daily_reset=is_daily_reset
            )

            self._log_state_word_changes(result)
            self._last_checked_data = dict(result)
            self._last_checked_time = dt.now()
            self._status = CoordinatorStatus.SUCCESS
            if self._store is not None:
                self._store.async_delay_save(self._persisted_state, STATE_SAVE_DELAY_S)

            return dict(result)
        except Exception as err:
            self._status = CoordinatorStatus.PROCESSING_FAILED
            _LOGGER.error(f"Unexpected error during data fetch: {repr(err)}")
            return None

    # ── Parameter and setpoint writes ─────────────────────────────────────────

    async def async_write_modbus_register(
        self, entity_def: NumberWritableDef, value: int
    ) -> None:
        """Write a parameter or setpoint register and verify it by reading it back.

        UINT16 registers go out with FC6; INT32/UINT32 with FC16, low word first,
        the same order the read path decodes.
        """
        if not self.connected:
            raise HomeAssistantError("Modbus client is not connected")

        target_value = int(value)
        register_address = entity_def.register
        key = entity_def.read_key

        try:
            words = encode_register(target_value, entity_def.data_type)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

        required = entity_def.requires_control_method
        if required is not None and required is not self._control_method:
            _LOGGER.warning(
                "%s only takes effect while the control method is %s, but %s is "
                "commanded. The value is stored; select the control method for the "
                "device to act on it.",
                key,
                required,
                self._control_method,
            )

        # The device silently ignores writes when control authority has lapsed.
        await self._async_require_control_authority()

        _LOGGER.debug(
            "Sending Modbus write command [%s]: value %s -> %s to address %s (Key: %s, Device ID: %s)",
            "FC6" if len(words) == 1 else "FC16",
            target_value,
            [f"0x{word:04X}" for word in words],
            register_address,
            key,
            self._client_slave_id,
        )

        try:
            async with self._lock:
                if len(words) == 1:
                    response = await self._client.write_register(
                        address=register_address,
                        value=words[0],
                        device_id=self._client_slave_id,
                    )
                else:
                    response = await self._client.write_registers(
                        address=register_address,
                        values=words,
                        device_id=self._client_slave_id,
                    )
                if response.isError():
                    raise HomeAssistantError(
                        f"Modbus rejected write to register {register_address}: {response}"
                    )

                readback_response = await self._client.read_holding_registers(
                    address=register_address,
                    count=len(words),
                    device_id=self._client_slave_id,
                )
                if readback_response.isError():
                    raise HomeAssistantError(
                        f"Could not verify write to register {register_address}: {readback_response}"
                    )
                readback_words = list(readback_response.registers)
        except (ModbusException, ConnectionError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(
                f"Error writing register {register_address} via Modbus TCP: {err!r}"
            ) from err

        readback_value = decode_register(readback_words, entity_def.data_type)
        if readback_value is None or int(readback_value) != target_value:
            raise HomeAssistantError(
                f"Register {register_address} acknowledged value {target_value}, "
                f"but read back {readback_value}"
            )

        _LOGGER.info(
            "Register %s [%s] acknowledged value: %s (the device may still ignore "
            "it; confirm the effect, not the readback)",
            register_address,
            key,
            target_value,
        )

        updated_data = {**(self.data or {}), key: target_value}
        self.async_set_updated_data(updated_data)
