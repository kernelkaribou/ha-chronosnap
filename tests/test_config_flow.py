"""Configuration edits must be isolated, validated, and recoverable."""

import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from tests.helpers import (
    PROFILE,
    Entry,
    Hass,
    api,
    client,
    flow,
    make_coordinator,
)


class OptionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.c = make_coordinator()
        self.options = flow.ChronoSnapOptionsFlow(self.c.entry)
        self.options.hass = self.c.hass
        self.options._editing_profile_id = "p"
        self.api = client()
        self.patch = patch.object(flow, "ChronoSnapClient", return_value=self.api)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        await self.c.async_shutdown()

    async def test_edit_draft_does_not_mutate_saved_profile(self):
        values = {**PROFILE, "name": "Changed"}
        await self.options.async_step_profile_basic(values)
        self.assertEqual(self.c.entry.options["profiles"]["p"]["name"], "Printer")
        self.assertEqual(self.options._profiles["p"]["name"], "Changed")

    async def test_unrelated_edit_preserves_concurrent_switch_change(self):
        self.options._editing_profile_id = "new"
        self.options._profiles["new"] = {**PROFILE, "name": "New"}
        await self.c.async_set_enabled("p", False)
        saved = self.options._save_and_finish()
        self.assertFalse(saved["data"]["profiles"]["p"]["enabled"])

    async def test_enabled_conflict_requires_explicit_choice_without_saving(self):
        self.options._profiles["p"]["name"] = "Changed"
        await self.c.async_set_enabled("p", False)
        result = self.options._save_and_finish()
        self.assertEqual(result["step_id"], "profile_enabled_conflict")
        self.assertFalse(result["data_schema"]({})["enabled"])
        self.assertEqual(self.c.entry.options["profiles"]["p"]["name"], "Printer")
        self.assertFalse(self.c.entry.options["profiles"]["p"]["enabled"])
        saved = await self.options.async_step_profile_enabled_conflict(
            {"enabled": True}
        )
        self.assertTrue(saved["data"]["profiles"]["p"]["enabled"])
        self.assertEqual(saved["data"]["profiles"]["p"]["name"], "Changed")

    async def test_enabled_conflict_can_preserve_live_switch_setting(self):
        await self.c.async_set_enabled("p", False)
        self.options._save_and_finish()
        saved = await self.options.async_step_profile_enabled_conflict(
            {"enabled": False}
        )
        self.assertFalse(saved["data"]["profiles"]["p"]["enabled"])

    async def test_capture_step_checks_conflicts_before_closing_edit(self):
        await self.c.async_set_enabled("p", False)
        result = await self.options.async_step_profile_capture(
            {"interval_mode": "fixed", "resolution": "1920x1080"}
        )
        self.assertEqual(result["step_id"], "profile_enabled_conflict")
        self.assertEqual(self.options._editing_profile_id, "p")

    async def test_target_step_checks_conflicts_before_closing_edit(self):
        await self.c.async_set_enabled("p", False)
        result = await self.options.async_step_profile_target(
            {"target_duration": 30, "duration_entity": "sensor.remaining"}
        )
        self.assertEqual(result["step_id"], "profile_enabled_conflict")
        self.assertEqual(self.options._editing_profile_id, "p")

    async def test_explicit_enabled_edit_is_saved(self):
        self.options._profiles["p"]["enabled"] = False
        saved = self.options._save_and_finish()
        self.assertFalse(saved["data"]["profiles"]["p"]["enabled"])

    async def test_blank_name_state_invalid_stream_and_excluded_active_rejected(self):
        for changes, key, error in (
            ({"name": " "}, "name", "required"),
            ({"active_state": " "}, "active_state", "required"),
            ({"stream_url": "rtsp://"}, "stream_url", "invalid_stream_url"),
            (
                {"stream_type": "device", "stream_url": "/dev/videoabc"},
                "stream_url",
                "invalid_stream_url",
            ),
            ({"exclude_states": "printing"}, "exclude_states", "active_excluded"),
        ):
            with self.subTest(changes=changes):
                result = await self.options.async_step_profile_basic(
                    {**PROFILE, **changes}
                )
                self.assertEqual(result["errors"][key], error)

    async def test_local_device_path_is_accepted(self):
        result = await self.options.async_step_profile_basic(
            {
                **PROFILE,
                "stream_type": "device",
                "stream_url": "/dev/video0",
            }
        )
        self.assertEqual(result["step_id"], "profile_capture")

    async def test_invalid_resolution_does_not_finish_flow(self):
        result = await self.options.async_step_profile_capture(
            {
                "resolution": "0x1080",
                "interval_mode": "fixed",
            }
        )
        self.assertEqual(result["errors"]["resolution"], "invalid_resolution")
        self.assertEqual(result["type"], "form")

    async def test_failed_build_preservation_defaults_on_for_existing_profiles(self):
        result = await self.options.async_step_profile_capture()
        marker = next(
            key
            for key in result["data_schema"].schema
            if key.schema == "preserve_failed_frames"
        )
        self.assertTrue(marker.default())

    async def test_tag_fetch_failure_is_visible_and_existing_tags_are_retained(self):
        self.options._profiles["p"]["tag_ids"] = [5]
        self.api.get_tags.side_effect = api.ChronoSnapConnectionError()
        result = await self.options.async_step_profile_capture()
        self.assertIn(
            "could not be loaded", result["description_placeholders"]["tag_warning"]
        )
        schema = result["data_schema"].schema
        tag_selector = next(
            value for key, value in schema.items() if key.schema == "tag_ids"
        )
        self.assertEqual(tag_selector.config["options"][0]["value"], "5")
        self.assertEqual(self.options._profiles["p"]["tag_ids"], [5])

    async def test_tags_can_be_cleared(self):
        self.options._profiles["p"]["tag_ids"] = [5]
        result = await self.options.async_step_profile_capture(
            {
                "resolution": "1920x1080",
                "interval_mode": "fixed",
                "tag_ids": [],
            }
        )
        self.assertEqual(result["data"]["profiles"]["p"]["tag_ids"], [])

    async def test_delete_requires_confirmation(self):
        result = await self.options.async_step_profile_action({"action": "delete"})
        self.assertEqual(result["step_id"], "profile_delete")
        self.assertIn("p", self.options._profiles)

    async def test_delete_stops_recording_before_removing_profile(self):
        await self.c._handle_start("p")
        result = await self.options.async_step_profile_delete({"confirm": True})
        self.c.client.complete_job.assert_awaited_once()
        self.assertNotIn("p", result["data"]["profiles"])
        self.assertEqual(self.c.recordings["p"].phase, "building")

    async def test_delete_failure_keeps_profile_and_disarms_it(self):
        self.c.STOP_RETRY_DELAY = 0
        await self.c._handle_start("p")
        self.c.client.complete_job.side_effect = api.ChronoSnapConnectionError()
        result = await self.options.async_step_profile_delete({"confirm": True})
        self.assertEqual(result["errors"]["base"], "cannot_stop")
        self.assertIn("p", self.options._profiles)
        self.assertFalse(self.c.is_enabled("p"))

    async def test_changing_servers_is_blocked_while_recordings_are_tracked(self):
        await self.c._handle_start("p")
        result = await self.options.async_step_connection(
            {
                "url": "http://different-server",
                "api_key": "new-key",
            }
        )
        self.assertEqual(result["errors"]["base"], "recordings_pending")
        self.assertEqual(self.c.entry.data["url"], "http://chronosnap")

    async def test_api_key_can_be_updated_without_losing_profiles(self):
        old_profiles = deepcopy(self.c.entry.options["profiles"])
        result = await self.options.async_step_connection(
            {
                "url": "http://chronosnap",
                "api_key": "new-key",
            }
        )
        self.assertEqual(result["data"]["profiles"], old_profiles)
        self.assertEqual(self.c.entry.data["api_key"], "new-key")

    async def test_pending_delayed_start_blocks_server_change(self):
        self.c.profiles["p"]["start_delay_seconds"] = 60
        self.c._schedule_start("p")
        result = await self.options.async_step_connection(
            {"url": "http://different-server", "api_key": "new-key"}
        )
        self.assertEqual(result["errors"]["base"], "recordings_pending")

    async def test_inflight_start_blocks_server_change_before_job_id_is_known(self):
        async with self.c._lock("p"):
            result = await self.options.async_step_connection(
                {"url": "http://different-server", "api_key": "new-key"}
            )
        self.assertEqual(result["errors"]["base"], "recordings_pending")

    async def test_server_change_closes_old_start_gate_before_reload(self):
        result = await self.options.async_step_connection(
            {"url": "http://different-server", "api_key": "new-key"}
        )
        self.assertEqual(result["type"], "create_entry")
        self.assertFalse(self.c._running)
        await self.c._handle_start("p")
        self.c.client.create_job.assert_not_awaited()

    async def test_reload_failure_is_not_reported_as_success(self):
        self.c.hass.config_entries.async_reload.return_value = False
        result = await self.options.async_step_connection(
            {"url": "http://chronosnap", "api_key": "new-key"}
        )
        self.assertEqual(result["errors"]["base"], "reload_failed")


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.entry = Entry({"p": PROFILE})
        self.hass = Hass(self.entry)
        self.client = client()
        self.patch = patch.object(flow, "ChronoSnapClient", return_value=self.client)
        self.patch.start()
        self.flow = flow.ChronoSnapConfigFlow()
        self.flow.hass = self.hass
        self.flow.context = {"entry_id": self.entry.entry_id}

    async def asyncTearDown(self):
        self.patch.stop()

    async def test_initial_connection_validates_url_and_key(self):
        for url in (
            "ftp://server",
            "http://user:password@server",
            "http://server?token=key",
        ):
            result = await self.flow.async_step_user({"url": url, "api_key": "key"})
            self.assertEqual(result["errors"]["url"], "invalid_server_url")
        result = await self.flow.async_step_user(
            {"url": "http://server", "api_key": " "}
        )
        self.assertEqual(result["errors"]["api_key"], "required")

    async def test_auth_error_uses_recoverable_form(self):
        self.client.test_connection.side_effect = api.ChronoSnapAuthError()
        result = await self.flow.async_step_user(
            {"url": "http://server", "api_key": "key"}
        )
        self.assertEqual(result["errors"]["base"], "invalid_auth")

    async def test_reauthentication_preserves_options(self):
        before = deepcopy(self.entry.options)
        result = await self.flow.async_step_reauth_confirm({"api_key": "new-key"})
        self.assertEqual(result["reason"], "reauth_successful")
        self.assertEqual(self.entry.options, before)
        self.assertEqual(self.entry.data["api_key"], "new-key")

    async def test_reconfiguration_accepts_same_entry_url(self):
        result = await self.flow.async_step_reconfigure(
            {
                "url": "http://chronosnap",
                "api_key": "new-key",
            }
        )
        self.assertEqual(result["reason"], "reconfigure_successful")

    async def test_unloaded_entry_pending_jobs_block_server_change(self):
        self.hass.storage["chronosnap_active_jobs_entry"] = {
            "recordings": {"p": {"job_id": 1}},
        }
        result = await self.flow.async_step_reconfigure(
            {
                "url": "http://different-server",
                "api_key": "new-key",
            }
        )
        self.assertEqual(result["errors"]["base"], "recordings_pending")

    async def test_translation_files_match_and_cover_new_errors(self):
        root = Path(__file__).resolve().parents[1] / "custom_components" / "chronosnap"
        strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
        english = json.loads(
            (root / "translations" / "en.json").read_text(encoding="utf-8")
        )
        self.assertEqual(strings, english)
        for key in ("invalid_stream_url", "cannot_stop", "invalid_resolution"):
            self.assertIn(key, strings["options"]["error"])
