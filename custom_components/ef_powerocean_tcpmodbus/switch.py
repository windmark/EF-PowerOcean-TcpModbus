"""Switch entities for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONTROL_FEATURES, DOMAIN, POWER_SAVING_SWITCH
from .coordinator import EcoflowCoordinator
from .entity import EcoFlowBaseEntity
from .models import ControlFeature, FeatureEntityDef, SwitchDef

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow switches from a config entry."""
    coordinator: EcoflowCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SwitchEntity] = [
        EcoFlowPowerSavingSwitch(coordinator, entry, POWER_SAVING_SWITCH)
    ]
    entities.extend(
        EcoFlowFeatureSwitch(
            coordinator,
            entry,
            FeatureEntityDef(key=str(feature), feature=feature, icon=definition.icon),
        )
        for feature, definition in CONTROL_FEATURES.items()
        if feature is not ControlFeature.AUTOMATIC
    )
    async_add_entities(entities)


class EcoFlowSwitch(EcoFlowBaseEntity, SwitchEntity):
    """Shared setup for the integration's switches."""

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: SwitchDef | FeatureEntityDef,
    ) -> None:
        super().__init__(coordinator, entry, definition)
        self._attr_device_class = getattr(definition, "device_class", None)
        self._attr_entity_category = definition.entity_category
        if definition.icon:
            self._attr_icon = definition.icon


class EcoFlowFeatureSwitch(EcoFlowSwitch):
    """Switches one feature on, turning off whichever was on before.

    The inverter follows a single control method, so these are exclusive. Being on
    is not the same as acting: a feature that has met its SOC target stays on and
    engages by itself when the state of charge moves back. The state attribute says
    which of those is happening.
    """

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: FeatureEntityDef,
    ) -> None:
        super().__init__(coordinator, entry, definition)
        self._feature = definition.feature

    @property
    def is_on(self) -> bool:
        return self.coordinator.selected_feature is self._feature

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        definition = CONTROL_FEATURES[self._feature]
        attributes: dict[str, Any] = {
            "state": str(self.coordinator.feature_state(self._feature)),
        }
        if definition.has_power:
            attributes["power"] = self.coordinator.feature_power(self._feature)
        if definition.has_target_soc:
            attributes["target_soc"] = self.coordinator.feature_target_soc(
                self._feature
            )
        return attributes

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_select_feature(self._feature)

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self.is_on:
            await self.coordinator.async_select_feature(ControlFeature.AUTOMATIC)


class EcoFlowPowerSavingSwitch(EcoFlowSwitch):
    """Power-saving mode, bit 3 of the write-only control command.

    The coordinator composes the control word from this bit and whichever feature
    is selected, so toggling here never disturbs the feature.
    """

    @property
    def is_on(self) -> bool:
        # System Modes bit 3 is a status, not an echo: it only rises once the
        # inverter has actually gone idle, so it cannot confirm the command.
        return self.coordinator.power_saving_commanded

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "device_reports_low_power": data.get("battery_saver_mode_ena"),
            "commanded_word": f"0x{self.coordinator.control_command:08X}",
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_power_saving(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_power_saving(False)
