"""Profile coordinator — manages state listeners, job lifecycle, and persistence."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store

from .api import ChronoSnapClient, ChronoSnapError
from .const import (
    CONF_ACTIVE_STATE,
    CONF_AUTO_CLEANUP,
    CONF_CAPTURE_QUALITY,
    CONF_DEBOUNCE_SECONDS,
    CONF_DURATION_ENTITY,
    CONF_EXCLUDE_STATES,
    CONF_FPS,
    CONF_INTERVAL_MODE,
    CONF_INTERVAL_SECONDS,
    CONF_PROFILE_NAME,
    CONF_PROFILES,
    CONF_QUALITY,
    CONF_RESOLUTION,
    CONF_STREAM_TYPE,
    CONF_STREAM_URL,
    CONF_TAG_IDS,
    CONF_TARGET_DURATION,
    CONF_TRIGGER_ENTITY,
    DEFAULT_AUTO_CLEANUP,
    DEFAULT_CAPTURE_QUALITY,
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_FPS,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_QUALITY,
    DEFAULT_RESOLUTION,
    DEFAULT_TARGET_DURATION,
    DOMAIN,
    INTERVAL_MODE_TARGET,
    MIN_INTERVAL_SECONDS,
    STATUS_BUILDING,
    STATUS_CAPTURING,
    STATUS_ERROR,
    STATUS_IDLE,
    STORAGE_KEY,
    STORAGE_VERSION,
    VIDEO_POLL_INTERVAL,
    VIDEO_POLL_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


class ProfileCoordinator:
    """Coordinates timelapse profiles: listens for state changes and manages jobs."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: ChronoSnapClient,
        entry_id: str,
    ) -> None:
        self.hass = hass
        self.client = client
        self.entry_id = entry_id

        # profile_id → current status
        self.profile_status: dict[str, str] = {}
        # profile_id → active ChronoSnap job ID
        self.active_jobs: dict[str, int] = {}
        # profile_id → capture count
        self.capture_counts: dict[str, int] = {}
        # profile_id → listener unsubscribe callback
        self._listeners: dict[str, CALLBACK_TYPE] = {}
        # profile_id → debounce timer handle
        self._debounce_timers: dict[str, asyncio.TimerHandle] = {}
        # profile_id → video polling task
        self._video_tasks: dict[str, asyncio.Task] = {}

        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._update_callbacks: list = []

    # ── Persistence ─────────────────────────────────────────────

    async def async_load(self) -> None:
        """Load persisted active job mappings."""
        data = await self._store.async_load()
        if data and isinstance(data, dict):
            stored_jobs = data.get(self.entry_id, {})
            # Validate that stored jobs still exist in ChronoSnap
            for profile_id, job_id in stored_jobs.items():
                try:
                    job = await self.client.get_job(job_id)
                    if job and job.get("status") in ("active", "sleeping"):
                        self.active_jobs[profile_id] = job_id
                        self.profile_status[profile_id] = STATUS_CAPTURING
                        _LOGGER.info(
                            "Restored active job %s for profile %s",
                            job_id,
                            profile_id,
                        )
                except ChronoSnapError:
                    _LOGGER.warning(
                        "Stored job %s for profile %s no longer exists",
                        job_id,
                        profile_id,
                    )

    async def _async_save(self) -> None:
        """Persist active job mappings."""
        data = await self._store.async_load() or {}
        data[self.entry_id] = {
            pid: jid for pid, jid in self.active_jobs.items()
        }
        await self._store.async_save(data)

    # ── Listener management ─────────────────────────────────────

    def setup_listeners(self, profiles: dict[str, dict[str, Any]]) -> None:
        """Register state change listeners for all profiles."""
        self.teardown_listeners()

        for profile_id, profile in profiles.items():
            entity_id = profile.get(CONF_TRIGGER_ENTITY)
            if not entity_id:
                continue

            self.profile_status.setdefault(profile_id, STATUS_IDLE)
            self.capture_counts.setdefault(profile_id, 0)

            unsub = async_track_state_change_event(
                self.hass,
                [entity_id],
                self._make_state_handler(profile_id, profile),
            )
            self._listeners[profile_id] = unsub
            _LOGGER.debug(
                "Listening on %s for profile %s", entity_id, profile_id
            )

    def teardown_listeners(self) -> None:
        """Remove all state change listeners and cancel timers."""
        for unsub in self._listeners.values():
            unsub()
        self._listeners.clear()

        for timer in self._debounce_timers.values():
            timer.cancel()
        self._debounce_timers.clear()

        for task in self._video_tasks.values():
            task.cancel()
        self._video_tasks.clear()

    def _make_state_handler(
        self, profile_id: str, profile: dict[str, Any]
    ):
        """Create a state change event handler bound to a specific profile."""
        active_state = profile.get(CONF_ACTIVE_STATE, "").lower()
        debounce = profile.get(CONF_DEBOUNCE_SECONDS, DEFAULT_DEBOUNCE_SECONDS)
        exclude_raw = profile.get(CONF_EXCLUDE_STATES, "")
        exclude_states = {
            s.strip().lower() for s in exclude_raw.split(",") if s.strip()
        }

        @callback
        def _handler(event: Event) -> None:
            new_state: State | None = event.data.get("new_state")
            old_state: State | None = event.data.get("old_state")

            if new_state is None:
                return

            new_val = (new_state.state or "").lower()
            old_val = (old_state.state or "").lower() if old_state else ""

            # Entity entered the active state → start capture
            if new_val == active_state and old_val != active_state:
                # Cancel any pending stop debounce
                timer = self._debounce_timers.pop(profile_id, None)
                if timer:
                    timer.cancel()
                self.hass.async_create_task(
                    self._handle_start(profile_id, profile)
                )

            # Entity left the active state → check if new state is excluded
            elif old_val == active_state and new_val != active_state:
                if new_val in exclude_states:
                    _LOGGER.debug(
                        "Profile %s: ignoring excluded state '%s'",
                        profile_id,
                        new_val,
                    )
                    return

                # Cancel any existing debounce
                timer = self._debounce_timers.pop(profile_id, None)
                if timer:
                    timer.cancel()

                if debounce > 0:
                    self._debounce_timers[profile_id] = (
                        self.hass.loop.call_later(
                            debounce,
                            lambda: self.hass.async_create_task(
                                self._handle_stop(profile_id, profile)
                            ),
                        )
                    )
                else:
                    self.hass.async_create_task(
                        self._handle_stop(profile_id, profile)
                    )

            # Entity moved from an excluded state to another non-active state
            elif (
                old_val in exclude_states
                and new_val != active_state
                and new_val not in exclude_states
                and profile_id in self.active_jobs
            ):
                # Cancel any existing debounce
                timer = self._debounce_timers.pop(profile_id, None)
                if timer:
                    timer.cancel()

                if debounce > 0:
                    self._debounce_timers[profile_id] = (
                        self.hass.loop.call_later(
                            debounce,
                            lambda: self.hass.async_create_task(
                                self._handle_stop(profile_id, profile)
                            ),
                        )
                    )
                else:
                    self.hass.async_create_task(
                        self._handle_stop(profile_id, profile)
                    )

        return _handler

    # ── Start capture ───────────────────────────────────────────

    async def _handle_start(
        self, profile_id: str, profile: dict[str, Any]
    ) -> None:
        """Create a ChronoSnap job when the trigger entity enters the active state."""
        if profile_id in self.active_jobs:
            _LOGGER.warning(
                "Profile %s already has active job %s, skipping start",
                profile_id,
                self.active_jobs[profile_id],
            )
            return

        interval = self._calculate_interval(profile)
        name = profile.get(CONF_PROFILE_NAME, profile_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        job_name = f"{name} {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        try:
            job = await self.client.create_job(
                name=job_name,
                url=profile.get(CONF_STREAM_URL, ""),
                stream_type=profile.get(CONF_STREAM_TYPE, "rtsp"),
                start_datetime=now_iso,
                interval_seconds=interval,
                framerate=profile.get(CONF_FPS, DEFAULT_FPS),
                capture_quality=profile.get(
                    CONF_CAPTURE_QUALITY, DEFAULT_CAPTURE_QUALITY
                ),
                tag_ids=profile.get(CONF_TAG_IDS),
            )
            job_id = job["id"]
            self.active_jobs[profile_id] = job_id
            self.profile_status[profile_id] = STATUS_CAPTURING
            self.capture_counts[profile_id] = 0
            await self._async_save()
            self._fire_update()

            _LOGGER.info(
                "Started job %s for profile %s (interval=%ds)",
                job_id,
                name,
                interval,
            )
        except ChronoSnapError as err:
            _LOGGER.error("Failed to create job for profile %s: %s", name, err)
            self.profile_status[profile_id] = STATUS_ERROR
            self._fire_update()

    def _calculate_interval(self, profile: dict[str, Any]) -> int:
        """Calculate the capture interval based on profile settings."""
        mode = profile.get(CONF_INTERVAL_MODE, "fixed")

        if mode == INTERVAL_MODE_TARGET:
            target_duration = profile.get(
                CONF_TARGET_DURATION, DEFAULT_TARGET_DURATION
            )
            fps = profile.get(CONF_FPS, DEFAULT_FPS)
            target_frames = target_duration * fps

            # Read total time from the duration entity
            duration_entity = profile.get(CONF_DURATION_ENTITY)
            if duration_entity:
                state = self.hass.states.get(duration_entity)
                if state and state.state not in ("unknown", "unavailable"):
                    try:
                        total_seconds = float(state.state)
                        if total_seconds > 0:
                            interval = int(total_seconds / target_frames)
                            return max(interval, MIN_INTERVAL_SECONDS)
                    except (ValueError, TypeError):
                        _LOGGER.warning(
                            "Cannot parse duration from %s (value: %s), using default",
                            duration_entity,
                            state.state,
                        )

            _LOGGER.warning(
                "Duration entity unavailable, falling back to default interval"
            )

        return max(
            profile.get(CONF_INTERVAL_SECONDS, DEFAULT_INTERVAL_SECONDS),
            MIN_INTERVAL_SECONDS,
        )

    # ── Stop capture & build video ──────────────────────────────

    async def _handle_stop(
        self, profile_id: str, profile: dict[str, Any]
    ) -> None:
        """Complete the job, build a video, and optionally clean up."""
        job_id = self.active_jobs.get(profile_id)
        if job_id is None:
            _LOGGER.debug("No active job for profile %s on stop", profile_id)
            return

        name = profile.get(CONF_PROFILE_NAME, profile_id)
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            # 1. Complete the job
            await self.client.complete_job(job_id, now_iso)
            _LOGGER.info("Completed job %s for profile %s", job_id, name)

            # 2. Start video build
            self.profile_status[profile_id] = STATUS_BUILDING
            self._fire_update()

            video_name = f"{name} {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            video = await self.client.create_video(
                job_id=job_id,
                name=video_name,
                framerate=profile.get(CONF_FPS, DEFAULT_FPS),
                quality=profile.get(CONF_QUALITY, DEFAULT_QUALITY),
                resolution=profile.get(CONF_RESOLUTION, DEFAULT_RESOLUTION),
                tag_ids=profile.get(CONF_TAG_IDS),
            )
            video_id = video["id"]
            _LOGGER.info(
                "Started video build %s for profile %s", video_id, name
            )

            # 3. Poll for completion in background
            task = self.hass.async_create_task(
                self._poll_and_cleanup(profile_id, profile, job_id, video_id)
            )
            self._video_tasks[profile_id] = task

        except ChronoSnapError as err:
            _LOGGER.error(
                "Failed to complete/build video for profile %s: %s", name, err
            )
            self.profile_status[profile_id] = STATUS_ERROR
            self.active_jobs.pop(profile_id, None)
            await self._async_save()
            self._fire_update()

    async def _poll_and_cleanup(
        self,
        profile_id: str,
        profile: dict[str, Any],
        job_id: int,
        video_id: int,
    ) -> None:
        """Poll video status and clean up when done."""
        name = profile.get(CONF_PROFILE_NAME, profile_id)
        auto_cleanup = profile.get(CONF_AUTO_CLEANUP, DEFAULT_AUTO_CLEANUP)

        try:
            video = await self.client.poll_video_until_complete(
                video_id,
                poll_interval=VIDEO_POLL_INTERVAL,
                timeout=VIDEO_POLL_TIMEOUT,
            )
            status = video.get("status", "unknown")
            if status == "completed":
                _LOGGER.info(
                    "Video %s completed for profile %s", video_id, name
                )
            else:
                _LOGGER.warning(
                    "Video %s finished with status '%s' for profile %s",
                    video_id,
                    status,
                    name,
                )

            # 4. Delete job (preserves video)
            if auto_cleanup:
                await self.client.delete_job(job_id)
                _LOGGER.info(
                    "Deleted job %s for profile %s (video preserved)",
                    job_id,
                    name,
                )

        except ChronoSnapError as err:
            _LOGGER.error(
                "Error during video poll/cleanup for profile %s: %s",
                name,
                err,
            )
        finally:
            self.active_jobs.pop(profile_id, None)
            self.profile_status[profile_id] = STATUS_IDLE
            self.capture_counts[profile_id] = 0
            self._video_tasks.pop(profile_id, None)
            await self._async_save()
            self._fire_update()

    # ── Sensor update callbacks ─────────────────────────────────

    def register_update_callback(self, callback_fn) -> None:
        """Register a callback for sensor updates."""
        self._update_callbacks.append(callback_fn)

    def unregister_update_callback(self, callback_fn) -> None:
        """Remove a sensor update callback."""
        self._update_callbacks = [
            cb for cb in self._update_callbacks if cb is not callback_fn
        ]

    def _fire_update(self) -> None:
        """Notify all registered sensors of a state change."""
        for cb in self._update_callbacks:
            cb()

    async def async_update_capture_counts(self) -> None:
        """Fetch current capture counts for all active jobs."""
        for profile_id, job_id in self.active_jobs.items():
            try:
                count = await self.client.get_capture_count(job_id)
                self.capture_counts[profile_id] = count
            except ChronoSnapError:
                pass
        self._fire_update()
