"""Shared profile device identity and push-update subscriptions."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import CONF_INSTANCE_NAME, CONF_PROFILE_NAME, CONF_URL, DOMAIN
from .coordinator import ProfileCoordinator


def server_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Keep platform setup order from affecting the profile's parent device."""
    instance_name = entry.data.get(CONF_INSTANCE_NAME, "ChronoSnap")
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_server")},
        name=f"{instance_name} Server",
        manufacturer="ChronoSnap",
        model="Server",
        configuration_url=entry.data.get(CONF_URL),
    )


class ChronoSnapProfileEntity(Entity):
    """A local profile entity that does not depend on sensor polling."""

    _attr_has_entity_name = True
    _attr_should_poll = False

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
        parent_identifier = (DOMAIN, f"{entry.entry_id}_server")
        parent: dict[str, Any] = {}
        if "via_device_id" in getattr(DeviceInfo, "__annotations__", {}):
            server = next(
                (
                    device
                    for device in dr.async_entries_for_config_entry(
                        dr.async_get(coordinator.hass), entry.entry_id
                    )
                    if parent_identifier in device.identifiers
                ),
                None,
            )
            if server is not None:
                parent["via_device_id"] = server.id
        else:
            parent["via_device"] = parent_identifier
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{profile_id}")},
            name=profile.get(CONF_PROFILE_NAME, profile_id),
            manufacturer="ChronoSnap",
            model="Timelapse Profile",
            configuration_url=entry.data.get(CONF_URL),
            **parent,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.register_update_callback(self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
