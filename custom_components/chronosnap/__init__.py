"""The ChronoSnap integration."""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ChronoSnapAuthError, ChronoSnapClient, ChronoSnapError
from .const import (
    CONF_API_KEY,
    CONF_ENABLED,
    CONF_PROFILES,
    CONF_URL,
    DOMAIN,
    SERVER_STATS_POLL_INTERVAL,
)
from .coordinator import ProfileCoordinator
from .entity import server_device_info

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ChronoSnap from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    session = async_get_clientsession(hass)
    client = ChronoSnapClient(
        url=entry.data[CONF_URL],
        api_key=entry.data[CONF_API_KEY],
        session=session,
    )

    coordinator = ProfileCoordinator(hass, client, entry)
    profiles: dict[str, dict[str, Any]] = entry.options.get(CONF_PROFILES, {})

    # Set up server stats polling
    stats_coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_server_stats",
        update_method=lambda: _async_fetch_server_stats(client),
        update_interval=timedelta(seconds=SERVER_STATS_POLL_INTERVAL),
    )
    await stats_coordinator.async_config_entry_first_refresh()
    await coordinator.async_load()
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **server_device_info(entry)
    )

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "stats_coordinator": stats_coordinator,
        "client": client,
    }

    # Clean up entity registry for removed profiles
    _cleanup_stale_entities(hass, entry, profiles)

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        await coordinator.async_start()
    except BaseException:
        await coordinator.async_shutdown()
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_fetch_server_stats(
    client: ChronoSnapClient,
) -> dict[str, Any]:
    """Fetch server stats from ChronoSnap API."""
    try:
        storage = await client.get_storage_stats()
        all_jobs = await client.get_jobs()
        active_jobs = [
            j for j in all_jobs if j.get("status") in ("active", "sleeping", "warning")
        ]
        return {
            "storage": storage,
            "total_jobs": len(all_jobs),
            "active_jobs": len(active_jobs),
        }
    except ChronoSnapAuthError as err:
        raise ConfigEntryAuthFailed("ChronoSnap API key is no longer valid") from err
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
        for suffix in ("enabled", "start_now", "retry"):
            valid_unique_ids.add(f"{DOMAIN}_{entry.entry_id}_{profile_id}_{suffix}")

    # Server stats sensor unique IDs
    for suffix in (
        "total_jobs",
        "active_jobs",
        "total_videos",
        "total_captures",
        "disk_free",
        "disk_used",
    ):
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
    valid_device_ids = {(DOMAIN, f"{entry.entry_id}_{pid}") for pid in profiles}
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


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply control changes in place; reload only for structural edits."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if data is None:
        return
    coordinator: ProfileCoordinator = data["coordinator"]
    profiles = entry.options.get(CONF_PROFILES, {})
    old = deepcopy(coordinator.profiles)
    new = deepcopy(profiles)
    for collection in (old, new):
        for profile in collection.values():
            profile.pop(CONF_ENABLED, None)
    if old == new:
        await coordinator.async_apply_enabled(profiles)
    else:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN][entry.entry_id]
        await data["coordinator"].async_shutdown(
            stop_jobs=entry.disabled_by is not None
        )
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Stop tracked captures even when removing an already-unloaded entry."""
    client = ChronoSnapClient(
        entry.data[CONF_URL],
        entry.data[CONF_API_KEY],
        session=async_get_clientsession(hass),
    )
    coordinator = ProfileCoordinator(hass, client, entry)
    await coordinator.async_load()
    await coordinator.async_prepare_removal()
    if coordinator.recordings or any(coordinator.failed_recordings.values()):
        recordings = [
            *coordinator.recordings.values(),
            *(
                recording
                for records in coordinator.failed_recordings.values()
                for recording in records
            ),
        ]
        details = "\n".join(
            f"- Job {recording.job_id}, video "
            f"{recording.video_id if recording.video_id is not None else 'not created'}, "
            f"phase: {recording.phase}"
            for recording in recordings
        )
        persistent_notification.async_create(
            hass,
            "ChronoSnap was removed with unfinished recordings. Automatic recovery "
            "and cleanup have stopped. Open your ChronoSnap server to verify capture "
            "has stopped, finish any video builds, and review source frames before "
            "deleting them. Recovery metadata has been retained in Home Assistant's "
            "storage; reinstalling creates a new entry and does not automatically "
            "resume this removed entry.\n\n"
            f"{details}",
            title="ChronoSnap recordings need attention",
            notification_id=f"{DOMAIN}_{entry.entry_id}_unfinished",
        )
        _LOGGER.warning(
            "ChronoSnap was removed with unfinished recordings. Their recovery "
            "state is retained; inspect and finish the jobs in ChronoSnap."
        )
