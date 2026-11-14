"""Run separately on Linux with real Home Assistant installed, without doubles."""

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import frame
from homeassistant.helpers.storage import Store

from custom_components.chronosnap.button import (
    ChronoSnapRetryButton,
    ChronoSnapStartButton,
)
from custom_components.chronosnap.config_flow import ChronoSnapOptionsFlow
from custom_components.chronosnap.coordinator import ProfileCoordinator
from custom_components.chronosnap.sensor import ChronoSnapStatusSensor
from custom_components.chronosnap.switch import ChronoSnapProfileSwitch


async def smoke():
    with tempfile.TemporaryDirectory() as config_dir:
        hass = HomeAssistant(config_dir)
        if setup_frame := getattr(frame, "async_setup", None):
            setup_frame(hass)
        if setup_registry := getattr(dr, "async_setup", None):
            setup_registry(hass)
        await dr.async_load(hass)
        profile = {
            "name": "Printer",
            "stream_url": "rtsp://camera/stream",
            "stream_type": "rtsp",
            "trigger_entity": "sensor.printer",
            "active_state": "printing",
            "debounce_seconds": 0,
            "enabled": True,
        }
        entry = SimpleNamespace(
            entry_id="test_entry",
            options={"profiles": {"p": profile}},
            data={"url": "http://chronosnap", "api_key": "test-key"},
            disabled_by=None,
        )
        client = SimpleNamespace(
            create_job=AsyncMock(return_value={"id": 1}),
            complete_job=AsyncMock(return_value={"id": 1}),
            create_video=AsyncMock(return_value={"id": 2}),
            get_job=AsyncMock(return_value={"id": 1, "status": "active"}),
            get_video=AsyncMock(return_value={"id": 2, "status": "failed"}),
            delete_job=AsyncMock(),
        )
        coordinator = ProfileCoordinator(hass, client, entry)
        coordinator._running = coordinator._ready = True
        hass.states.async_set("sensor.printer", "printing")
        try:
            await coordinator.async_load()
            options = ChronoSnapOptionsFlow(entry)
            options.hass = hass
            options._editing_profile_id = "p"
            form = await options.async_step_profile_basic()
            validated = form["data_schema"](profile)
            assert validated["enabled"] is True
            assert isinstance(
                ChronoSnapProfileSwitch(coordinator, entry, "p", profile), SwitchEntity
            )
            assert isinstance(
                ChronoSnapStartButton(coordinator, entry, "p", profile), ButtonEntity
            )
            assert isinstance(
                ChronoSnapRetryButton(coordinator, entry, "p", profile), ButtonEntity
            )
            sensor = ChronoSnapStatusSensor(coordinator, entry, "p", profile)
            assert isinstance(sensor, SensorEntity)
            await coordinator._handle_start("p")
            await coordinator._handle_stop("p")
            await coordinator._async_poll()
            assert sensor.native_value == "error"
            client.delete_job.assert_not_awaited()
            stored = await Store(
                hass, 1, "chronosnap_active_jobs_test_entry"
            ).async_load()
            assert stored["recordings"]["p"]["video_id"] == 2
            assert stored["recordings"]["p"]["phase"] == "failed"
            strings = json.loads(
                (Path("custom_components") / "chronosnap" / "strings.json").read_text()
            )
            assert "reauth_confirm" in strings["config"]["step"]
            assert (
                "preserve_failed_frames"
                in strings["options"]["step"]["profile_capture"]["data"]
            )
        finally:
            await coordinator.async_shutdown()
            await hass.async_stop()
    print(
        "Real Home Assistant imports, selectors, entities, storage, and lifecycle smoke checks passed"
    )


if __name__ == "__main__":
    asyncio.run(smoke())
