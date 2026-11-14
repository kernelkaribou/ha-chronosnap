"""Deliberately exclude API keys, camera URLs, and raw server responses."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    return {
        "profiles": {
            pid: {
                "enabled": coordinator.is_enabled(pid),
                "status": coordinator.status(pid),
                "last_job_id": coordinator.last_jobs.get(pid),
                "last_video_id": coordinator.last_videos.get(pid),
                "last_error": coordinator.last_errors.get(pid),
            }
            for pid in coordinator.profiles
        },
        "recordings": {
            pid: {
                "job_id": recording.job_id,
                "video_id": recording.video_id,
                "phase": recording.phase,
            }
            for pid, recording in coordinator.recordings.items()
        },
        "retained_failed_builds": {
            pid: [
                {"job_id": recording.job_id, "video_id": recording.video_id}
                for recording in recordings
            ]
            for pid, recordings in coordinator.failed_recordings.items()
            if recordings
        },
    }
