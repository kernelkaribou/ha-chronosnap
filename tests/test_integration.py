"""Entry lifecycle, control-only updates, and registry compatibility."""

import asyncio
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from tests.helpers import (
    PROFILE,
    ConfigEntryAuthFailed,
    Entry,
    Hass,
    HomeAssistantError,
    api,
    buttons,
    client,
    integration,
    make_coordinator,
    sensors,
    switches,
)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_first_refresh_does_not_install_listeners(self):
        entry = Entry({"p": PROFILE})
        hass = Hass(entry)
        api_client = client()
        api_client.get_storage_stats.side_effect = api.ChronoSnapConnectionError()
        with patch.object(integration, "ChronoSnapClient", return_value=api_client):
            with self.assertRaises(integration.UpdateFailed):
                await integration.async_setup_entry(hass, entry)
        self.assertFalse(hass.bus.listeners)
        self.assertNotIn(entry.entry_id, hass.data["chronosnap"])

    async def test_setup_auth_error_requests_reauthentication(self):
        entry = Entry({"p": PROFILE})
        hass = Hass(entry)
        api_client = client()
        api_client.get_storage_stats.side_effect = api.ChronoSnapAuthError()
        with patch.object(integration, "ChronoSnapClient", return_value=api_client):
            with self.assertRaises(ConfigEntryAuthFailed):
                await integration.async_setup_entry(hass, entry)
        self.assertFalse(hass.bus.listeners)

    async def test_successful_setup_and_unload_release_listeners(self):
        entry = Entry({"p": PROFILE})
        hass = Hass(entry)
        with patch.object(integration, "ChronoSnapClient", return_value=client()):
            await integration.async_setup_entry(hass, entry)
        self.assertTrue(hass.bus.listeners)
        await integration.async_unload_entry(hass, entry)
        self.assertFalse(hass.bus.listeners)
        self.assertNotIn(entry.entry_id, hass.data["chronosnap"])

    async def test_platform_setup_failure_cleans_runtime_and_listeners(self):
        entry = Entry({"p": PROFILE})
        hass = Hass(entry)
        hass.config_entries.async_forward_entry_setups.side_effect = RuntimeError(
            "platform error"
        )
        with patch.object(integration, "ChronoSnapClient", return_value=client()):
            with self.assertRaises(RuntimeError):
                await integration.async_setup_entry(hass, entry)
        self.assertFalse(hass.bus.listeners)
        self.assertNotIn(entry.entry_id, hass.data["chronosnap"])

    async def test_failed_unload_keeps_runtime_and_listeners(self):
        entry = Entry({"p": PROFILE})
        hass = Hass(entry)
        with patch.object(integration, "ChronoSnapClient", return_value=client()):
            await integration.async_setup_entry(hass, entry)
        hass.config_entries.async_unload_platforms.return_value = False
        self.assertFalse(await integration.async_unload_entry(hass, entry))
        self.assertIn(entry.entry_id, hass.data["chronosnap"])
        self.assertTrue(hass.bus.listeners)
        await hass.data["chronosnap"][entry.entry_id]["coordinator"].async_shutdown()

    async def test_enabled_only_options_update_does_not_reload(self):
        c = make_coordinator()
        c.entry.options["profiles"]["p"]["enabled"] = False
        await integration._async_update_listener(c.hass, c.entry)
        self.assertFalse(c.is_enabled("p"))
        c.hass.config_entries.async_reload.assert_not_awaited()
        await c.async_shutdown()

    async def test_switch_listener_does_not_duplicate_failed_stop_retries(self):
        c = make_coordinator()
        c.STOP_RETRY_DELAY = 0
        await c._handle_start("p")
        c.client.complete_job.side_effect = api.ChronoSnapConnectionError()
        with self.assertRaises(HomeAssistantError):
            await c.async_set_enabled("p", False)
        await integration._async_update_listener(c.hass, c.entry)
        self.assertEqual(c.client.complete_job.await_count, c.STOP_MAX_RETRIES)
        await c.async_shutdown()

    async def test_bulk_disable_applies_all_gates_before_waiting_for_server(self):
        c = make_coordinator({"p": PROFILE, "q": PROFILE})
        await c._handle_start("p")
        started, release = asyncio.Event(), asyncio.Event()

        async def complete(*args):
            started.set()
            await release.wait()
            return {"status": "completed"}

        c.client.complete_job.side_effect = complete
        profiles = deepcopy(c.entry.options["profiles"])
        for profile in profiles.values():
            profile["enabled"] = False
        c.entry.options["profiles"] = profiles
        applying = asyncio.create_task(
            integration._async_update_listener(c.hass, c.entry)
        )
        try:
            await asyncio.wait_for(started.wait(), 2)
            self.assertFalse(c.is_enabled("q"))
            await c.async_set_enabled("q", True)
        finally:
            release.set()
            await applying
            await c.async_shutdown()
        self.assertTrue(c.is_enabled("q"))
        self.assertTrue(c.entry.options["profiles"]["q"]["enabled"])

    async def test_removal_finishes_ready_build_and_removes_empty_store(self):
        c = make_coordinator()
        await c._handle_start("p")
        c.client.get_video.return_value = {"status": "completed"}
        with patch.object(integration, "ChronoSnapClient", return_value=c.client):
            await integration.async_remove_entry(c.hass, c.entry)
        c.client.complete_job.assert_awaited_once()
        c.client.delete_job.assert_awaited_once()
        self.assertNotIn("chronosnap_active_jobs_entry", c.hass.storage)
        self.assertFalse(c.hass.notifications)
        await c.async_shutdown()

    async def test_removal_surfaces_pending_build_without_background_worker(self):
        c = make_coordinator()
        await c._handle_start("p")
        with patch.object(integration, "ChronoSnapClient", return_value=c.client):
            await integration.async_remove_entry(c.hass, c.entry)
        c.client.complete_job.assert_awaited_once()
        c.client.delete_job.assert_not_awaited()
        self.assertIn("chronosnap_active_jobs_entry", c.hass.storage)
        self.assertEqual(len(c.hass.notifications), 1)
        message = c.hass.notifications[0]["message"]
        self.assertIn("Job 1, video 2, phase: building", message)
        self.assertIn("Automatic recovery and cleanup have stopped", message)
        self.assertNotIn(c.entry.data["api_key"], message)
        self.assertFalse(c.hass.bus.listeners)
        await c.async_shutdown()

    async def test_removal_reports_capture_that_could_not_stop(self):
        c = make_coordinator()
        await c._handle_start("p")
        c.client.complete_job.side_effect = api.ChronoSnapAuthError()
        with patch.object(integration, "ChronoSnapClient", return_value=c.client):
            await integration.async_remove_entry(c.hass, c.entry)
        self.assertIn("phase: capturing", c.hass.notifications[0]["message"])
        c.client.create_video.assert_not_awaited()
        c.client.delete_job.assert_not_awaited()
        await c.async_shutdown()

    async def test_removal_lists_archived_failed_builds(self):
        c = make_coordinator()
        await c._handle_start("p")
        await c._handle_stop("p")
        c.client.get_video.return_value = {"status": "failed"}
        await c._async_poll()
        c.client.create_job.return_value = {"id": 3}
        await c._handle_start("p")
        c.client.create_video.return_value = {"id": 4}
        c.client.get_video.return_value = {"status": "processing"}
        with patch.object(integration, "ChronoSnapClient", return_value=c.client):
            await integration.async_remove_entry(c.hass, c.entry)
        message = c.hass.notifications[0]["message"]
        self.assertIn("Job 1, video 2, phase: failed", message)
        self.assertIn("Job 3, video 4, phase: building", message)
        c.client.delete_job.assert_not_awaited()
        await c.async_shutdown()

    async def test_structural_options_update_reloads(self):
        c = make_coordinator()
        c.entry.options["profiles"]["p"]["name"] = "Renamed"
        await integration._async_update_listener(c.hass, c.entry)
        c.hass.config_entries.async_reload.assert_awaited_once()
        await c.async_shutdown()

    async def test_disabled_entry_stops_captures_on_unload(self):
        c = make_coordinator()
        await c._handle_start("p")
        c.entry.disabled_by = "user"
        await integration.async_unload_entry(c.hass, c.entry)
        c.client.complete_job.assert_awaited_once()

    async def test_new_controls_survive_registry_cleanup_and_keep_sensor_ids(self):
        c = make_coordinator()
        entities = [
            sensors.ChronoSnapStatusSensor(c, c.entry, "p", PROFILE),
            sensors.ChronoSnapCaptureCountSensor(c, c.entry, "p", PROFILE),
            switches.ChronoSnapProfileSwitch(c, c.entry, "p", PROFILE),
            buttons.ChronoSnapStartButton(c, c.entry, "p", PROFILE),
            buttons.ChronoSnapRetryButton(c, c.entry, "p", PROFILE),
        ]
        c.hass.entities.entities = [
            SimpleNamespace(unique_id=entity._attr_unique_id, entity_id=str(n))
            for n, entity in enumerate(entities)
        ]
        integration._cleanup_stale_entities(c.hass, c.entry, deepcopy(c.profiles))
        self.assertFalse(c.hass.entities.removed)
        self.assertEqual(entities[0]._attr_unique_id, "chronosnap_p_status")
        await c.async_shutdown()
