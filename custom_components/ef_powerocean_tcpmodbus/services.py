"""The set_control service: one command, optionally held for a while.

Nothing here is persisted. The control manager never restores the selected mode
across a restart, so a window that outlives Home Assistant is moot, and the
heartbeat hands control back to the app regardless.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import condition
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_BATTERY_RESERVE_SOC,
    ATTR_CHARGE_LIMIT_SOC,
    ATTR_DURATION,
    ATTR_MODE,
    ATTR_POWER,
    ATTR_REVERT_TO,
    ATTR_UNTIL,
    ATTR_UNTIL_MODE,
    REVERT_MAX_DURATION_S,
    UNTIL_MODE_ALL,
    UNTIL_MODE_ANY,
    UNTIL_MODES,
)
from .models import ControlFeature

if TYPE_CHECKING:
    from .coordinator import EcoflowCoordinator
    from .select import EcoFlowBatteryModeSelect

_LOGGER = logging.getLogger(__name__)

FEATURE_OPTIONS = [str(feature) for feature in ControlFeature]

SET_CONTROL_SCHEMA: dict[Any, Any] = {
    vol.Required(ATTR_MODE): vol.In(FEATURE_OPTIONS),
    vol.Optional(ATTR_POWER): vol.All(vol.Coerce(float), vol.Range(min=0)),
    vol.Optional(ATTR_CHARGE_LIMIT_SOC): vol.All(
        vol.Coerce(float), vol.Range(min=0, max=100)
    ),
    vol.Optional(ATTR_BATTERY_RESERVE_SOC): vol.All(
        vol.Coerce(float), vol.Range(min=0, max=100)
    ),
    vol.Optional(ATTR_DURATION): cv.positive_time_period,
    vol.Optional(ATTR_UNTIL): cv.CONDITION_SCHEMA,
    vol.Optional(ATTR_UNTIL_MODE, default=UNTIL_MODE_ANY): vol.In(UNTIL_MODES),
    vol.Optional(ATTR_REVERT_TO, default=str(ControlFeature.AUTOMATIC)): vol.In(
        FEATURE_OPTIONS
    ),
}


class PendingRevert:
    """Waits for the moment a commanded mode should be given up.

    Watches a deadline, the entities the condition mentions, and every coordinator
    poll. The poll is the backstop: a template condition names no entities to
    subscribe to.
    """

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        *,
        deadline: datetime | None,
        until_config: dict[str, Any] | None,
        until_mode: str,
        revert_to: ControlFeature,
    ) -> None:
        self._hass = coordinator.hass
        self._coordinator = coordinator
        self._control = coordinator.control
        self._deadline = deadline
        self._until_config = until_config
        self._until_mode = until_mode
        self._revert_to = revert_to

        self._checker: Callable[..., Any] | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._reverted = False

    @property
    def deadline(self) -> datetime | None:
        return self._deadline

    async def async_prepare(self) -> None:
        """Build the checker, so async_satisfied() can be asked before commanding."""
        if self._until_config is not None:
            self._checker = await condition.async_from_config(
                self._hass, self._until_config
            )

    async def async_satisfied(self) -> bool:
        """Return whether the command should now be given up."""
        terms: list[bool] = []
        if self._deadline is not None:
            terms.append(dt_util.utcnow() >= self._deadline)
        if self._checker is not None:
            terms.append(await self._async_condition_holds())
        if not terms:
            return False
        return any(terms) if self._until_mode == UNTIL_MODE_ANY else all(terms)

    async def _async_condition_holds(self) -> bool:
        """Evaluate the user's condition, treating an error as 'not yet'.

        An entity that has gone unavailable makes numeric_state raise rather than
        return False, and a command that ended because a sensor blinked would be
        worse than one that overran.
        """
        try:
            result = self._checker(self._hass, {})
            # Checkers are sync on some Home Assistant versions and async on others.
            if inspect.isawaitable(result):
                result = await result
        except HomeAssistantError as err:
            _LOGGER.debug(f"until condition could not be evaluated: {err!r}")
            return False
        return bool(result)

    async def async_start(self) -> None:
        """Subscribe to everything that could end the window."""
        if self._until_config is not None:
            entities = condition.async_extract_entities(self._until_config)
            if entities:
                self._unsubs.append(
                    async_track_state_change_event(
                        self._hass, list(entities), self._handle_change
                    )
                )
        if self._deadline is not None:
            self._unsubs.append(
                async_track_point_in_utc_time(
                    self._hass, self._handle_change, self._deadline
                )
            )
        # Backstop, and the retry path if the revert write itself fails.
        self._unsubs.append(self._coordinator.async_add_listener(self._handle_change))
        self._unsubs.append(self._control.call_when_superseded(self.cancel))

    @callback
    def _handle_change(self, *_args: Any) -> None:
        """Schedule a re-evaluation, whatever fired."""
        self._hass.async_create_task(self._async_reevaluate())

    async def _async_reevaluate(self) -> None:
        if self._reverted or not await self.async_satisfied():
            return

        # Set before commanding, so the revert does not supersede itself.
        self._reverted = True
        try:
            await self._control.async_set_control(self._revert_to)
        except HomeAssistantError as err:
            # Watchers stay in place, so the next poll retries.
            self._reverted = False
            _LOGGER.warning(
                f"Could not revert to {self._revert_to} yet, will retry: {err!r}"
            )
            return

        _LOGGER.debug(f"Timed command ended; reverted to {self._revert_to}")
        self.cancel()

    @callback
    def cancel(self) -> None:
        """Drop every subscription. Safe to call more than once."""
        while self._unsubs:
            self._unsubs.pop()()


def _validate(
    *,
    mode: str,
    duration: timedelta | None,
    until: dict[str, Any] | None,
    until_mode: str,
    revert_to: str,
) -> None:
    """Reject the combinations that would strand the inverter or never fire."""
    if duration is not None:
        if duration <= timedelta(0):
            raise ServiceValidationError("duration must be positive.")
        if duration > timedelta(seconds=REVERT_MAX_DURATION_S):
            raise ServiceValidationError(
                f"duration is capped at {REVERT_MAX_DURATION_S // 3600} hours. Use an "
                "automation if you need a mode to persist longer than that."
            )

    if until_mode == UNTIL_MODE_ALL and (duration is None or until is None):
        raise ServiceValidationError(
            "until_mode 'all' needs both duration and until, otherwise there is "
            "nothing for it to combine. Leave it at 'any', or supply both."
        )

    if mode == revert_to and (duration is not None or until is not None):
        _LOGGER.warning(
            f"revert_to is the same mode as mode ({mode}), so the window will end by "
            "re-sending the command it started with."
        )


async def async_set_control_service(
    entity: EcoFlowBatteryModeSelect,
    *,
    mode: str,
    power: float | None = None,
    charge_limit_soc: float | None = None,
    battery_reserve_soc: float | None = None,
    duration: timedelta | None = None,
    until: dict[str, Any] | None = None,
    until_mode: str = UNTIL_MODE_ANY,
    revert_to: str = str(ControlFeature.AUTOMATIC),
) -> None:
    """Command the inverter, and arrange for the command to end if asked."""
    coordinator = entity.coordinator

    _validate(
        mode=mode,
        duration=duration,
        until=until,
        until_mode=until_mode,
        revert_to=revert_to,
    )

    pending: PendingRevert | None = None
    if duration is not None or until is not None:
        pending = PendingRevert(
            coordinator,
            deadline=(dt_util.utcnow() + duration) if duration is not None else None,
            until_config=until,
            until_mode=until_mode,
            revert_to=ControlFeature(revert_to),
        )
        try:
            await pending.async_prepare()
        except (vol.Invalid, HomeAssistantError) as err:
            raise ServiceValidationError(
                f"until is not a valid condition: {err}"
            ) from err

        if await pending.async_satisfied():
            _LOGGER.info(
                f"set_control ignored: the end condition for {mode} already holds, so "
                "nothing was commanded."
            )
            return

    await coordinator.control.async_set_control(
        ControlFeature(mode),
        power=power,
        charge_limit_soc=charge_limit_soc,
        battery_reserve_soc=battery_reserve_soc,
    )

    entity.pending_revert = pending
    if pending is not None:
        await pending.async_start()
