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


class ChronoSnapNotFoundError(ChronoSnapError):
    """Raised when a requested resource no longer exists."""


class ChronoSnapAPIError(ChronoSnapError):
    """An HTTP error with a status code suitable for retry decisions."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"ChronoSnap returned HTTP {status}")


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
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
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
                if resp.status in (401, 403):
                    raise ChronoSnapAuthError("Invalid API key")
                if resp.status == 404:
                    raise ChronoSnapNotFoundError("ChronoSnap resource not found")
                if resp.status == 204:
                    return None
                if resp.status >= 400:
                    raise ChronoSnapAPIError(resp.status)
                return await resp.json()
        except TimeoutError as err:
            raise ChronoSnapConnectionError("Timeout connecting to ChronoSnap") from err
        except aiohttp.ContentTypeError as err:
            raise ChronoSnapError("ChronoSnap returned a non-JSON response") from err
        except aiohttp.ClientError as err:
            raise ChronoSnapConnectionError("Unable to connect to ChronoSnap") from err
        except ValueError as err:
            raise ChronoSnapError("ChronoSnap returned invalid JSON") from err

    async def _get_object(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        result = await self._request(method, path, json)
        if not isinstance(result, dict):
            raise ChronoSnapError("Expected an object from ChronoSnap")
        return result

    async def _get_list(self, path: str) -> list[dict[str, Any]]:
        result = await self._request("GET", path)
        if not isinstance(result, list) or not all(
            isinstance(item, dict) for item in result
        ):
            raise ChronoSnapError("Expected a list of objects from ChronoSnap")
        return result

    # ── Connection ──────────────────────────────────────────────

    async def test_connection(self) -> dict[str, Any]:
        """Test connection and auth by fetching version info."""
        return await self._get_object("GET", "/settings/version")

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
        tag_ids: list[int] | None = None,
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
        if tag_ids:
            payload["tag_ids"] = tag_ids
        return await self._get_object("POST", "/jobs/", json=payload)

    async def get_job(self, job_id: int) -> dict[str, Any]:
        """Get job details."""
        return await self._get_object("GET", f"/jobs/{job_id}")

    async def complete_job(self, job_id: int, end_datetime: str) -> dict[str, Any]:
        """Mark a job as completed."""
        payload = {
            "status": "completed",
            "end_datetime": end_datetime,
        }
        return await self._get_object("PATCH", f"/jobs/{job_id}", json=payload)

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
        tag_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """Create a timelapse video from job captures."""
        payload = {
            "job_id": job_id,
            "name": name,
            "framerate": framerate,
            "quality": quality,
            "resolution": resolution,
        }
        if tag_ids:
            payload["tag_ids"] = tag_ids
        return await self._get_object("POST", "/videos/", json=payload)

    async def get_video(self, video_id: int) -> dict[str, Any]:
        """Get video status and details."""
        return await self._get_object("GET", f"/videos/{video_id}")

    async def poll_video_until_complete(
        self,
        video_id: int,
        poll_interval: int = 30,
        timeout: int = 3600,
    ) -> dict[str, Any]:
        """Poll video status until completed or failed."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            video = await self.get_video(video_id)
            status = video.get("status", "")
            if status in ("completed", "failed"):
                return video
            await asyncio.sleep(
                min(poll_interval, max(0, deadline - asyncio.get_running_loop().time()))
            )

        raise ChronoSnapError(f"Video {video_id} did not complete within {timeout}s")

    # ── Captures ────────────────────────────────────────────────

    async def get_capture_count(self, job_id: int) -> int:
        """Get the number of captures for a job."""
        result = await self._get_object("GET", f"/captures/job/{job_id}/count")
        count = result.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ChronoSnapError("Invalid capture count from ChronoSnap")
        return count

    # ── Tags ────────────────────────────────────────────────────

    async def get_tags(self) -> list[dict[str, Any]]:
        """Get all available tags."""
        tags = await self._get_list("/tags/")
        if any(
            isinstance(tag.get("id"), bool)
            or not isinstance(tag.get("id"), int)
            or tag["id"] <= 0
            or not isinstance(tag.get("name"), str)
            or not tag["name"]
            for tag in tags
        ):
            raise ChronoSnapError("Invalid tags returned by ChronoSnap")
        return tags

    # ── Server stats ───────────────────────────────────────────

    async def get_storage_stats(self) -> dict[str, Any]:
        """Get server storage statistics."""
        return await self._get_object("GET", "/storage/stats")

    async def get_jobs(self, status: str | None = None) -> list[dict[str, Any]]:
        """List jobs, optionally filtered by status."""
        jobs: list[dict[str, Any]] = []
        offset = 0
        while True:
            path = f"/jobs/?limit=1000&offset={offset}"
            if status:
                path += f"&status={status}"
            page = await self._get_list(path)
            jobs.extend(page)
            if len(page) < 1000:
                return jobs
            offset += len(page)
