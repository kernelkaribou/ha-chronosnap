"""The ChronoSnap integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ChronoSnapClient, ChronoSnapError
from .const import (
    CONF_API_KEY,
    CONF_INSTANCE_NAME,
    CONF_PROFILES,
    CONF_URL,
    DOMAIN,
    SERVER_STATS_POLL_INTERVAL,
)
from .coordinator import ProfileCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ChronoSnap from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    session = async_get_clientsession(hass)
    client = ChronoSnapClient(
        url=entry.data[CONF_URL],
        api_key=entry.data[CONF_API_KEY],
        session=session,
    )

    coordinator = ProfileCoordinator(hass, client, entry.entry_id)

    # Load persisted active job state
    await coordinator.async_load()

    # Set up state listeners for all configured profiles
    profiles: dict[str, dict[str, Any]] = entry.options.get(CONF_PROFILES, {})
    coordinator.setup_listeners(profiles)

    # Set up server stats polling
    stats_coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_server_stats",
        update_method=lambda: _async_fetch_server_stats(client),
        update_interval=timedelta(seconds=SERVER_STATS_POLL_INTERVAL),
    )
    await stats_coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "stats_coordinator": stats_coordinator,
        "client": client,
    }

    # Clean up entity registry for removed profiles
    _cleanup_stale_entities(hass, entry, profiles)

    # Set up sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Re-setup listeners when options change
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_fetch_server_stats(
    client: ChronoSnapClient,
) -> dict[str, Any]:
    """Fetch server stats from ChronoSnap API."""
    try:
        storage = await client.get_storage_stats()
        all_jobs = await client.get_jobs()
        active_jobs = [j for j in all_jobs if j.get("status") in ("active", "sleeping")]
        return {
            "storage": storage,
            "total_jobs": len(all_jobs),
            "active_jobs": len(active_jobs),
        }
    except ChronoSnapError as err:
        raise UpdateFailed(f"Error fetching server stats: {err}") from err


def _cleanup_stale_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    profiles: dict[str, dict[str, Any]],
) -> None:
    """Remove entity and device registry entries for profiles that no longer exist."""
    # Clean up stale entities
    ent_registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(ent_registry, entry.entry_id)

    valid_unique_ids = set()
    for profile_id in profiles:
        valid_unique_ids.add(f"{DOMAIN}_{profile_id}_status")
        valid_unique_ids.add(f"{DOMAIN}_{profile_id}_captures")

    # Server stats sensor unique IDs
    for suffix in ("total_jobs", "active_jobs", "total_videos", "total_captures", "disk_free", "disk_used"):
        valid_unique_ids.add(f"{DOMAIN}_{entry.entry_id}_{suffix}")

    for entity_entry in entries:
        if entity_entry.unique_id not in valid_unique_ids:
            _LOGGER.info(
                "Removing stale entity %s (profile deleted)",
                entity_entry.entity_id,
            )
            ent_registry.async_remove(entity_entry.entity_id)

    # Clean up stale devices
    dev_registry = dr.async_get(hass)
    valid_device_ids = {
        (DOMAIN, f"{entry.entry_id}_{pid}") for pid in profiles
    }
    # Include the server device
    valid_device_ids.add((DOMAIN, f"{entry.entry_id}_server"))
    devices = dr.async_entries_for_config_entry(dev_registry, entry.entry_id)
    for device in devices:
        if not device.identifiers & valid_device_ids:
            _LOGGER.info(
                "Removing stale device %s (profile deleted)",
                device.name,
            )
            dev_registry.async_remove_device(device.id)


async def _async_update_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update — reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data[DOMAIN].pop(entry.entry_id)
    coordinator: ProfileCoordinator = data["coordinator"]
    coordinator.teardown_listeners()

    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    return unload_ok
