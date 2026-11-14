"""Exercise real HA config entries, platforms, services, events, and options."""

import asyncio
from copy import deepcopy
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant import bootstrap, config_entries, loader
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.chronosnap.api import ChronoSnapConnectionError

DOMAIN = "chronosnap"
TRIGGER = "binary_sensor.test_motion"
PROFILE = {
    "enabled": True,
    "name": "Motion",
    "stream_type": "http",
    "stream_url": "http://camera/image.jpg",
    "trigger_entity": TRIGGER,
    "active_state": "on",
    "exclude_states": "",
    "start_delay_seconds": 0,
    "debounce_seconds": 0,
}


async def lifecycle():
    client = SimpleNamespace(
        test_connection=AsyncMock(return_value={"version": "test"}),
        get_storage_stats=AsyncMock(return_value={}),
        get_jobs=AsyncMock(return_value=[]),
        get_tags=AsyncMock(return_value=[]),
        create_job=AsyncMock(side_effect=[{"id": n} for n in range(1, 10)]),
        complete_job=AsyncMock(return_value={"status": "completed"}),
        create_video=AsyncMock(side_effect=[{"id": n} for n in range(11, 20)]),
        get_job=AsyncMock(return_value={"status": "active"}),
        get_video=AsyncMock(return_value={"status": "processing"}),
        get_capture_count=AsyncMock(return_value=4),
        delete_job=AsyncMock(),
    )
    with (
        TemporaryDirectory() as config_dir,
        patch("custom_components.chronosnap.ChronoSnapClient", return_value=client),
        patch(
            "custom_components.chronosnap.config_flow.ChronoSnapClient",
            return_value=client,
        ),
        patch(
            "custom_components.chronosnap.async_get_clientsession", return_value=None
        ),
        patch(
            "custom_components.chronosnap.config_flow.async_get_clientsession",
            return_value=None,
        ),
    ):
        hass = HomeAssistant(config_dir)
        loader.async_setup(hass)
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        try:
            if load_base := getattr(bootstrap, "async_load_base_functionality", None):
                assert await load_base(hass)
            else:
                await hass.config_entries.async_initialize()
                await bootstrap.load_registries(hass)
            await hass.async_start()
            hass.states.async_set(TRIGGER, "on")
            with patch.object(
                hass.config_entries,
                "async_reload",
                wraps=hass.config_entries.async_reload,
            ) as reload_entry:
                result = await hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": "user"},
                    data={"url": "http://chronosnap", "api_key": "test-key"},
                )
                assert result["type"] == "create_entry", result
                entry = result["result"]
                assert entry.state is config_entries.ConfigEntryState.LOADED, (
                    entry.state
                )
                options = hass.config_entries.options
                result = await options.async_init(entry.entry_id)
                flow_id = result["flow_id"]
                await options.async_configure(flow_id, {"action": "add"})
                result = await options.async_configure(flow_id, dict(PROFILE))
                assert result["step_id"] == "profile_capture", result
                result = await options.async_configure(
                    flow_id, {"interval_mode": "fixed", "resolution": "1920x1080"}
                )
                assert result["type"] == "create_entry", result
                await hass.async_block_till_done()
                profile_id = next(iter(entry.options["profiles"]))
                coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
                assert coordinator._running and coordinator._ready
                client.create_job.assert_not_awaited()

                def entity(domain, suffix):
                    unique_id = f"{DOMAIN}_{entry.entry_id}_{profile_id}_{suffix}"
                    entity_id = er.async_get(hass).async_get_entity_id(
                        domain, DOMAIN, unique_id
                    )
                    assert entity_id is not None, unique_id
                    return entity_id

                async def service(domain, action, suffix):
                    await hass.services.async_call(
                        domain,
                        action,
                        {"entity_id": entity(domain, suffix)},
                        blocking=True,
                    )
                    await hass.async_block_till_done()

                await service("switch", "turn_off", "enabled")
                await service("switch", "turn_on", "enabled")
                assert hass.data[DOMAIN][entry.entry_id]["coordinator"] is coordinator
                assert reload_entry.await_count == 1
                client.create_job.assert_not_awaited()
                await service("button", "press", "start_now")
                client.create_job.assert_awaited_once()

                registry = dr.async_get(hass)
                device = next(
                    device
                    for device in dr.async_entries_for_config_entry(
                        registry, entry.entry_id
                    )
                    if (DOMAIN, f"{entry.entry_id}_{profile_id}") in device.identifiers
                )
                server = registry.async_get(device.via_device_id)
                assert (DOMAIN, f"{entry.entry_id}_server") in server.identifiers
                registry.async_update_device(
                    device.id, disabled_by=dr.DeviceEntryDisabler.USER
                )
                await hass.async_block_till_done()
                assert not coordinator.is_enabled(profile_id)
                client.complete_job.assert_awaited_once()
                assert coordinator.recordings[profile_id].phase == "building"
                registry.async_update_device(device.id, disabled_by=None)
                await hass.async_block_till_done()
                assert await hass.config_entries.async_reload(entry.entry_id)
                await hass.async_block_till_done()
                coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
                assert coordinator.recordings[profile_id].video_id == 11
                client.create_job.assert_awaited_once()
                client.create_video.assert_awaited_once()

                client.get_video.return_value = {"status": "failed"}
                await coordinator._async_poll()
                assert coordinator.recordings[profile_id].phase == "failed"
                client.delete_job.assert_not_awaited()
                await service("button", "press", "retry")
                assert client.create_video.await_count == 2
                client.get_video.return_value = {"status": "completed"}
                await coordinator._async_poll()
                assert profile_id not in coordinator.recordings
                client.delete_job.assert_awaited_once_with(1)

                await service("button", "press", "start_now")
                before = client.complete_job.await_count
                coordinator.STOP_RETRY_DELAY = 0
                client.complete_job.side_effect = ChronoSnapConnectionError("offline")
                try:
                    await service("switch", "turn_off", "enabled")
                except HomeAssistantError:
                    pass
                else:
                    raise AssertionError("A failed stop must be reported to the caller")
                await hass.async_block_till_done()
                assert (
                    client.complete_job.await_count - before
                    == coordinator.STOP_MAX_RETRIES
                )
                assert not entry.options["profiles"][profile_id]["enabled"]
                assert hass.data[DOMAIN][entry.entry_id]["coordinator"] is coordinator
                client.complete_job.side_effect = None
                await coordinator._async_poll()
                await coordinator._async_poll()
                assert profile_id not in coordinator.recordings

                await service("switch", "turn_on", "enabled")
                assert client.create_job.await_count == 2
                hass.states.async_set(TRIGGER, "off")
                await hass.async_block_till_done()
                hass.states.async_set(TRIGGER, "on")
                await hass.async_block_till_done()
                assert client.create_job.await_count == 3

                client.get_video.return_value = {"status": "processing"}
                before = client.complete_job.await_count
                assert await hass.config_entries.async_set_disabled_by(
                    entry.entry_id, config_entries.ConfigEntryDisabler.USER
                )
                await hass.async_block_till_done()
                assert entry.entry_id not in hass.data[DOMAIN]
                assert client.complete_job.await_count == before + 1
                assert await hass.config_entries.async_set_disabled_by(
                    entry.entry_id, None
                )
                await hass.async_block_till_done()
                coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
                assert coordinator.recordings[profile_id].job_id == 3
                assert coordinator.recordings[profile_id].phase == "building"
                assert client.create_job.await_count == 3

                result = await options.async_init(entry.entry_id)
                flow_id = result["flow_id"]
                await options.async_configure(flow_id, {"action": profile_id})
                await options.async_configure(flow_id, {"action": "edit"})
                edited = deepcopy(PROFILE)
                edited["name"] = "Motion edited"
                await options.async_configure(flow_id, edited)
                client.get_video.return_value = {"status": "processing"}
                await service("switch", "turn_off", "enabled")
                result = await options.async_configure(
                    flow_id, {"interval_mode": "fixed", "resolution": "1920x1080"}
                )
                assert result.get("step_id") == "profile_enabled_conflict", result
                result = await options.async_configure(flow_id, {"enabled": False})
                assert result["type"] == "create_entry", result
                await hass.async_block_till_done()
                assert entry.options["profiles"][profile_id]["name"] == "Motion edited"
                assert not entry.options["profiles"][profile_id]["enabled"]
                coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
                assert coordinator.recordings[profile_id].job_id == 3
                assert client.create_job.await_count == 3

                with patch.object(
                    persistent_notification,
                    "async_create",
                    wraps=persistent_notification.async_create,
                ) as notify:
                    removed = await hass.config_entries.async_remove(entry.entry_id)
                    await hass.async_block_till_done()
                    assert not removed["require_restart"], removed
                    notify.assert_called_once()
                    assert "Job 3" in notify.call_args.args[1]
                assert entry.entry_id not in hass.data[DOMAIN]
                assert not coordinator._listeners
        finally:
            await hass.async_stop()
    print(
        "Real HA config-entry, platform, service, device, options, reload, and removal checks passed"
    )


if __name__ == "__main__":
    asyncio.run(lifecycle())
