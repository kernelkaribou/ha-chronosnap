"""Async API client for ChronoSnap."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


class ChronoSnapError(Exception):
    """Base exception for ChronoSnap API errors."""


class ChronoSnapConnectionError(ChronoSnapError):
    """Raised when unable to connect to ChronoSnap."""


class ChronoSnapAuthError(ChronoSnapError):
    """Raised when API key is invalid."""


class ChronoSnapClient:
    """Async client for the ChronoSnap REST API."""

    def __init__(
        self,
        url: str,
        api_key: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._base_url = url.rstrip("/")
        self._api_key = api_key
        self._session = session
        self._owns_session = session is None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self._api_key,
            "Content-Type": "application/json",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        """Close the client session if we own it."""
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Make an API request to ChronoSnap."""
        session = await self._get_session()
        url = f"{self._base_url}/api{path}"

        try:
            async with session.request(
                method,
                url,
                headers=self._headers,
                json=json,
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
            ) as resp:
                if resp.status == 401:
                    raise ChronoSnapAuthError("Invalid API key")
                if resp.status == 204:
                    return None
                if resp.status >= 400:
                    text = await resp.text()
                    raise ChronoSnapError(
                        f"API error {resp.status}: {text}"
                    )
                return await resp.json()
        except aiohttp.ClientError as err:
            raise ChronoSnapConnectionError(
                f"Unable to connect to ChronoSnap at {self._base_url}: {err}"
            ) from err

    # ── Connection ──────────────────────────────────────────────

    async def test_connection(self) -> dict[str, Any]:
        """Test connection and auth by fetching version info."""
        return await self._request("GET", "/settings/version")

    # ── Jobs ────────────────────────────────────────────────────

    async def create_job(
        self,
        name: str,
        url: str,
        stream_type: str,
        start_datetime: str,
        interval_seconds: int,
        framerate: int = 30,
        capture_quality: str = "high",
        capture_resolution: str = "native",
    ) -> dict[str, Any]:
        """Create a new capture job."""
        payload = {
            "name": name,
            "url": url,
            "stream_type": stream_type,
            "start_datetime": start_datetime,
            "interval_seconds": interval_seconds,
            "framerate": framerate,
            "capture_quality": capture_quality,
            "capture_resolution": capture_resolution,
        }
        return await self._request("POST", "/jobs/", json=payload)

    async def get_job(self, job_id: int) -> dict[str, Any]:
        """Get job details."""
        return await self._request("GET", f"/jobs/{job_id}")

    async def complete_job(
        self, job_id: int, end_datetime: str
    ) -> dict[str, Any]:
        """Mark a job as completed."""
        payload = {
            "status": "completed",
            "end_datetime": end_datetime,
        }
        return await self._request("PATCH", f"/jobs/{job_id}", json=payload)

    async def delete_job(self, job_id: int) -> None:
        """Delete a job and its captures (videos are preserved)."""
        await self._request("DELETE", f"/jobs/{job_id}")

    # ── Videos ──────────────────────────────────────────────────

    async def create_video(
        self,
        job_id: int,
        name: str,
        framerate: int = 30,
        quality: str = "high",
        resolution: str = "1920x1080",
    ) -> dict[str, Any]:
        """Create a timelapse video from job captures."""
        payload = {
            "job_id": job_id,
            "name": name,
            "framerate": framerate,
            "quality": quality,
            "resolution": resolution,
        }
        return await self._request("POST", "/videos/", json=payload)

    async def get_video(self, video_id: int) -> dict[str, Any]:
        """Get video status and details."""
        return await self._request("GET", f"/videos/{video_id}")

    async def poll_video_until_complete(
        self,
        video_id: int,
        poll_interval: int = 30,
        timeout: int = 3600,
    ) -> dict[str, Any]:
        """Poll video status until completed or failed."""
        elapsed = 0
        while elapsed < timeout:
            video = await self.get_video(video_id)
            status = video.get("status", "")
            if status in ("completed", "failed"):
                return video
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        raise ChronoSnapError(
            f"Video {video_id} did not complete within {timeout}s"
        )

    # ── Captures ────────────────────────────────────────────────

    async def get_capture_count(self, job_id: int) -> int:
        """Get the number of captures for a job."""
        result = await self._request(
            "GET", f"/captures/job/{job_id}/count"
        )
        return result.get("count", 0) if result else 0
