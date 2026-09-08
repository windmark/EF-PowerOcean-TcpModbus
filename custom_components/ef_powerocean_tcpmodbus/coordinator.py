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
    CONF_MODBUS_CONTROL,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONTROL_COMMAND_METHOD_MASK,
    CONTROL_COMMAND_METHOD_SHIFT,
    CONTROL_COMMAND_POWER_SAVING_BIT,
    CONTROL_COMMAND_REGISTER,
    CONTROL_COMMAND_UNSAFE_BITS,
    CONTROL_FEATURES,
    CONTROL_POWER_FALLBACK_MAX,
    DEFAULT_BATTERY_COUNT,
    DEFAULT_INVERTER_MODEL,
    DEFAULT_MAX_GRID_POWER,
    DEFAULT_MAX_SOLAR_POWER,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL_S,
    DEFAULT_SLAVE,
    DEVICE_INFO_BLOCK,
    DOMAIN,
    FEATURE_SOC_HYSTERESIS,
    FIRMWARE_VERSION,
    HEARTBEAT_INTERVAL_S,
    HEARTBEAT_LAPSE_S,
    HEARTBEAT_REGISTER,
    HEARTBEAT_VALUE,
    MAX_BATTERY_CHARGED_POWER,
    MAX_BATTERY_DISCHARGED_POWER,
    MODBUS_DISABLED_READ_THRESHOLD,
    PRODUCT_CATEGORY,
    PRODUCT_NUMBER,
    SERIAL_NUMBER,
    SLEEP_TIME_AFTER_RECONNECT_S,
    STATE_SAVE_DELAY_S,
    STORAGE_VERSION,
    SYSTEM_STATE_2_CONTROL_MODE_MASK,
    SYSTEM_STATE_2_CONTROL_MODE_SHIFT,
    register_blocks_for,
)
from .energy_processor import EnergyProcessor
from .models import (
    ControlFeature,
    ControlMode,
    CoordinatorStatus,
    FeatureState,
    InverterModel,
    NumberWritableDef,
    RegisterType,
    SocCondition,
    deviation_state,
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
        self._register_blocks = register_blocks_for(self.inverter_model)
        self._registers_by_key = {
            register.key: register
            for block in self._register_blocks
            for register in block.registers
        }
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
        self._consecutive_modbus_disabled_reads = 0
        self._client: AsyncModbusTcpClient = AsyncModbusTcpClient(
            host=self.host, port=self.port, timeout=20, reconnect_delay=0, retries=0
        )
        self._client_slave_id = DEFAULT_SLAVE
        self._lock = asyncio.Lock()
        self._last_checked_data: dict[str, Any] = {}
        self._last_checked_time: datetime | None = None
        self._last_heartbeat_time: datetime | None = None
        self._heartbeat_enabled = config_entry.data.get(CONF_MODBUS_CONTROL, False)
        # None until the device has answered once, so an unsupported model is logged once.
        self._heartbeat_supported: bool | None = None

        # A restart stops the heartbeat, so the device has already handed control back
        # to the app by the time we get here: automatic is the truth, not a guess.
        # The parameters are restored from disk, the arm deliberately is not.
        self._feature = ControlFeature.AUTOMATIC
        self._feature_power: dict[ControlFeature, float] = {
            feature: definition.default_power
            for feature, definition in CONTROL_FEATURES.items()
            if definition.has_power
        }
        self._feature_target_soc: dict[ControlFeature, float] = {
            feature: definition.default_target_soc
            for feature, definition in CONTROL_FEATURES.items()
            if definition.has_target_soc
        }
        # Latched so the feature does not chatter on a SOC sitting at its target.
        self._target_soc_reached = False

        self._commanded_feature = ControlFeature.AUTOMATIC
        self._commanded_power = 0.0
        self._power_saving = False
        self._last_control_write_time: datetime | None = None
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
        return self._consecutive_modbus_disabled_reads >= MODBUS_DISABLED_READ_THRESHOLD

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
    def selected_feature(self) -> ControlFeature:
        """Return the feature the user switched on, driving or merely waiting."""
        return self._feature

    @property
    def active_feature(self) -> ControlFeature | None:
        """Return the feature actually moving power, if any."""
        if self._feature is ControlFeature.AUTOMATIC or not self._heartbeat_enabled:
            return None
        return None if self._target_soc_reached else self._feature

    @property
    def control_method(self) -> ControlMode:
        """Return the protocol control method currently being commanded."""
        return CONTROL_FEATURES[self._commanded_feature].method

    @property
    def control_power(self) -> float:
        """Return the power magnitude currently being commanded."""
        return self._commanded_power

    def feature_power(self, feature: ControlFeature) -> float:
        """Return the configured power, or zero for a feature that has none."""
        return self._feature_power.get(feature, 0.0)

    def feature_power_max(self, feature: ControlFeature) -> float:
        return self._control_power_ceiling(feature)

    def feature_target_soc(self, feature: ControlFeature) -> float:
        return self._feature_target_soc.get(feature, 100.0)

    def feature_state(self, feature: ControlFeature) -> FeatureState:
        """Explain, in one word, why this feature is or is not moving power."""
        if feature is not self._feature:
            return FeatureState.OFF
        if not self._heartbeat_enabled:
            return FeatureState.NO_MODBUS_CONTROL
        if self._target_soc_reached:
            return FeatureState.WAITING_FOR_SOC

        data = self.data or {}
        definition = CONTROL_FEATURES[feature]
        measured = (
            data.get(definition.measure_key)
            if definition.measure_key is not None
            else None
        )
        return deviation_state(
            signed_target=self._commanded_power * definition.sign,
            measured=None if measured is None else float(measured),
            soc=None if (soc := data.get("battery_soc")) is None else float(soc),
            min_soc=float(data.get("min_soc_limit") or 0.0),
        )

    def _control_power_ceiling(self, feature: ControlFeature) -> float:
        """Return the lowest ceiling that applies to *feature*.

        Nothing can exceed the inverter's AC rating whatever the feature asks for,
        and the device's own limit caps it further where one is published.
        """
        data = self.data or {}
        definition = CONTROL_FEATURES[feature]
        ceilings = [float(CONTROL_POWER_FALLBACK_MAX)]

        if definition.limit_key is not None and (
            limit := data.get(definition.limit_key)
        ):
            ceilings.append(float(limit))
        if rated := data.get("inverter_rated_power"):
            ceilings.append(float(rated))

        return min(ceilings)

    @property
    def in_control(self) -> bool:
        """Return whether the device is currently accepting our commands."""
        if not self._heartbeat_enabled or self._heartbeat_supported is not True:
            return False
        if self._last_heartbeat_time is None:
            return False
        return (
            dt.now() - self._last_heartbeat_time
        ).total_seconds() <= HEARTBEAT_LAPSE_S

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
        """Return the control method the device reports in System State 2, if usable.

        Bits 0-6 of System State 2 mirror System Modes, so an all-zero word on a
        system that is reporting anything at all means the register is not
        implemented. The PowerOcean Plus is such a unit: it would otherwise report
        "default" forever and provoke an endless re-send of the control word.
        """
        source = data if data is not None else self.data
        state = (source or {}).get("system_state_2")
        if state is None or int(state) == 0:
            return None
        return ControlMode.from_command_value(
            (int(state) >> SYSTEM_STATE_2_CONTROL_MODE_SHIFT)
            & SYSTEM_STATE_2_CONTROL_MODE_MASK
        )

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
            "feature_power": {
                str(feature): power for feature, power in self._feature_power.items()
            },
            "feature_target_soc": {
                str(feature): soc for feature, soc in self._feature_target_soc.items()
            },
            **self._energy_processor.dump_state(),
        }

    async def async_load_persisted_state(self) -> None:
        """Seed the state from disk so the first poll is validated."""
        if self._store is None or (stored := await self._store.async_load()) is None:
            return

        self._last_checked_data = stored.get("last_checked_data") or {}
        self._last_checked_time = parse_datetime(stored.get("last_checked_time"))
        self._restore_feature_parameters(stored)
        self._energy_processor.load_state(stored)

    def _restore_feature_parameters(self, stored: dict[str, Any]) -> None:
        """Restore what each feature would command, but never that it was on."""
        for feature in self._feature_power:
            if (
                power := (stored.get("feature_power") or {}).get(str(feature))
            ) is not None:
                self._feature_power[feature] = float(power)
        for feature in self._feature_target_soc:
            if (
                soc := (stored.get("feature_target_soc") or {}).get(str(feature))
            ) is not None:
                self._feature_target_soc[feature] = float(soc)

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

    def _require_modbus_control(self) -> None:
        """Refuse a command the device would store and ignore."""
        if not self._heartbeat_enabled:
            raise HomeAssistantError(
                "Modbus control is off. Enable Modbus Control in the integration "
                "configuration to command the inverter; nothing was written."
            )

    async def _async_require_control_authority(self) -> None:
        """Confirm the device is still following us before the write that follows.

        The device stores every write but only acts on it while the heartbeat is
        current, so a command sent without one looks successful and does nothing.
        """
        self._require_modbus_control()

        if not await self.async_send_heartbeat(force=True):
            raise HomeAssistantError(
                f"Heartbeat write to register {HEARTBEAT_REGISTER} failed or was "
                "rejected, so the device would ignore the command. Nothing written."
            )

    def _compose_control_command(self) -> int:
        """Build the control word from the commanded intent and power-saving state.

        System control command (0x0215)
        """
        method = self.control_method.command_value or 0
        word = (method & CONTROL_COMMAND_METHOD_MASK) << CONTROL_COMMAND_METHOD_SHIFT
        if self._power_saving:
            word |= 1 << CONTROL_COMMAND_POWER_SAVING_BIT
        return word

    def _clamp_power(self, watts: float, feature: ControlFeature) -> float:
        """Clamp a magnitude to zero and the device's own ceiling for *feature*."""
        return max(0.0, min(float(watts), self._control_power_ceiling(feature)))

    # ── Features ──────────────────────────────────────────────────────────────

    async def async_select_feature(self, feature: ControlFeature) -> None:
        """Switch a feature on, replacing whatever was on before.

        Switching one on does not necessarily command anything: a feature whose SOC
        target is already met waits, armed, until the state of charge moves back.
        """
        if feature is not ControlFeature.AUTOMATIC:
            self._require_modbus_control()

        self._feature = feature
        self._target_soc_reached = False
        await self.async_apply_feature()

    async def async_set_feature_power(
        self, feature: ControlFeature, watts: float
    ) -> None:
        """Set a feature's power. Editable whether or not the feature is switched on."""
        self._feature_power[feature] = self._clamp_power(watts, feature)
        await self.async_apply_feature()

    async def async_set_feature_target_soc(
        self, feature: ControlFeature, soc: float
    ) -> None:
        """Set the SOC at which a feature stops asking for power."""
        self._feature_target_soc[feature] = max(0.0, min(100.0, soc))
        self._target_soc_reached = False
        await self.async_apply_feature()

    def _update_target_soc_reached(self, data: dict[str, Any]) -> None:
        """Latch whether the selected feature has met its SOC target."""
        definition = CONTROL_FEATURES[self._feature]
        if not definition.has_target_soc:
            self._target_soc_reached = False
            return

        soc = data.get("battery_soc")
        if soc is None:
            return

        soc = float(soc)
        target = self._feature_target_soc[self._feature]
        if definition.soc_condition is SocCondition.STOP_AT_OR_ABOVE:
            reached, released = soc >= target, soc <= target - FEATURE_SOC_HYSTERESIS
        else:
            reached, released = soc <= target, soc >= target + FEATURE_SOC_HYSTERESIS

        if reached:
            self._target_soc_reached = True
        elif released:
            self._target_soc_reached = False

    def _desired_command(self) -> tuple[ControlFeature, float]:
        """Return what the device should be told right now.

        A feature that has met its target keeps its control method with a setpoint
        of zero rather than releasing it: dropping back to the default method would
        let the inverter resume self-consumption and lose the arm.
        """
        if not self._heartbeat_enabled or self._feature is ControlFeature.AUTOMATIC:
            return ControlFeature.AUTOMATIC, 0.0
        if self._target_soc_reached:
            return self._feature, 0.0
        return self._feature, self._clamp_power(
            self.feature_power(self._feature), self._feature
        )

    async def async_apply_feature(
        self, data: dict[str, Any] | None = None, *, notify: bool = True
    ) -> None:
        """Send what the selected feature asks for, if it differs from the last send."""
        self._update_target_soc_reached(data if data is not None else self.data or {})

        feature, power = self._desired_command()
        changed = (feature, round(power)) != (
            self._commanded_feature,
            round(self._commanded_power),
        )
        self._commanded_feature = feature
        self._commanded_power = power

        try:
            if changed or self._control_stale:
                await self._async_send_control(feature, power)
        finally:
            if notify:
                self.async_update_listeners()

    async def _async_send_control(self, feature: ControlFeature, power: float) -> None:
        """Write the setpoint and then the control word that selects its method."""
        if not self.connected:
            raise HomeAssistantError("Modbus client is not connected")

        if CONTROL_FEATURES[feature].commands_power:
            await self._async_require_control_authority()
            await self._async_write_setpoint(feature, power)

        await self._async_write_control_word(self._compose_control_command())
        self._last_control_write_time = dt.now()
        self._control_stale = False

    async def _async_apply_feature_safe(self, data: dict[str, Any]) -> None:
        """Run from a poll, where a write failure must not stop the read."""
        try:
            await self.async_apply_feature(data, notify=False)
        except HomeAssistantError as err:
            _LOGGER.debug(f"Could not apply {self._feature} this poll: {err!r}")

    async def _async_write_setpoint(
        self, feature: ControlFeature, watts: float
    ) -> None:
        """Write the register the feature's method acts on, with the feature's sign."""
        definition = CONTROL_FEATURES[feature]
        if definition.setpoint_key is None:
            return
        try:
            register = self._registers_by_key[definition.setpoint_key]
        except KeyError as err:
            raise HomeAssistantError(
                f"No register mapped for setpoint {definition.setpoint_key} on "
                f"{self.inverter_model}"
            ) from err
        value = int(round(watts)) * definition.sign

        try:
            words = encode_register(value, RegisterType.INT32)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

        _LOGGER.debug(
            "Sending Modbus write command [FC16]: %sW as %s to address %s (%s)",
            value,
            [f"0x{word:04X}" for word in words],
            register.address,
            definition.setpoint_key,
        )

        try:
            async with self._lock:
                response = await self._client.write_registers(
                    address=register.address,
                    values=words,
                    device_id=self._client_slave_id,
                )
        except (ModbusException, ConnectionError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(
                f"Setpoint {value} W could not be sent to {register.address}: {err!r}"
            ) from err

        if response.isError():
            raise HomeAssistantError(
                f"Modbus rejected setpoint {value} W to {register.address}: {response}"
            )

    async def async_set_power_saving(self, enabled: bool) -> None:
        """Command power-saving mode without disturbing the control intent."""
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

        # Power saving applies on its own, like the LED brightness does. Only a
        # control method needs the app locked out, so only it takes control.
        if CONTROL_FEATURES[self._commanded_feature].commands_power:
            await self._async_require_control_authority()

        await self._async_write_control_word(value)
        self._last_control_write_time = dt.now()
        self._control_stale = False
        self.async_update_listeners()
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
                    values=encode_register(value, RegisterType.UINT32),
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

    async def async_get_raw_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {}

        # ── Check Connection, if not -> start reconnection ──
        if not self._client.connected and not await self.async_reconnect():
            raise UpdateFailed("Reconnect failed!")

        await self.async_send_heartbeat()

        try:
            for register_block in self._register_blocks:
                raw = await self.async_read_block(
                    register_block.start, register_block.count
                )
                for register in register_block.registers:
                    data[register.key] = decode_register(
                        register_block.registers_for(raw, register),
                        register.data_type,
                    )

            if is_modbus_disabled(
                self.serial_number,
                data.get("inverter_rated_power"),
                data.get("limit_inv_max"),
            ):
                self._consecutive_modbus_disabled_reads += 1
            else:
                self._consecutive_modbus_disabled_reads = 0

            configured_battery_count = self.limits[CONF_BATTERY_COUNT]
            if data["battery_count"] != configured_battery_count:
                _LOGGER.debug(
                    "Inverter reported battery count %s, but %s is configured; "
                    "using the configured count for this update",
                    data["battery_count"],
                    configured_battery_count,
                )
                data["battery_count"] = configured_battery_count

            await self._async_apply_feature_safe(data)
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
        """Write a device setting and verify it by reading it back.

        Settings apply without Modbus control authority, unlike the control word and
        its setpoints, so this never takes control away from the EcoFlow app.
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
        # A 32-bit register echoes the words just written and only swaps them into
        # read order a few seconds later, so either form means the write landed.
        if readback_words != words and (
            readback_value is None or int(readback_value) != target_value
        ):
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
