"""Explicit start and recovery actions without editing a profile."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_PROFILES, DOMAIN
from .coordinator import ProfileCoordinator
from .entity import ChronoSnapProfileEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        entity(coordinator, entry, pid, profile)
        for pid, profile in entry.options.get(CONF_PROFILES, {}).items()
        for entity in (ChronoSnapStartButton, ChronoSnapRetryButton)
    )


class ChronoSnapStartButton(ChronoSnapProfileEntity, ButtonEntity):
    """Start a partial recording when the trigger is already active."""

    _attr_name = "Start now"
    _attr_icon = "mdi:camera-plus"
    _attr_translation_key = "start_now"

    def __init__(
        self,
        coordinator: ProfileCoordinator,
        entry: ConfigEntry,
        profile_id: str,
        profile: dict[str, Any],
    ) -> None:
        super().__init__(coordinator, entry, profile_id, profile)
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{profile_id}_start_now"

    async def async_press(self) -> None:
        await self._coordinator.async_start_now(self._profile_id)


class ChronoSnapRetryButton(ChronoSnapProfileEntity, ButtonEntity):
    """Recover a tracked recording; never create a new capture job."""

    _attr_name = "Retry recovery"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "retry_recovery"

    def __init__(
        self,
        coordinator: ProfileCoordinator,
        entry: ConfigEntry,
        profile_id: str,
        profile: dict[str, Any],
    ) -> None:
        super().__init__(coordinator, entry, profile_id, profile)
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{profile_id}_retry"

    async def async_press(self) -> None:
        await self._coordinator.async_retry(self._profile_id)
