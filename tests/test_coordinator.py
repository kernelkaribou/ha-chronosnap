"""Regression coverage for profile gates, races, and recovery phases."""

import asyncio
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from tests.helpers import (
    PROFILE,
    Entry,
    HomeAssistantError,
    api,
    client,
    coordinator,
    diagnostics,
    make_coordinator,
    sensors,
    switches,
)


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.c = make_coordinator()
        self.c.STOP_RETRY_DELAY = 0

    async def asyncTearDown(self):
        await self.c.async_shutdown()

    async def drain(self):
        if self.c._tasks:
            await asyncio.gather(*tuple(self.c._tasks))

    def event(self, old, new):
        self.c.hass.set_state(new)
        return SimpleNamespace(
            data={
                "old_state": SimpleNamespace(state=old),
                "new_state": SimpleNamespace(state=new),
            }
        )

    async def test_switch_off_persists_without_reloading(self):
        await self.c.async_set_enabled("p", False)
        self.assertFalse(self.c.entry.options["profiles"]["p"]["enabled"])
        self.assertFalse(self.c.is_enabled("p"))
        self.assertEqual(self.c.status("p"), "disabled")
        self.c.hass.config_entries.async_reload.assert_not_awaited()
        await self.c._handle_start("p")
        self.c.client.create_job.assert_not_awaited()

    async def test_rearming_does_not_start_mid_print(self):
        await self.c.async_set_enabled("p", False)
        await self.c.async_set_enabled("p", True)
        await self.c._async_poll()
        self.c.client.create_job.assert_not_awaited()
        await self.c.async_start_now("p")
        self.c.client.create_job.assert_awaited_once()

    async def test_start_now_rejects_disabled_or_inactive_profile(self):
        await self.c.async_set_enabled("p", False)
        with self.assertRaises(HomeAssistantError):
            await self.c.async_start_now("p")
        await self.c.async_set_enabled("p", True)
        self.c.hass.set_state("idle")
        with self.assertRaises(HomeAssistantError):
            await self.c.async_start_now("p")

    async def test_disabled_device_blocks_start_and_finishes_current_job(self):
        await self.c._handle_start("p")
        device = SimpleNamespace(
            identifiers={("chronosnap", "entry_p")}, disabled_by="user"
        )
        self.c.hass.devices.devices["device"] = device
        self.c._device_updated(
            SimpleNamespace(
                data={
                    "device_id": "device",
                    "changes": {"disabled_by": None},
                }
            )
        )
        await self.drain()
        self.assertFalse(self.c.is_enabled("p"))
        self.c.client.complete_job.assert_awaited_once()
        self.c.client.create_video.assert_awaited_once()
        self.assertEqual(self.c.recordings["p"].phase, "building")

    async def test_device_disabled_before_setup_blocks_recording(self):
        self.c.hass.devices.devices["device"] = SimpleNamespace(
            identifiers={("chronosnap", "entry_p")}, disabled_by="user"
        )
        await self.c._handle_start("p")
        self.c.client.create_job.assert_not_awaited()

    async def test_disable_cancels_pending_start(self):
        self.c.profiles["p"]["start_delay_seconds"] = 100
        self.c._make_state_handler("p")(self.event("idle", "printing"))
        timer = self.c._start_timers["p"]
        await self.c.async_set_enabled("p", False)
        self.assertTrue(timer.cancelled())
        self.assertFalse(self.c._start_timers)

    def fire_timer(self, timers):
        timer = timers["p"]
        timer._run()
        timer.cancel()

    async def test_expired_start_cannot_bypass_replacement_delay(self):
        self.c.profiles["p"]["start_delay_seconds"] = 60
        handler = self.c._make_state_handler("p")
        async with self.c._lock("p"):
            handler(self.event("idle", "printing"))
            self.fire_timer(self.c._start_timers)
            await asyncio.sleep(0)
            handler(self.event("printing", "idle"))
            handler(self.event("idle", "printing"))
        await self.drain()
        self.c.client.create_job.assert_not_awaited()
        self.assertIn("p", self.c._start_timers)
        self.fire_timer(self.c._start_timers)
        await self.drain()
        self.c.client.create_job.assert_awaited_once()

    async def test_expired_stop_cannot_bypass_replacement_debounce(self):
        self.c.profiles["p"]["debounce_seconds"] = 60
        await self.c._handle_start("p")
        handler = self.c._make_state_handler("p")
        async with self.c._lock("p"):
            handler(self.event("printing", "idle"))
            self.fire_timer(self.c._stop_timers)
            await asyncio.sleep(0)
            handler(self.event("idle", "printing"))
            handler(self.event("printing", "idle"))
        await self.drain()
        self.c.client.complete_job.assert_not_awaited()
        self.assertIn("p", self.c._stop_timers)
        self.fire_timer(self.c._stop_timers)
        await self.drain()
        self.c.client.complete_job.assert_awaited_once()

    async def test_disarm_rearm_invalidates_queued_start(self):
        async with self.c._lock("p"):
            self.c._schedule_start("p")
            await asyncio.sleep(0)
            off = asyncio.create_task(self.c.async_set_enabled("p", False))
            await asyncio.sleep(0)
            await self.c.async_set_enabled("p", True)
        await asyncio.gather(off, *tuple(self.c._tasks))
        self.assertTrue(self.c.is_enabled("p"))
        self.c.client.create_job.assert_not_awaited()

    async def test_disarm_drains_and_stops_already_issued_creation(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def create(**kwargs):
            started.set()
            await release.wait()
            return {"id": 7}

        self.c.client.create_job.side_effect = create
        self.c._schedule_start("p")
        await started.wait()
        off = asyncio.create_task(self.c.async_set_enabled("p", False))
        await asyncio.sleep(0)
        await self.c.async_set_enabled("p", True)
        release.set()
        await asyncio.gather(off, *tuple(self.c._tasks))
        self.c.client.complete_job.assert_awaited_once()
        self.assertEqual(self.c.recordings["p"].job_id, 7)
        self.assertEqual(self.c.recordings["p"].phase, "building")

    async def test_excluded_state_during_issued_creation_does_not_stop_capture(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def create(**kwargs):
            started.set()
            await release.wait()
            return {"id": 7}

        self.c.client.create_job.side_effect = create
        self.c._schedule_start("p")
        await started.wait()
        self.c._make_state_handler("p")(self.event("printing", "paused"))
        release.set()
        await self.drain()
        self.c.client.complete_job.assert_not_awaited()
        self.assertEqual(self.c.recordings["p"].phase, "capturing")

    async def test_shutdown_stops_profiles_with_bounded_concurrency(self):
        profile_count = self.c.SHUTDOWN_CONCURRENCY + 2
        for index in range(profile_count):
            pid = str(index)
            self.c.profiles[pid] = deepcopy(PROFILE)
            self.c.recordings[pid] = coordinator.Recording(index + 1, deepcopy(PROFILE))
        active = peak = 0
        saturated, release = asyncio.Event(), asyncio.Event()

        async def complete(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == self.c.SHUTDOWN_CONCURRENCY:
                saturated.set()
            await release.wait()
            active -= 1
            return {"status": "completed"}

        self.c.client.complete_job.side_effect = complete
        shutdown = asyncio.create_task(self.c.async_shutdown(stop_jobs=True))
        try:
            await asyncio.wait_for(saturated.wait(), 2)
            self.assertEqual(peak, self.c.SHUTDOWN_CONCURRENCY)
            self.assertEqual(
                self.c.client.complete_job.await_count, self.c.SHUTDOWN_CONCURRENCY
            )
        finally:
            release.set()
            await shutdown
        self.assertEqual(self.c.client.complete_job.await_count, profile_count)
        self.assertEqual(peak, self.c.SHUTDOWN_CONCURRENCY)

    async def test_excluded_state_cancels_pending_stop(self):
        self.c.profiles["p"]["debounce_seconds"] = 100
        await self.c._handle_start("p")
        handler = self.c._make_state_handler("p")
        handler(self.event("printing", "idle"))
        timer = self.c._stop_timers["p"]
        handler(self.event("idle", "paused"))
        self.assertTrue(timer.cancelled())
        self.assertFalse(self.c._stop_timers)
        await self.c._handle_stop("p", check_trigger=True)
        self.c.client.complete_job.assert_not_awaited()

    async def test_return_to_active_cancels_stop_and_does_not_duplicate_job(self):
        self.c.profiles["p"]["debounce_seconds"] = 100
        await self.c._handle_start("p")
        handler = self.c._make_state_handler("p")
        handler(self.event("printing", "idle"))
        handler(self.event("idle", "printing"))
        self.assertFalse(self.c._stop_timers)
        await self.drain()
        self.c.client.create_job.assert_awaited_once()

    async def test_attribute_only_changes_do_not_start_capture(self):
        self.c._make_state_handler("p")(self.event("printing", "printing"))
        await self.drain()
        self.c.client.create_job.assert_not_awaited()

    async def test_overlapping_starts_create_only_one_job(self):
        await asyncio.gather(self.c._handle_start("p"), self.c._handle_start("p"))
        self.c.client.create_job.assert_awaited_once()

    async def test_overlapping_stops_create_only_one_video(self):
        await self.c._handle_start("p")
        await asyncio.gather(self.c._handle_stop("p"), self.c._handle_stop("p"))
        self.c.client.complete_job.assert_awaited_once()
        self.c.client.create_video.assert_awaited_once()

    async def test_disable_during_creation_stops_returned_job(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def create(**kwargs):
            started.set()
            await release.wait()
            return {"id": 1}

        self.c.client.create_job.side_effect = create
        task = asyncio.create_task(self.c._handle_start("p"))
        await started.wait()
        off = asyncio.create_task(self.c.async_set_enabled("p", False))
        await asyncio.sleep(0)
        self.assertFalse(self.c.is_enabled("p"))
        release.set()
        await asyncio.gather(task, off)
        self.c.client.complete_job.assert_awaited_once()
        self.assertEqual(self.c.recordings["p"].phase, "building")

    async def test_shutdown_drains_creation_and_preserves_returned_ids(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def create(**kwargs):
            started.set()
            await release.wait()
            return {"id": 1}

        self.c.client.create_job.side_effect = create
        self.c._spawn(self.c._handle_start("p"))
        await started.wait()
        shutdown = asyncio.create_task(self.c.async_shutdown())
        await asyncio.sleep(0)
        release.set()
        await shutdown
        self.c.client.complete_job.assert_awaited_once()
        stored = self.c.hass.storage["chronosnap_active_jobs_entry"]
        self.assertEqual(stored["recordings"]["p"]["video_id"], 2)

    async def test_failed_video_never_deletes_frames_or_reports_idle(self):
        await self.c._handle_start("p")
        await self.c._handle_stop("p")
        self.c.client.get_video.return_value = {"id": 2, "status": "failed"}
        await self.c._async_poll()
        self.c.client.delete_job.assert_not_awaited()
        self.assertEqual(self.c.recordings["p"].phase, "failed")
        self.assertEqual(self.c.status("p"), "error")

    async def test_successful_video_cleanup_releases_recording(self):
        await self.c._handle_start("p")
        await self.c._handle_stop("p")
        self.c.client.get_video.return_value = {"id": 2, "status": "completed"}
        await self.c._async_poll()
        self.c.client.delete_job.assert_awaited_once_with(1)
        self.assertNotIn("p", self.c.recordings)
        self.assertEqual(self.c.last_videos["p"], 2)

    async def test_failed_video_cleanup_is_explicitly_selectable(self):
        self.c.profiles["p"]["preserve_failed_frames"] = False
        await self.c._handle_start("p")
        await self.c._handle_stop("p")
        self.c.client.get_video.return_value = {"id": 2, "status": "failed"}
        await self.c._async_poll()
        self.c.client.delete_job.assert_awaited_once_with(1)
        self.assertNotIn("p", self.c.recordings)
        self.assertIn("deleted by the profile cleanup policy", self.c.last_errors["p"])
        with self.assertRaises(HomeAssistantError):
            await self.c.async_retry("p")

    async def test_auto_cleanup_off_overrides_failed_build_deletion(self):
        self.c.profiles["p"].update(auto_cleanup=False, preserve_failed_frames=False)
        await self.c._handle_start("p")
        await self.c._handle_stop("p")
        self.c.client.get_video.return_value = {"status": "failed"}
        await self.c._async_poll()
        self.c.client.delete_job.assert_not_awaited()
        self.assertEqual(self.c.recordings["p"].phase, "failed")

    async def test_failed_build_policy_does_not_delete_on_network_errors(self):
        self.c.profiles["p"]["preserve_failed_frames"] = False
        await self.c._handle_start("p")
        await self.c._handle_stop("p")
        self.c.client.get_video.side_effect = api.ChronoSnapConnectionError()
        await self.c._async_poll()
        self.c.client.delete_job.assert_not_awaited()
        self.assertEqual(self.c.recordings["p"].phase, "building")

    async def test_failed_build_policy_does_not_delete_unknown_status(self):
        self.c.profiles["p"]["preserve_failed_frames"] = False
        await self.c._handle_start("p")
        await self.c._handle_stop("p")
        self.c.client.get_video.return_value = {"status": "unexpected"}
        await self.c._async_poll()
        self.c.client.delete_job.assert_not_awaited()
        self.assertEqual(self.c.recordings["p"].phase, "building")

    async def test_failed_build_cleanup_failure_is_retried(self):
        self.c.profiles["p"]["preserve_failed_frames"] = False
        await self.c._handle_start("p")
        await self.c._handle_stop("p")
        self.c.client.get_video.return_value = {"status": "failed"}
        self.c.client.delete_job.side_effect = api.ChronoSnapConnectionError()
        await self.c._async_poll()
        self.assertEqual(self.c.recordings["p"].phase, "cleanup_failed")
        self.c.client.delete_job.side_effect = None
        await self.c._async_poll()
        self.assertNotIn("p", self.c.recordings)
        self.assertIn("cannot be rebuilt", self.c.last_errors["p"])

    async def test_retention_policy_is_snapshotted_with_recording(self):
        await self.c._handle_start("p")
        self.c.profiles["p"]["preserve_failed_frames"] = False
        await self.c._handle_stop("p")
        self.c.client.get_video.return_value = {"status": "failed"}
        await self.c._async_poll()
        self.c.client.delete_job.assert_not_awaited()
        self.assertEqual(self.c.recordings["p"].phase, "failed")

    async def test_retaining_failed_build_does_not_block_the_next_print(self):
        await self.c._handle_start("p")
        await self.c._handle_stop("p")
        self.c.client.get_video.return_value = {"id": 2, "status": "failed"}
        await self.c._async_poll()
        self.c.client.create_job.return_value = {"id": 3}
        await self.c._handle_start("p")
        self.assertEqual(self.c.recordings["p"].job_id, 3)
        self.assertEqual(self.c.failed_recordings["p"][0].job_id, 1)
        self.c.client.delete_job.assert_not_awaited()
        self.assertEqual(self.c.status("p"), "capturing")

    async def test_archived_failure_can_be_recovered_after_next_print_finishes(self):
        await self.test_retaining_failed_build_does_not_block_the_next_print()
        await self.c._handle_stop("p")
        self.c.client.get_video.return_value = {"status": "completed"}
        await self.c._async_poll()
        self.assertEqual(self.c.status("p"), "error")
        await self.c.async_retry("p")
        self.assertEqual(self.c.recordings["p"].job_id, 1)
        self.assertEqual(self.c.recordings["p"].phase, "building")

    async def test_archived_recoveries_survive_restart(self):
        await self.test_retaining_failed_build_does_not_block_the_next_print()
        restarted = coordinator.ProfileCoordinator(
            self.c.hass, self.c.client, self.c.entry
        )
        await restarted.async_load()
        self.assertEqual(restarted.failed_recordings["p"][0].job_id, 1)
        self.assertEqual(restarted.recordings["p"].job_id, 3)
        await restarted.async_shutdown()

    async def test_cleanup_can_be_disabled(self):
        self.c.profiles["p"]["auto_cleanup"] = False
        await self.c._handle_start("p")
        await self.c._handle_stop("p")
        self.c.client.get_video.return_value = {"id": 2, "status": "completed"}
        await self.c._async_poll()
        self.c.client.delete_job.assert_not_awaited()
        self.assertNotIn("p", self.c.recordings)

    async def test_cleanup_failure_retains_phase_for_next_poll(self):
        await self.c._handle_start("p")
        await self.c._handle_stop("p")
        self.c.client.get_video.return_value = {"status": "completed"}
        self.c.client.delete_job.side_effect = api.ChronoSnapConnectionError()
        await self.c._async_poll()
        self.assertEqual(self.c.recordings["p"].phase, "cleanup")
        self.assertEqual(self.c.status("p"), "error")
        self.c.client.delete_job.side_effect = None
        await self.c._async_poll()
        self.assertNotIn("p", self.c.recordings)

    async def test_poll_error_retains_video_for_recovery(self):
        await self.c._handle_start("p")
        await self.c._handle_stop("p")
        self.c.client.get_video.side_effect = api.ChronoSnapConnectionError()
        await self.c._async_poll()
        self.assertEqual(self.c.status("p"), "error")
        self.assertEqual(self.c.recordings["p"].video_id, 2)
        self.c.client.delete_job.assert_not_awaited()

    async def test_retry_failed_video_creates_a_new_build_not_a_capture(self):
        await self.c._handle_start("p")
        await self.c._handle_stop("p")
        self.c.client.get_video.return_value = {"status": "failed"}
        await self.c._async_poll()
        await self.c.async_retry("p")
        self.assertEqual(self.c.client.create_job.await_count, 1)
        self.assertEqual(self.c.client.create_video.await_count, 2)

    async def test_stop_retries_transient_errors_but_not_auth_errors(self):
        await self.c._handle_start("p")
        self.c.client.complete_job.side_effect = [
            api.ChronoSnapConnectionError(),
            {"id": 1},
        ]
        await self.c._handle_stop("p")
        self.assertEqual(self.c.client.complete_job.await_count, 2)
        self.c.recordings["p"].phase = "capturing"
        self.c.client.complete_job.reset_mock()
        self.c.client.complete_job.side_effect = api.ChronoSnapAuthError()
        await self.c._handle_stop("p")
        self.c.client.complete_job.assert_awaited_once()
        self.assertEqual(self.c.recordings["p"].phase, "capturing")

    async def test_delete_is_blocked_if_server_cannot_stop_capture(self):
        await self.c._handle_start("p")
        self.c.client.complete_job.side_effect = api.ChronoSnapConnectionError()
        with self.assertRaises(HomeAssistantError):
            await self.c.async_remove_profile("p")
        self.assertFalse(self.c.is_enabled("p"))
        self.assertEqual(self.c.recordings["p"].phase, "capturing")

    async def test_video_settings_are_snapshotted_when_capture_starts(self):
        await self.c._handle_start("p")
        self.c.profiles["p"]["resolution"] = "640x480"
        await self.c._handle_stop("p")
        self.assertEqual(
            self.c.client.create_video.call_args.kwargs["resolution"], "1920x1080"
        )

    async def test_failed_build_creation_is_not_repeated_automatically(self):
        await self.c._handle_start("p")
        self.c.client.create_video.side_effect = api.ChronoSnapConnectionError()
        await self.c._handle_stop("p")
        self.assertEqual(self.c.recordings["p"].phase, "completed")
        await self.c._async_poll()
        self.c.client.create_video.assert_awaited_once()
        self.assertEqual(self.c.status("p"), "error")
        self.c.client.create_video.side_effect = None
        await self.c.async_retry("p")
        self.assertEqual(self.c.client.create_video.await_count, 2)

    async def test_restart_reconciles_job_that_ended_while_ha_was_offline(self):
        await self.c._handle_start("p")
        restarted = coordinator.ProfileCoordinator(
            self.c.hass, self.c.client, self.c.entry
        )
        await restarted.async_load()
        restarted.hass.set_state("idle")
        await restarted.async_start()
        if restarted._tasks:
            await asyncio.gather(*tuple(restarted._tasks))
        self.c.client.complete_job.assert_awaited_once()
        self.assertEqual(restarted.recordings["p"].phase, "building")
        await restarted.async_shutdown()

    async def test_build_recovery_uses_saved_video_id(self):
        await self.c._handle_start("p")
        await self.c._handle_stop("p")
        restarted = coordinator.ProfileCoordinator(
            self.c.hass, self.c.client, self.c.entry
        )
        await restarted.async_load()
        restarted._running = restarted._ready = True
        await restarted._async_poll()
        self.c.client.get_video.assert_awaited_with(2)
        self.c.client.create_video.assert_awaited_once()
        await restarted.async_shutdown()

    async def test_legacy_job_migration_and_instances_are_independent(self):
        self.c.hass.storage["chronosnap_active_jobs"] = {
            "entry": {"p": 7},
            "other": {"q": 8},
        }
        await self.c.async_load()
        self.assertEqual(self.c.recordings["p"].job_id, 7)
        self.assertIn("chronosnap_active_jobs_entry", self.c.hass.storage)
        self.assertEqual(
            self.c.hass.storage["chronosnap_active_jobs"]["other"], {"q": 8}
        )
        other = coordinator.ProfileCoordinator(
            self.c.hass, client(), Entry({}, "other")
        )
        await other.async_load()
        await other._async_save()
        self.assertEqual(
            self.c.hass.storage["chronosnap_active_jobs_entry"]["recordings"]["p"][
                "job_id"
            ],
            7,
        )

    async def test_auth_failure_during_recovery_never_discards_job(self):
        await self.c._handle_start("p")
        self.c.client.get_job.side_effect = api.ChronoSnapAuthError()
        await self.c._async_poll()
        self.assertEqual(self.c.recordings["p"].job_id, 1)
        self.assertEqual(self.c.status("p"), "error")

    async def test_confirmed_deleted_job_releases_tracking_with_visible_error(self):
        await self.c._handle_start("p")
        self.c.client.get_job.side_effect = api.ChronoSnapNotFoundError()
        await self.c._async_poll()
        self.assertNotIn("p", self.c.recordings)
        self.assertIn("deleted on ChronoSnap", self.c.last_errors["p"])
        self.c.client.create_job.assert_awaited_once()

    async def test_missing_video_preserves_frames_and_can_be_rebuilt(self):
        await self.c._handle_start("p")
        await self.c._handle_stop("p")
        self.c.client.get_video.side_effect = api.ChronoSnapNotFoundError()
        await self.c._async_poll()
        self.c.client.delete_job.assert_not_awaited()
        self.assertEqual(self.c.recordings["p"].phase, "building")
        await self.c.async_retry("p")
        self.assertEqual(self.c.client.create_video.await_count, 2)

    async def test_unknown_trigger_at_startup_preserves_restored_capture(self):
        await self.c._handle_start("p")
        self.c.hass.set_state("unavailable")
        await self.c._async_poll()
        self.c.client.complete_job.assert_not_awaited()
        self.assertIn("p", self.c.active_jobs)

    async def test_capture_warning_does_not_prevent_later_stop(self):
        await self.c._handle_start("p")
        self.c.client.get_job.return_value = {"id": 1, "status": "warning"}
        await self.c._async_poll()
        self.assertEqual(self.c.status("p"), "error")
        await self.c.async_set_enabled("p", False)
        self.c.client.complete_job.assert_awaited_once()

    async def test_switch_off_reports_failure_but_remains_disarmed(self):
        await self.c._handle_start("p")
        self.c.client.complete_job.side_effect = api.ChronoSnapConnectionError()
        with self.assertRaisesRegex(HomeAssistantError, "profile is disabled"):
            await self.c.async_set_enabled("p", False)
        self.assertFalse(self.c.entry.options["profiles"]["p"]["enabled"])

    async def test_failed_cleanup_phase_survives_restart(self):
        self.c.profiles["p"]["preserve_failed_frames"] = False
        await self.c._handle_start("p")
        await self.c._handle_stop("p")
        self.c.client.get_video.return_value = {"status": "failed"}
        self.c.client.delete_job.side_effect = api.ChronoSnapConnectionError()
        await self.c._async_poll()
        restarted = coordinator.ProfileCoordinator(
            self.c.hass, self.c.client, self.c.entry
        )
        await restarted.async_load()
        restarted._running = restarted._ready = True
        self.c.client.delete_job.side_effect = None
        await restarted._async_poll()
        self.assertNotIn("p", restarted.recordings)
        self.assertIn("cannot be rebuilt", restarted.last_errors["p"])
        await restarted.async_shutdown()

    async def test_startup_waits_for_ha_and_does_not_start_existing_print(self):
        self.c.hass.is_running = False
        self.c._running = self.c._ready = False
        await self.c.async_start()
        self.assertFalse(self.c._ready)
        await self.c._ha_started(SimpleNamespace())
        self.c.client.create_job.assert_not_awaited()

    async def test_removed_profile_active_job_is_finalized_during_recovery(self):
        await self.c._handle_start("p")
        self.c.profiles.clear()
        await self.c._async_poll()
        self.c.client.complete_job.assert_awaited_once()

    async def test_capture_counts_update_and_failures_become_unavailable(self):
        await self.c._handle_start("p")
        sensor = sensors.ChronoSnapCaptureCountSensor(
            self.c, self.c.entry, "p", PROFILE
        )
        await self.c._async_poll()
        self.assertEqual(sensor.native_value, 12)
        self.assertTrue(sensor.available)
        self.c.client.get_capture_count.side_effect = api.ChronoSnapConnectionError()
        await self.c._async_poll()
        self.assertFalse(sensor.available)

    async def test_entity_subscription_is_removed_using_exact_callback(self):
        sensor = sensors.ChronoSnapStatusSensor(self.c, self.c.entry, "p", PROFILE)
        await sensor.async_added_to_hass()
        self.assertEqual(len(self.c._update_callbacks), 1)
        await sensor.async_will_remove_from_hass()
        self.assertFalse(self.c._update_callbacks)
        self.c._fire_update()
        self.assertEqual(getattr(sensor, "_writes", 0), 0)

    async def test_switch_entity_uses_persisted_control(self):
        switch = switches.ChronoSnapProfileSwitch(self.c, self.c.entry, "p", PROFILE)
        self.assertTrue(switch.is_on)
        await switch.async_turn_off()
        self.assertFalse(switch.is_on)

    async def test_diagnostics_do_not_include_stream_url_or_api_key(self):
        await self.c._handle_start("p")
        result = await diagnostics.async_get_config_entry_diagnostics(
            self.c.hass, self.c.entry
        )
        self.assertNotIn(PROFILE["stream_url"], repr(result))
        self.assertNotIn(self.c.entry.data["api_key"], repr(result))

    async def test_duration_units_nonfinite_and_timezones(self):
        self.assertEqual(self.c._parse_duration_value("2", "min"), 120)
        self.assertEqual(self.c._parse_duration_value("2", "h"), 7200)
        self.assertIsNone(self.c._parse_duration_value("nan"))
        self.assertIsNone(self.c._parse_duration_value("inf"))
        self.assertIsNone(self.c._parse_duration_value("12", "unsupported"))
        future = datetime.now(UTC) + timedelta(hours=1)
        self.assertAlmostEqual(
            self.c._parse_duration_value(future.isoformat()), 3600, delta=3
        )
        self.assertAlmostEqual(
            self.c._parse_duration_value(future.replace(tzinfo=None).isoformat()),
            3600,
            delta=3,
        )

    async def test_interval_minimum_and_fallback(self):
        profile = deepcopy(PROFILE)
        profile.update(
            interval_mode="target_duration",
            duration_entity="sensor.remaining",
            fps=30,
            target_duration=30,
        )
        self.c.hass.set_state("60", "sensor.remaining", unit_of_measurement="min")
        self.assertEqual(self.c._calculate_interval(profile), 10)
        self.c.hass.set_state("900", "sensor.remaining", unit_of_measurement="min")
        self.assertEqual(self.c._calculate_interval(profile), 60)
        self.c.hass.set_state("unavailable", "sensor.remaining")
        self.assertEqual(self.c._calculate_interval(profile), 30)
