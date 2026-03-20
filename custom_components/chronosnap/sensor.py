"""Sensor entities for ChronoSnap timelapse profiles and server stats."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfInformation
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import (
    CONF_ACTIVE_STATE,
    CONF_INSTANCE_NAME,
    CONF_PROFILE_NAME,
    CONF_PROFILES,
    CONF_STREAM_URL,
    CONF_TRIGGER_ENTITY,
    CONF_URL,
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


def _server_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Build DeviceInfo for the ChronoSnap server."""
    instance_name = entry.data.get(CONF_INSTANCE_NAME, "ChronoSnap")
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_server")},
        name=f"{instance_name} Server",
        manufacturer="ChronoSnap",
        model="Server",
        configuration_url=entry.data.get(CONF_URL),
        entry_type=None,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ChronoSnap sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: ProfileCoordinator = data["coordinator"]
    stats_coordinator: DataUpdateCoordinator = data["stats_coordinator"]
    profiles: dict[str, dict[str, Any]] = entry.options.get(CONF_PROFILES, {})

    entities: list[SensorEntity] = []

    # Profile sensors
    for profile_id, profile in profiles.items():
        entities.append(
            ChronoSnapStatusSensor(coordinator, entry, profile_id, profile)
        )
        entities.append(
            ChronoSnapCaptureCountSensor(coordinator, entry, profile_id, profile)
        )

    # Server stats sensors
    entities.append(ChronoSnapTotalJobsSensor(stats_coordinator, entry))
    entities.append(ChronoSnapActiveJobsSensor(stats_coordinator, entry))
    entities.append(ChronoSnapTotalVideosSensor(stats_coordinator, entry))
    entities.append(ChronoSnapTotalCapturesSensor(stats_coordinator, entry))
    entities.append(ChronoSnapDiskFreeSensor(stats_coordinator, entry))
    entities.append(ChronoSnapDiskUsedSensor(stats_coordinator, entry))

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


# ── Server stats sensors ────────────────────────────────────────


def _bytes_to_gb(value: int | None) -> float | None:
    """Convert bytes to gigabytes."""
    if value is None:
        return None
    return round(value / (1024 ** 3), 2)


class ChronoSnapServerSensor(CoordinatorEntity, SensorEntity):
    """Base class for server-level sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = _server_device_info(entry)


class ChronoSnapTotalJobsSensor(ChronoSnapServerSensor):
    """Total number of jobs on the ChronoSnap server."""

    _attr_icon = "mdi:briefcase-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_total_jobs"
        self._attr_name = "Total Jobs"

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data:
            return self.coordinator.data.get("total_jobs")
        return None


class ChronoSnapActiveJobsSensor(ChronoSnapServerSensor):
    """Number of active jobs on the ChronoSnap server."""

    _attr_icon = "mdi:briefcase-check"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_active_jobs"
        self._attr_name = "Active Jobs"

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data:
            return self.coordinator.data.get("active_jobs")
        return None


class ChronoSnapTotalVideosSensor(ChronoSnapServerSensor):
    """Total number of videos on the ChronoSnap server."""

    _attr_icon = "mdi:movie-open-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_total_videos"
        self._attr_name = "Total Videos"

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data:
            storage = self.coordinator.data.get("storage", {})
            return storage.get("videos_total_count")
        return None


class ChronoSnapTotalCapturesSensor(ChronoSnapServerSensor):
    """Total number of captures on the ChronoSnap server."""

    _attr_icon = "mdi:image-multiple-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_total_captures"
        self._attr_name = "Total Captures"

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data:
            storage = self.coordinator.data.get("storage", {})
            return storage.get("captures_total_count")
        return None


class ChronoSnapDiskFreeSensor(ChronoSnapServerSensor):
    """Free disk space on the ChronoSnap server."""

    _attr_icon = "mdi:harddisk"
    _attr_native_unit_of_measurement = UnitOfInformation.GIGABYTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_disk_free"
        self._attr_name = "Disk Free"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data:
            storage = self.coordinator.data.get("storage", {})
            return _bytes_to_gb(storage.get("disk_free"))
        return None


class ChronoSnapDiskUsedSensor(ChronoSnapServerSensor):
    """Used disk space on the ChronoSnap server."""

    _attr_icon = "mdi:harddisk"
    _attr_native_unit_of_measurement = UnitOfInformation.GIGABYTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_disk_used"
        self._attr_name = "Disk Used"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data:
            storage = self.coordinator.data.get("storage", {})
            return _bytes_to_gb(storage.get("disk_used"))
        return None
