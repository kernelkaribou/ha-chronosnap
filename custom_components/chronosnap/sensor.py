"""Sensor entities for ChronoSnap timelapse profiles."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ACTIVE_STATE,
    CONF_PROFILE_NAME,
    CONF_PROFILES,
    CONF_STREAM_URL,
    CONF_TRIGGER_ENTITY,
    DOMAIN,
    STATUS_BUILDING,
    STATUS_CAPTURING,
    STATUS_ERROR,
    STATUS_IDLE,
)
from .coordinator import ProfileCoordinator

_LOGGER = logging.getLogger(__name__)

STATUS_ICONS = {
    STATUS_IDLE: "mdi:camera-off",
    STATUS_CAPTURING: "mdi:camera",
    STATUS_BUILDING: "mdi:movie-open",
    STATUS_ERROR: "mdi:alert-circle",
}


def _device_info(entry: ConfigEntry, profile_id: str, profile: dict[str, Any]) -> DeviceInfo:
    """Build DeviceInfo for a timelapse profile."""
    name = profile.get(CONF_PROFILE_NAME, profile_id)
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{profile_id}")},
        name=name,
        manufacturer="ChronoSnap",
        model="Timelapse Profile",
        configuration_url=entry.data.get("url"),
        entry_type=None,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ChronoSnap sensors from a config entry."""
    coordinator: ProfileCoordinator = hass.data[DOMAIN][entry.entry_id]
    profiles: dict[str, dict[str, Any]] = entry.options.get(CONF_PROFILES, {})

    entities: list[SensorEntity] = []
    for profile_id, profile in profiles.items():
        entities.append(
            ChronoSnapStatusSensor(coordinator, entry, profile_id, profile)
        )
        entities.append(
            ChronoSnapCaptureCountSensor(coordinator, entry, profile_id, profile)
        )

    async_add_entities(entities)


class ChronoSnapStatusSensor(SensorEntity):
    """Sensor showing the current status of a timelapse profile."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ProfileCoordinator,
        entry: ConfigEntry,
        profile_id: str,
        profile: dict[str, Any],
    ) -> None:
        self._coordinator = coordinator
        self._profile_id = profile_id
        self._profile = profile

        self._attr_unique_id = f"{DOMAIN}_{profile_id}_status"
        self._attr_name = "Status"
        self._attr_icon = STATUS_ICONS.get(STATUS_IDLE, "mdi:camera")
        self._attr_device_info = _device_info(entry, profile_id, profile)

    @property
    def native_value(self) -> str:
        """Return the current profile status."""
        return self._coordinator.profile_status.get(
            self._profile_id, STATUS_IDLE
        )

    @property
    def icon(self) -> str:
        """Return icon based on status."""
        status = self.native_value
        return STATUS_ICONS.get(status, "mdi:camera")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        job_id = self._coordinator.active_jobs.get(self._profile_id)
        return {
            "profile_id": self._profile_id,
            "trigger_entity": self._profile.get(CONF_TRIGGER_ENTITY),
            "active_state": self._profile.get(CONF_ACTIVE_STATE),
            "active_job_id": job_id,
        }

    async def async_added_to_hass(self) -> None:
        """Register update callback."""
        self._coordinator.register_update_callback(
            self._handle_coordinator_update
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unregister update callback."""
        self._coordinator.unregister_update_callback(
            self._handle_coordinator_update
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from coordinator."""
        self.async_write_ha_state()


class ChronoSnapCaptureCountSensor(SensorEntity):
    """Sensor showing the capture count for the active job."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ProfileCoordinator,
        entry: ConfigEntry,
        profile_id: str,
        profile: dict[str, Any],
    ) -> None:
        self._coordinator = coordinator
        self._profile_id = profile_id
        self._profile = profile

        self._attr_unique_id = f"{DOMAIN}_{profile_id}_captures"
        self._attr_name = "Captures"
        self._attr_icon = "mdi:image-multiple"
        self._attr_native_unit_of_measurement = "frames"
        self._attr_device_info = _device_info(entry, profile_id, profile)

    @property
    def native_value(self) -> int:
        """Return the current capture count."""
        return self._coordinator.capture_counts.get(self._profile_id, 0)

    @property
    def available(self) -> bool:
        """Only available when actively capturing."""
        return self._profile_id in self._coordinator.active_jobs

    async def async_added_to_hass(self) -> None:
        """Register update callback."""
        self._coordinator.register_update_callback(
            self._handle_coordinator_update
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unregister update callback."""
        self._coordinator.unregister_update_callback(
            self._handle_coordinator_update
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from coordinator."""
        self.async_write_ha_state()
