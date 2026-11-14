"""Persistent, automatable controls for individual profiles."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENABLED, CONF_PROFILES, DOMAIN
from .coordinator import ProfileCoordinator
from .entity import ChronoSnapProfileEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        ChronoSnapProfileSwitch(coordinator, entry, pid, profile)
        for pid, profile in entry.options.get(CONF_PROFILES, {}).items()
    )


class ChronoSnapProfileSwitch(ChronoSnapProfileEntity, SwitchEntity):
    """Arming a profile never implicitly starts an already-active process."""

    _attr_name = "Automatic timelapse"
    _attr_icon = "mdi:camera-timer"
    _attr_translation_key = "automatic_timelapse"

    def __init__(
        self,
        coordinator: ProfileCoordinator,
        entry: ConfigEntry,
        profile_id: str,
        profile: dict[str, Any],
    ) -> None:
        super().__init__(coordinator, entry, profile_id, profile)
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{profile_id}_enabled"

    @property
    def is_on(self) -> bool:
        return self._coordinator.profiles[self._profile_id].get(CONF_ENABLED, True)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_enabled(self._profile_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_enabled(self._profile_id, False)
