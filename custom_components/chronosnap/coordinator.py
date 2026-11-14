"""Profile controls, serialized recording lifecycles, and restart recovery."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Coroutine, Iterable
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store

from .api import (
    ChronoSnapAPIError,
    ChronoSnapClient,
    ChronoSnapConnectionError,
    ChronoSnapError,
    ChronoSnapNotFoundError,
)
from .const import (
    CONF_ACTIVE_STATE,
    CONF_AUTO_CLEANUP,
    CONF_CAPTURE_QUALITY,
    CONF_DEBOUNCE_SECONDS,
    CONF_DURATION_ENTITY,
    CONF_ENABLED,
    CONF_EXCLUDE_STATES,
    CONF_FPS,
    CONF_INTERVAL_MODE,
    CONF_INTERVAL_SECONDS,
    CONF_PRESERVE_FAILED_FRAMES,
    CONF_PROFILE_NAME,
    CONF_PROFILES,
    CONF_QUALITY,
    CONF_RESOLUTION,
    CONF_START_DELAY,
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
    DEFAULT_PRESERVE_FAILED_FRAMES,
    DEFAULT_QUALITY,
    DEFAULT_RESOLUTION,
    DEFAULT_START_DELAY,
    DEFAULT_TARGET_DURATION,
    DOMAIN,
    INTERVAL_MODE_TARGET,
    MIN_INTERVAL_SECONDS,
    PROFILE_POLL_INTERVAL,
    STATUS_BUILDING,
    STATUS_CAPTURING,
    STATUS_DISABLED,
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_STARTING,
    STATUS_STOPPING,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class Recording:
    """Persist the phase and original settings until the recording is resolved."""

    job_id: int
    profile: dict[str, Any]
    phase: Literal[
        "capturing", "completed", "building", "failed", "cleanup", "cleanup_failed"
    ] = "capturing"
    video_id: int | None = None


class ProfileCoordinator:
    """Own profile controls independently of whether sensor entities are loaded."""

    STOP_MAX_RETRIES = 3
    STOP_RETRY_DELAY = 10
    SHUTDOWN_CONCURRENCY = 4

    def __init__(
        self, hass: HomeAssistant, client: ChronoSnapClient, entry: ConfigEntry
    ) -> None:
        self.hass = hass
        self.client = client
        self.entry = entry
        self.profiles: dict[str, dict[str, Any]] = deepcopy(
            entry.options.get(CONF_PROFILES, {})
        )
        self.recordings: dict[str, Recording] = {}
        self.failed_recordings: dict[str, list[Recording]] = {}
        self.capture_counts: dict[str, int | None] = {}
        self.last_errors: dict[str, str] = {}
        self.last_jobs: dict[str, int] = {}
        self.last_videos: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._listeners: list[CALLBACK_TYPE] = []
        self._start_timers: dict[str, asyncio.TimerHandle] = {}
        self._stop_timers: dict[str, asyncio.TimerHandle] = {}
        self._start_generations: dict[str, int] = {}
        self._stop_generations: dict[str, int] = {}
        self._stop_due: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._update_callbacks: list[Callable[[], None]] = []
        self._running = False
        self._ready = False
        self._polling = False
        self._save_lock = asyncio.Lock()
        self._store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry.entry_id}")

    @property
    def active_jobs(self) -> dict[str, int]:
        """Return tracked captures, excluding jobs already stopped."""
        return {
            pid: rec.job_id
            for pid, rec in self.recordings.items()
            if rec.phase == "capturing"
        }

    def _lock(self, profile_id: str) -> asyncio.Lock:
        return self._locks.setdefault(profile_id, asyncio.Lock())

    @property
    def has_pending_work(self) -> bool:
        """Include queued and in-flight starts before changing server identity."""
        return bool(
            self.recordings
            or any(self.failed_recordings.values())
            or self._tasks
            or self._start_timers
            or any(lock.locked() for lock in self._locks.values())
        )

    def is_enabled(self, profile_id: str) -> bool:
        """Respect both the persisted profile control and HA's device control."""
        profile = self.profiles.get(profile_id)
        if profile is None or not profile.get(CONF_ENABLED, True):
            return False
        identifier = (DOMAIN, f"{self.entry.entry_id}_{profile_id}")
        device = next(
            (
                device
                for device in dr.async_entries_for_config_entry(
                    dr.async_get(self.hass), self.entry.entry_id
                )
                if identifier in device.identifiers
            ),
            None,
        )
        return device is None or device.disabled_by is None

    def can_start(self, profile_id: str) -> bool:
        recording = self.recordings.get(profile_id)
        return recording is None or recording.phase == "failed"

    def status(self, profile_id: str) -> str:
        if profile_id in self.last_errors:
            return STATUS_ERROR
        if profile_id in self._start_timers:
            return STATUS_STARTING
        if profile_id in self._stop_timers:
            return STATUS_STOPPING
        if recording := self.recordings.get(profile_id):
            return (
                STATUS_CAPTURING if recording.phase == "capturing" else STATUS_BUILDING
            )
        if self.failed_recordings.get(profile_id):
            return STATUS_ERROR
        return STATUS_IDLE if self.is_enabled(profile_id) else STATUS_DISABLED

    async def async_load(self) -> None:
        """Migrate the shared legacy store to an independent per-entry store."""
        data = await self._store.async_load()
        if data is None:
            legacy = (
                await Store(self.hass, STORAGE_VERSION, STORAGE_KEY).async_load() or {}
            )
            for pid, job_id in legacy.get(self.entry.entry_id, {}).items():
                self.recordings[pid] = Recording(
                    job_id, deepcopy(self.profiles.get(pid, {}))
                )
        else:
            self.recordings = {
                pid: Recording(**record)
                for pid, record in data.get("recordings", {}).items()
            }
            self.failed_recordings = {
                pid: [Recording(**record) for record in records]
                for pid, records in data.get("failed_recordings", {}).items()
            }
            self.last_errors = data.get("last_errors", {})
            self.last_jobs = data.get("last_jobs", {})
            self.last_videos = data.get("last_videos", {})
        await self._async_save()

    async def _async_save(self) -> None:
        async with self._save_lock:
            await self._store.async_save(
                {
                    "recordings": {
                        pid: asdict(record) for pid, record in self.recordings.items()
                    },
                    "failed_recordings": {
                        pid: [asdict(record) for record in records]
                        for pid, records in self.failed_recordings.items()
                        if records
                    },
                    "last_errors": dict(self.last_errors),
                    "last_jobs": dict(self.last_jobs),
                    "last_videos": dict(self.last_videos),
                }
            )

    def _spawn(self, operation: Coroutine[Any, Any, None]) -> None:
        task = self.hass.async_create_task(operation)
        self._tasks.add(task)

        def finished(done: asyncio.Task) -> None:
            self._tasks.discard(done)
            if not done.cancelled() and (error := done.exception()) is not None:
                _LOGGER.error(
                    "Unexpected ChronoSnap lifecycle error",
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(finished)

    async def async_start(self) -> None:
        """Subscribe only after setup succeeds; reconcile once HA is ready."""
        self._running = True
        for pid, profile in self.profiles.items():
            self._listeners.append(
                async_track_state_change_event(
                    self.hass,
                    [profile[CONF_TRIGGER_ENTITY]],
                    self._make_state_handler(pid),
                )
            )
        self._listeners.append(
            self.hass.bus.async_listen(
                dr.EVENT_DEVICE_REGISTRY_UPDATED, self._device_updated
            )
        )
        self._listeners.append(
            async_track_time_interval(
                self.hass, self._async_poll, timedelta(seconds=PROFILE_POLL_INTERVAL)
            )
        )
        if self.hass.is_running:
            self._ready = True
            await self._async_poll()
        else:
            self._listeners.append(
                self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED, self._ha_started
                )
            )

    async def _ha_started(self, event: Event) -> None:
        self._ready = True
        await self._async_poll()

    @callback
    def _device_updated(self, event: Event) -> None:
        device = dr.async_get(self.hass).async_get(event.data["device_id"])
        if device is None or "disabled_by" not in event.data.get("changes", {}):
            return
        for pid in self.profiles:
            if (DOMAIN, f"{self.entry.entry_id}_{pid}") in device.identifiers:
                if not self.is_enabled(pid):
                    self._cancel_timers(pid)
                    self._spawn(self._handle_stop(pid))
                self._fire_update()
                return

    def teardown_listeners(self) -> None:
        """Synchronously close the start gate before awaiting any cleanup."""
        self._running = False
        for unsub in self._listeners:
            unsub()
        self._listeners.clear()
        for pid in (
            self.profiles.keys()
            | self.recordings.keys()
            | self._start_generations.keys()
            | self._stop_generations.keys()
        ):
            self._cancel_timers(pid)

    async def async_shutdown(self, stop_jobs: bool = False) -> None:
        """Drain in-flight API operations without losing their server job IDs."""
        self.teardown_listeners()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks))
        for pid in tuple(self._locks):
            async with self._lock(pid):
                pass
        if stop_jobs:
            await self._async_process_profiles(self.recordings, self._handle_stop)
        await self._async_save()

    async def _async_process_profiles(
        self,
        profile_ids: Iterable[str],
        operation: Callable[[str], Coroutine[Any, Any, None]],
    ) -> None:
        semaphore = asyncio.Semaphore(self.SHUTDOWN_CONCURRENCY)

        async def process(profile_id: str) -> None:
            async with semaphore:
                await operation(profile_id)

        await asyncio.gather(*(process(pid) for pid in tuple(profile_ids)))

    async def async_prepare_removal(self) -> None:
        """Stop capture and finish already-resolved builds without a detached worker."""
        await self.async_shutdown(stop_jobs=True)

        async def finish(profile_id: str) -> None:
            async with self._lock(profile_id):
                if self.recordings[profile_id].phase in (
                    "building",
                    "cleanup",
                    "cleanup_failed",
                ):
                    await self._reconcile(profile_id)

        await self._async_process_profiles(self.recordings, finish)
        if not self.recordings and not any(self.failed_recordings.values()):
            await self._store.async_remove()

    def _cancel_timer(
        self, timers: dict[str, asyncio.TimerHandle], profile_id: str
    ) -> None:
        if timer := timers.pop(profile_id, None):
            timer.cancel()

    def _cancel_timers(self, profile_id: str) -> None:
        self._cancel_start(profile_id)
        self._cancel_stop(profile_id)

    def _cancel_start(self, profile_id: str) -> None:
        self._cancel_timer(self._start_timers, profile_id)
        self._start_generations[profile_id] = (
            self._start_generations.get(profile_id, 0) + 1
        )

    def _cancel_stop(self, profile_id: str) -> None:
        self._cancel_timer(self._stop_timers, profile_id)
        self._stop_generations[profile_id] = (
            self._stop_generations.get(profile_id, 0) + 1
        )
        self._stop_due.discard(profile_id)

    def _trigger_value(self, profile: dict[str, Any]) -> str | None:
        state = self.hass.states.get(profile.get(CONF_TRIGGER_ENTITY, ""))
        if state is None:
            return None
        return state.state.lower()

    @staticmethod
    def _excluded(profile: dict[str, Any]) -> set[str]:
        return {
            value.strip().lower()
            for value in profile.get(CONF_EXCLUDE_STATES, "").split(",")
            if value.strip()
        }

    def _schedule_start(self, profile_id: str) -> None:
        self._cancel_start(profile_id)
        generation = self._start_generations[profile_id]
        delay = self.profiles[profile_id].get(CONF_START_DELAY, DEFAULT_START_DELAY)
        if delay:

            @callback
            def start() -> None:
                self._start_timers.pop(profile_id, None)
                self._spawn(self._handle_start(profile_id, generation=generation))

            self._start_timers[profile_id] = self.hass.loop.call_later(delay, start)
        else:
            self._spawn(self._handle_start(profile_id, generation=generation))
        self._fire_update()

    def _schedule_stop(self, profile_id: str) -> None:
        self._cancel_stop(profile_id)
        generation = self._stop_generations[profile_id]
        profile = self.recordings[profile_id].profile
        delay = profile.get(CONF_DEBOUNCE_SECONDS, DEFAULT_DEBOUNCE_SECONDS)
        if delay:

            @callback
            def stop() -> None:
                self._stop_timers.pop(profile_id, None)
                self._stop_due.add(profile_id)
                self._spawn(
                    self._handle_stop(
                        profile_id, check_trigger=True, generation=generation
                    )
                )

            self._stop_timers[profile_id] = self.hass.loop.call_later(delay, stop)
        else:
            self._spawn(
                self._handle_stop(profile_id, check_trigger=True, generation=generation)
            )
        self._fire_update()

    def _make_state_handler(self, profile_id: str) -> Callable[[Event], None]:
        @callback
        def handler(event: Event) -> None:
            if not self._running or not self._ready:
                return
            profile = self.profiles[profile_id]
            state = event.data.get("new_state")
            old_state = event.data.get("old_state")
            value = state.state.lower() if state else None
            old_value = old_state.state.lower() if old_state else None
            if value == old_value:
                return
            if not self.is_enabled(profile_id):
                self._cancel_timers(profile_id)
                if profile_id in self.recordings:
                    self._spawn(self._handle_stop(profile_id))
                return
            active = profile[CONF_ACTIVE_STATE].lower()
            if value == active:
                self._cancel_stop(profile_id)
                if self.can_start(profile_id):
                    self._schedule_start(profile_id)
            else:
                self._cancel_start(profile_id)
                if value in self._excluded(profile):
                    self._cancel_stop(profile_id)
                elif (
                    profile_id in self.active_jobs
                    and profile_id not in self._stop_timers
                ):
                    self._schedule_stop(profile_id)
            self._fire_update()

        return handler

    async def async_set_enabled(self, profile_id: str, enabled: bool) -> None:
        """Persist the control without reloading any profile."""
        if not self._running:
            raise HomeAssistantError("ChronoSnap is unloading; try again after reload")
        profiles = deepcopy(self.entry.options.get(CONF_PROFILES, {}))
        profiles[profile_id][CONF_ENABLED] = enabled
        self.profiles[profile_id][CONF_ENABLED] = enabled
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, CONF_PROFILES: profiles}
        )
        if not enabled:
            self._cancel_timers(profile_id)
            await self._handle_stop(profile_id)
        self._fire_update()
        if not enabled and profile_id in self.active_jobs:
            raise HomeAssistantError(
                "The profile is disabled, but its server capture could not be stopped. Check ChronoSnap; automatic recovery will keep retrying."
            )

    async def async_apply_enabled(self, profiles: dict[str, dict[str, Any]]) -> None:
        disabled = []
        for pid, profile in profiles.items():
            enabled = profile.get(CONF_ENABLED, True)
            if self.profiles[pid].get(CONF_ENABLED, True) == enabled:
                continue
            self.profiles[pid][CONF_ENABLED] = enabled
            if not self.is_enabled(pid):
                self._cancel_timers(pid)
                disabled.append(pid)
        self._fire_update()
        await self._async_process_profiles(disabled, self._handle_stop)

    async def async_start_now(self, profile_id: str) -> None:
        """An explicit opt-in for recording a process that is already active."""
        if not self._running or not self._ready or not self.is_enabled(profile_id):
            raise HomeAssistantError("Enable Automatic timelapse before starting")
        if (
            self._trigger_value(self.profiles[profile_id])
            != self.profiles[profile_id][CONF_ACTIVE_STATE].lower()
        ):
            raise HomeAssistantError("The trigger entity is not in its active state")
        if not self.can_start(profile_id):
            raise HomeAssistantError("This profile already has a recording to finish")
        self._cancel_start(profile_id)
        generation = self._start_generations[profile_id]
        await self._handle_start(profile_id, generation=generation)
        if (
            generation != self._start_generations[profile_id]
            and profile_id not in self.active_jobs
        ):
            raise HomeAssistantError(
                "The start request was superseded by a profile or trigger change"
            )
        if error := self.last_errors.get(profile_id):
            raise HomeAssistantError(error)

    async def async_retry(self, profile_id: str) -> None:
        """Retry recovery/building without silently starting another recording."""
        async with self._lock(profile_id):
            recording = self.recordings.get(profile_id)
            if (
                recording is not None
                and recording.phase == "failed"
                and self.failed_recordings.get(profile_id)
            ):
                self.failed_recordings[profile_id].append(recording)
                recording = self.failed_recordings[profile_id].pop(0)
                self.recordings[profile_id] = recording
                await self._async_save()
            if recording is None and self.failed_recordings.get(profile_id):
                recording = self.failed_recordings[profile_id].pop(0)
                self.recordings[profile_id] = recording
                await self._async_save()
            if recording is None:
                raise HomeAssistantError(
                    "There is no tracked recording to recover. Check the last error and video in ChronoSnap."
                )
            if recording.phase == "building" and recording.video_id is not None:
                try:
                    await self.client.get_video(recording.video_id)
                except ChronoSnapNotFoundError:
                    recording.phase = "completed"
                    recording.video_id = None
                    await self._async_save()
                except ChronoSnapError as err:
                    await self._error(profile_id, f"Recording recovery failed: {err}")
                    raise HomeAssistantError(self.last_errors[profile_id]) from err
            if recording.phase == "failed":
                try:
                    await self.client.get_job(recording.job_id)
                except ChronoSnapNotFoundError:
                    self.recordings.pop(profile_id)
                    await self._error(
                        profile_id,
                        "The retained source job was deleted on ChronoSnap. Its recovery tracking was released.",
                    )
                    raise HomeAssistantError(self.last_errors[profile_id]) from None
                except ChronoSnapError as err:
                    await self._error(profile_id, f"Recording recovery failed: {err}")
                    raise HomeAssistantError(self.last_errors[profile_id]) from err
                recording.phase = "completed"
                recording.video_id = None
                await self._async_save()
            await self._reconcile(profile_id, retry=True)
            if error := self.last_errors.get(profile_id):
                raise HomeAssistantError(error)

    async def async_remove_profile(self, profile_id: str) -> None:
        """Disarm before deleting and refuse deletion if capture cannot be stopped."""
        await self.async_set_enabled(profile_id, False)
        recording = self.recordings.get(profile_id)
        if recording and recording.phase == "capturing":
            raise HomeAssistantError(
                "The server job could not be stopped. The profile remains disabled; retry deletion when the server is available."
            )

    async def _handle_start(
        self, profile_id: str, *, generation: int | None = None
    ) -> None:
        async with self._lock(profile_id):
            if generation is not None and generation != self._start_generations.get(
                profile_id
            ):
                return
            if (
                not self._running
                or not self.is_enabled(profile_id)
                or not self.can_start(profile_id)
            ):
                return
            profile = deepcopy(self.profiles[profile_id])
            if self._trigger_value(profile) != profile[CONF_ACTIVE_STATE].lower():
                return
            if previous := self.recordings.pop(profile_id, None):
                self.failed_recordings.setdefault(profile_id, []).append(previous)
                await self._async_save()
            self.last_errors.pop(profile_id, None)
            try:
                job = await self.client.create_job(
                    name=f"{profile[CONF_PROFILE_NAME]} {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                    url=profile[CONF_STREAM_URL],
                    stream_type=profile[CONF_STREAM_TYPE],
                    start_datetime=datetime.now(UTC).isoformat(),
                    interval_seconds=self._calculate_interval(profile),
                    framerate=profile.get(CONF_FPS, DEFAULT_FPS),
                    capture_quality=profile.get(
                        CONF_CAPTURE_QUALITY, DEFAULT_CAPTURE_QUALITY
                    ),
                    tag_ids=profile.get(CONF_TAG_IDS),
                )
                job_id = self._resource_id(job)
                self.recordings[profile_id] = Recording(job_id, profile)
                self.last_jobs[profile_id] = job_id
                self.capture_counts[profile_id] = 0
                await self._async_save()
                if not self._running or not self.is_enabled(profile_id):
                    await self._finish(profile_id)
                elif self._trigger_value(profile) not in (
                    {profile[CONF_ACTIVE_STATE].lower()} | self._excluded(profile)
                ):
                    self._schedule_stop(profile_id)
            except ChronoSnapError as err:
                await self._error(profile_id, f"Could not start recording: {err}")
            self._fire_update()

    @staticmethod
    def _resource_id(resource: dict[str, Any]) -> int:
        value = resource.get("id")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ChronoSnapError(
                "Server returned an invalid resource ID; check ChronoSnap before retrying"
            )
        return value

    async def _handle_stop(
        self,
        profile_id: str,
        check_trigger: bool = False,
        *,
        generation: int | None = None,
    ) -> None:
        async with self._lock(profile_id):
            if generation is not None and generation != self._stop_generations.get(
                profile_id
            ):
                return
            recording = self.recordings.get(profile_id)
            if recording is None or recording.phase != "capturing":
                return
            if check_trigger and self.is_enabled(profile_id):
                value = self._trigger_value(recording.profile)
                if value in (
                    {recording.profile.get(CONF_ACTIVE_STATE, "").lower()}
                    | self._excluded(recording.profile)
                ):
                    return
            await self._finish(profile_id)

    async def _finish(self, profile_id: str) -> None:
        """Only called under the profile lock; retain every unfinished phase."""
        recording = self.recordings[profile_id]
        self._cancel_timers(profile_id)
        try:
            if recording.phase == "capturing":
                end = datetime.now(UTC).isoformat()
                for attempt in range(self.STOP_MAX_RETRIES):
                    try:
                        await self.client.complete_job(recording.job_id, end)
                        break
                    except (ChronoSnapConnectionError, ChronoSnapAPIError) as err:
                        transient = (
                            isinstance(err, ChronoSnapConnectionError)
                            or err.status == 429
                            or err.status >= 500
                        )
                        if not transient or attempt == self.STOP_MAX_RETRIES - 1:
                            raise
                        await asyncio.sleep(self.STOP_RETRY_DELAY)
                recording.phase = "completed"
                await self._async_save()
            if recording.phase == "completed":
                profile = recording.profile
                video = await self.client.create_video(
                    job_id=recording.job_id,
                    name=f"{profile.get(CONF_PROFILE_NAME, profile_id)} {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                    framerate=profile.get(CONF_FPS, DEFAULT_FPS),
                    quality=profile.get(CONF_QUALITY, DEFAULT_QUALITY),
                    resolution=profile.get(CONF_RESOLUTION, DEFAULT_RESOLUTION),
                    tag_ids=profile.get(CONF_TAG_IDS),
                )
                recording.video_id = self._resource_id(video)
                recording.phase = "building"
                self.last_videos[profile_id] = recording.video_id
                await self._async_save()
            self.last_errors.pop(profile_id, None)
        except ChronoSnapError as err:
            await self._error(profile_id, f"Could not finish recording: {err}")
        self._fire_update()

    async def _async_poll(self, now: datetime | None = None) -> None:
        if not self._running or not self._ready or self._polling:
            return
        self._polling = True
        try:
            for pid in tuple(self.recordings):
                if not self._running:
                    break
                async with self._lock(pid):
                    await self._reconcile(pid)
        finally:
            self._polling = False
            self._fire_update()

    async def _reconcile(self, profile_id: str, retry: bool = False) -> None:
        recording = self.recordings.get(profile_id)
        if recording is None:
            self.last_errors.pop(profile_id, None)
            await self._async_save()
            self._fire_update()
            return
        try:
            if recording.phase == "capturing":
                job = await self.client.get_job(recording.job_id)
                server_status = job.get("status")
                if server_status == "completed":
                    recording.phase = "completed"
                    self.last_errors.pop(profile_id, None)
                    await self._async_save()
                elif server_status not in ("active", "sleeping", "warning"):
                    raise ChronoSnapError(
                        "Tracked job is not capturing; inspect its status in ChronoSnap"
                    )
                else:
                    profile = self.profiles.get(profile_id, recording.profile)
                    value = self._trigger_value(profile)
                    active = profile.get(CONF_ACTIVE_STATE, "").lower()
                    if not self.is_enabled(profile_id):
                        await self._finish(profile_id)
                        return
                    if value not in ({active} | self._excluded(profile)):
                        if value is not None and value not in (
                            "unknown",
                            "unavailable",
                        ):
                            if retry or profile_id in self._stop_due:
                                await self._finish(profile_id)
                            elif profile_id not in self._stop_timers:
                                self._schedule_stop(profile_id)
                        return
                    self._cancel_stop(profile_id)
                    self.capture_counts[
                        profile_id
                    ] = await self.client.get_capture_count(recording.job_id)
                    if server_status == "warning":
                        await self._error(
                            profile_id,
                            "ChronoSnap reports capture failures for this job. Check the camera stream in ChronoSnap.",
                        )
                        return
            if recording.phase == "completed":
                if profile_id in self.last_errors and not retry:
                    return
                await self._finish(profile_id)
                return
            if recording.phase == "building":
                if recording.video_id is None:
                    raise ChronoSnapError("The tracked build is missing its video ID")
                video = await self.client.get_video(recording.video_id)
                if video.get("status") == "failed":
                    preserve = not recording.profile.get(
                        CONF_AUTO_CLEANUP, DEFAULT_AUTO_CLEANUP
                    ) or recording.profile.get(
                        CONF_PRESERVE_FAILED_FRAMES, DEFAULT_PRESERVE_FAILED_FRAMES
                    )
                    if preserve:
                        recording.phase = "failed"
                        await self._error(
                            profile_id,
                            "Video build failed. Source frames are preserved. Use Retry recovery or build the video in ChronoSnap.",
                        )
                        return
                    recording.phase = "cleanup_failed"
                    await self._async_save()
                elif video.get("status") == "completed":
                    recording.phase = "cleanup"
                    await self._async_save()
                elif video.get("status") != "processing":
                    raise ChronoSnapError(
                        "Unrecognized video status; source frames are preserved"
                    )
            if recording.phase in ("cleanup", "cleanup_failed"):
                if recording.profile.get(CONF_AUTO_CLEANUP, DEFAULT_AUTO_CLEANUP):
                    try:
                        await self.client.delete_job(recording.job_id)
                    except ChronoSnapNotFoundError:
                        pass
                self.recordings.pop(profile_id)
                if recording.phase == "cleanup_failed":
                    await self._error(
                        profile_id,
                        "Video build failed. Source frames were deleted by the profile cleanup policy; this recording cannot be rebuilt.",
                    )
            if recording.phase not in ("failed", "cleanup_failed"):
                self.last_errors.pop(profile_id, None)
            await self._async_save()
        except ChronoSnapNotFoundError:
            if recording.phase == "capturing":
                self.recordings.pop(profile_id)
                await self._error(
                    profile_id,
                    "The tracked job was deleted on ChronoSnap. Its tracking was released; no replacement recording was started.",
                )
                return
            await self._error(
                profile_id,
                "The tracked video is missing on ChronoSnap. Source frames are preserved. Use Retry recovery to attempt rebuilding it.",
            )
        except ChronoSnapError as err:
            self.capture_counts[profile_id] = None
            await self._error(profile_id, f"Recording recovery failed: {err}")
        self._fire_update()

    async def _error(self, profile_id: str, message: str) -> None:
        if self.last_errors.get(profile_id) != message:
            _LOGGER.error("Profile %s: %s", profile_id, message)
        self.last_errors[profile_id] = message
        await self._async_save()
        self._fire_update()

    def _calculate_interval(self, profile: dict[str, Any]) -> int:
        if profile.get(CONF_INTERVAL_MODE) == INTERVAL_MODE_TARGET:
            entity_id = profile.get(CONF_DURATION_ENTITY, "")
            state = self.hass.states.get(entity_id)
            if state and state.state not in ("unknown", "unavailable"):
                seconds = self._parse_duration_value(
                    state.state, state.attributes.get("unit_of_measurement")
                )
                if seconds is not None and seconds > 0:
                    frames = profile.get(
                        CONF_TARGET_DURATION, DEFAULT_TARGET_DURATION
                    ) * profile.get(CONF_FPS, DEFAULT_FPS)
                    return max(int(seconds / frames), MIN_INTERVAL_SECONDS)
            _LOGGER.warning(
                "Duration entity %s is unavailable or invalid; using the fixed interval",
                entity_id,
            )
        return max(
            int(profile.get(CONF_INTERVAL_SECONDS, DEFAULT_INTERVAL_SECONDS)),
            MIN_INTERVAL_SECONDS,
        )

    def _parse_duration_value(
        self, value: str, unit: str | None = None
    ) -> float | None:
        try:
            numeric = float(value)
        except ValueError:
            try:
                end = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            if end.tzinfo is None:
                end = end.replace(tzinfo=ZoneInfo(self.hass.config.time_zone))
            return (end - datetime.now(UTC)).total_seconds()
        factors = {
            None: 1,
            "s": 1,
            "sec": 1,
            "seconds": 1,
            "min": 60,
            "minutes": 60,
            "h": 3600,
            "hours": 3600,
        }
        if unit not in factors or not math.isfinite(numeric):
            return None
        return numeric * factors[unit]

    @callback
    def register_update_callback(
        self, callback_fn: Callable[[], None]
    ) -> CALLBACK_TYPE:
        self._update_callbacks.append(callback_fn)

        @callback
        def unsubscribe() -> None:
            self._update_callbacks.remove(callback_fn)

        return unsubscribe

    @callback
    def _fire_update(self) -> None:
        for update in tuple(self._update_callbacks):
            update()
