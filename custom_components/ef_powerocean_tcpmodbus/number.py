"""Dynamic number entities configuration platform for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONTROL_POWER_NUMBER, DOMAIN, WRITABLE_NUMBERS_MAP
from .coordinator import EcoflowCoordinator
from .entity import EcoFlowBaseEntity
from .models import ControlIntent, ControlPowerDef, NumberWritableDef

_LOGGER = logging.getLogger(__name__)

# Ranges wider than this get a text box; a slider over tens of kilowatts is unusable.
SLIDER_MAX_RANGE = 1000


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Automatically set up number entities from the WRITABLE_NUMBERS_MAP configuration list."""
    coordinator: EcoflowCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[NumberEntity] = [
        EcoFlowControlPowerNumber(coordinator, entry, CONTROL_POWER_NUMBER)
    ]
    entities.extend(
        EcoFlowGenericNumber(coordinator, entry, number_def)
        for number_def in WRITABLE_NUMBERS_MAP
    )

    async_add_entities(entities)


class EcoFlowControlPowerNumber(EcoFlowBaseEntity, NumberEntity):
    """The single power target for whichever control mode is selected.

    There is one of these rather than one per setpoint register, so a value can
    never be written to a register the active control method is ignoring. The
    ceiling follows the mode and comes from the device's own limits.
    """

    _attr_native_min_value = 0.0
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: ControlPowerDef,
    ) -> None:
        super().__init__(coordinator, entry, definition)
        self._attr_native_step = definition.step
        self._attr_native_unit_of_measurement = definition.unit
        self._attr_device_class = definition.device_class
        self._attr_entity_category = definition.entity_category
        if definition.icon:
            self._attr_icon = definition.icon

    @property
    def available(self) -> bool:
        # Nothing to target unless Modbus control is held and a mode is selected.
        return (
            super().available
            and self.coordinator.heartbeat_enabled
            and self.coordinator.control_intent is not ControlIntent.AUTOMATIC
        )

    @property
    def native_max_value(self) -> float:
        return self.coordinator.control_power_max

    @property
    def native_value(self) -> float:
        # The ceiling is read live, so it can drop below what was commanded.
        return min(self.coordinator.control_power, self.coordinator.control_power_max)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "control_mode": str(self.coordinator.control_intent),
            # The device slews at roughly 1.5 kW/min, so measured power lags this.
            "in_control": self.coordinator.in_control,
        }

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_control_power(value)


class EcoFlowGenericNumber(EcoFlowBaseEntity, NumberEntity):
    """Generic configuration entity dynamically driven by NumberWritableDef specifications."""

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: NumberWritableDef,
    ) -> None:
        """Initialize the generic number entity."""
        super().__init__(coordinator, entry, definition)

        # Track the last written value to prevent redundant state updates
        self._last_written_value: float | None = None

        # Raw register access the control mode already covers.
        self._attr_entity_registry_enabled_default = not definition.advanced

        # Configure native Home Assistant number attributes
        self._attr_native_min_value = definition.min_value
        self._attr_native_max_value = definition.max_value
        self._attr_native_step = definition.step
        self._attr_native_unit_of_measurement = definition.unit
        self._attr_device_class = definition.device_class
        self._attr_mode = (
            NumberMode.BOX
            if definition.max_value - definition.min_value > SLIDER_MAX_RANGE
            else NumberMode.SLIDER
        )

        # Categorize writeable management controls into the config section of the UI
        self._attr_entity_category = EntityCategory.CONFIG

        if definition.icon:
            self._attr_icon = definition.icon

    async def async_added_to_hass(self) -> None:
        """Initialize the initial value from coordinator data when entity is added to Home Assistant."""
        await super().async_added_to_hass()
        # Pre-populate native value from current coordinator data state to prevent 0 on startup
        initial_value = self.native_value
        if initial_value is not None:
            self._last_written_value = initial_value

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator efficiently."""
        new_value = self.native_value
        if new_value != self._last_written_value:
            self._last_written_value = new_value
            self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Dynamically fetch the active numerical value cached inside the coordinator data block by its read_key."""
        if self.coordinator.data is not None and isinstance(
            self.coordinator.data, dict
        ):
            # Read directly using the original sensor key defined in WRITABLE_NUMBERS_MAP
            val = self.coordinator.data.get(self._definition.read_key, None)
            if val is not None:
                return float(val)
        return self._last_written_value

    async def async_set_native_value(self, value: float) -> None:
        """Set new value asynchronously (overrides NumberEntity abstract method)."""
        await self.coordinator.async_write_modbus_register(
            entity_def=self._definition,
            value=int(round(value)),
        )
