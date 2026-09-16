"""Select entities for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BATTERY_MODE_SELECT, DOMAIN, SERVICE_SET_CONTROL
from .coordinator import EcoflowCoordinator
from .entity import EcoFlowBaseEntity
from .models import ControlEntityDef, ControlFeature
from .services import (
    SET_CONTROL_SCHEMA,
    PendingRevert,
    async_set_control_service,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow selects from a config entry."""
    coordinator: EcoflowCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [EcoFlowBatteryModeSelect(coordinator, entry, BATTERY_MODE_SELECT)]
    )

    # An entity service, so a second inverter is targeted like any other control.
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_CONTROL,
        SET_CONTROL_SCHEMA,
        "async_set_control_service",
    )


class EcoFlowBatteryModeSelect(EcoFlowBaseEntity, SelectEntity):
    """What the inverter should be doing.

    The protocol follows one control method at a time, so this is a single choice
    rather than several toggles. Each mode's power and the two state-of-charge
    limits are separate entities that stay editable whatever is selected here.
    """

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: ControlEntityDef,
    ) -> None:
        super().__init__(coordinator, entry, definition)
        self._attr_options = [str(feature) for feature in ControlFeature]
        self._attr_entity_category = definition.entity_category
        if definition.icon:
            self._attr_icon = definition.icon
        self.pending_revert: PendingRevert | None = None

    @property
    def current_option(self) -> str:
        return str(self.coordinator.control.selected_feature)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes: dict[str, Any] = {
            "status": str(self.coordinator.control.status),
            "commanded_power": self.coordinator.control.power,
        }
        if self.pending_revert is not None and self.pending_revert.deadline is not None:
            attributes["revert_at"] = self.pending_revert.deadline.isoformat()
        return attributes

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.control.async_select_feature(ControlFeature(option))

    async def async_set_control_service(self, **kwargs) -> None:
        """Handle ef_powerocean_tcpmodbus.set_control."""
        await async_set_control_service(self, **kwargs)

    async def async_will_remove_from_hass(self) -> None:
        """Drop a running window; nothing about it survives a reload."""
        if self.pending_revert is not None:
            self.pending_revert.cancel()
            self.pending_revert = None
        await super().async_will_remove_from_hass()
