# ChronoSnap for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration that automatically creates [ChronoSnap](https://github.com/kernelkaribou/chronosnap) timelapses based on entity state changes.

## What It Does

Define **timelapse profiles** that watch any Home Assistant entity. When the entity enters a configured state, ChronoSnap automatically begins capturing frames from a camera stream. When the entity leaves that state, it builds a timelapse video and cleans up.

**Example use cases:**
- Timelapse every 3D print from start to finish, triggered by your printer's status entity
- Capture what your dog gets up to when the house is detected as empty
- Timelapse your backyard camera as a storm rolls through, triggered by a weather sensor
- Record the full run of a CNC or laser cutter job, triggered by the machine's power sensor
- Capture a daily sunrise or sunset from an outdoor camera using a sun elevation sensor
- Timelapse your fish tank during scheduled feeding times
- Timelapse your garage workbench whenever the garage door opens

## Features

- **UI-based configuration** — No YAML editing required
- **Entity & state selectors** — Pick entities from dropdowns
- **Multiple profiles** — Run different timelapses simultaneously
- **Flexible intervals:**
  - **Fixed** — Capture every N seconds
  - **Target duration** — Automatically calculate interval so the timelapse is a specific length (e.g., always 30 seconds)
- **Start delay** — Configurable grace period before job creation, so brief/cancelled triggers are ignored
- **Stop debounce** — Configurable delay before stopping to prevent false triggers
- **Exclude states** — Temporary states that should not interrupt an active capture
- **Tags** — Apply ChronoSnap tags to jobs and videos
- **Auto-cleanup** — Optionally delete raw captures after the video is built
- **Restart-safe** — Active job IDs are persisted and restored on HA restart

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** → **⋮** (top right) → **Custom repositories**
3. Add `https://github.com/kernelkaribou/ha-chronosnap` as an **Integration**
4. Click **Install**
5. Restart Home Assistant

### Manual

1. Copy `custom_components/chronosnap/` to your HA `config/custom_components/` directory
2. Restart Home Assistant

## Setup

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for **ChronoSnap**
3. Enter your ChronoSnap server URL and API key
4. Click **Submit** — the connection will be validated

## Adding Timelapse Profiles

1. Go to **Settings** → **Devices & Services** → **ChronoSnap** → **Configure**
2. Click **➕ Add new profile**
3. **Step 1 — Profile Setup:**
   - **Name:** A friendly name for this profile
   - **Camera stream URL:** RTSP/HTTP URL for the camera
   - **Stream type:** RTSP, HTTP, or local device
   - **Trigger entity:** The HA entity to watch for state changes
   - **Active state:** The state value that triggers capture
   - **Exclude states:** Comma-separated states that should not interrupt capturing
   - **Start delay:** Seconds to wait before creating the job (0 for immediate)
   - **Stop debounce:** Seconds to wait before stopping (prevents false triggers from brief state changes)
4. **Step 2 — Capture & Video Settings:**
   - **Interval mode:** Fixed or target duration
   - **Video framerate, quality, resolution**
   - **Auto-cleanup:** Delete raw captures after video is built
5. *(If target duration mode)* **Step 3 — Target Duration:**
   - **Target video duration:** Desired timelapse length in seconds
   - **Duration source entity:** Entity whose state contains the total expected duration in seconds

## Sensors

Each profile creates two sensors:

| Sensor | Description |
|---|---|
| `sensor.chronosnap_<name>_status` | Current status: `idle`, `capturing`, `building_video`, `error` |
| `sensor.chronosnap_<name>_captures` | Number of frames captured in the current job |

## How It Works

```
Entity enters active state
        │
        ▼
  Wait start delay
  (if configured)
        │
        ├── Entity leaves? → Cancel, no job created
        │
        ▼
  Calculate interval
  (fixed or from entity)
        │
        ▼
  Create ChronoSnap job ──→ Frames captured automatically
        │
        │  Entity leaves active state
        │  (after stop debounce delay)
        ▼
  Complete the job
        │
        ▼
  Build timelapse video
        │
        ▼
  Poll until complete
        │
        ▼
  Delete job (video preserved)
```

## Target Duration Mode

Instead of a fixed capture interval, you can specify a desired video length. The integration calculates the interval automatically:

```
interval = total_time_seconds / (target_duration × fps)
```

For example, with a 4-hour process, target of 30s at 30fps:
- Total frames needed: 30 × 30 = 900
- Interval: 14400 / 900 = 16 seconds

The minimum interval is always 10 seconds (enforced by ChronoSnap).

## Requirements

- [ChronoSnap](https://github.com/kernelkaribou/chronosnap) instance with API key configured
- Camera accessible via RTSP, HTTP, or local device from the ChronoSnap server
- Home Assistant 2024.1+

## License

MIT
