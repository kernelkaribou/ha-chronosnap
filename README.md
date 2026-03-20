# <img src="https://raw.githubusercontent.com/kernelkaribou/ha-chronosnap/main/custom_components/chronosnap/brand/icon.png" width="48" align="top" /> + <img src="https://brands.home-assistant.io/homeassistant/icon.png" width="48" align="top" /> ChronoSnap for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration for [ChronoSnap](https://github.com/kernelkaribou/chronosnap) that turns entity state changes into automated timelapses.

## Examples

- Timelapse every 3D print from start to finish, triggered by your printer's status entity
- Capture what your dog gets up to when the house is detected as empty
- Timelapse your backyard camera as a storm rolls through, triggered by a weather sensor
- Capture a daily sunrise or sunset from an outdoor camera using a sun elevation sensor
- Timelapse your garage workbench whenever a presence sensor detects someone in the shop
- And more ...

## Features

- **UI-based configuration** - no YAML editing required
- **Entity & state selectors** - pick any entity and define trigger states from dropdowns
- **Multiple profiles** - run different timelapses simultaneously on different cameras
- **Flexible capture intervals:**
  - **Fixed** - capture every N seconds (minimum 10s)
  - **Target duration** - automatically calculate the interval from a duration entity so the final video is a specific length
- **Start delay** - configurable grace period before creating a job, cancels if the entity leaves the active state during the delay
- **Stop debounce** - configurable delay before stopping to prevent false triggers from brief state changes
- **Exclude states** - define temporary states that should not interrupt an active capture
- **Tags** - apply ChronoSnap tags to jobs and videos (fetched from your server)
- **Capture and video quality** - independent quality settings for captured frames and output video
- **Resolution control** - set the output video resolution
- **Auto-cleanup** - optionally delete the capture job and raw frames after the video is built (video is always preserved)
- **Per-profile devices** - each profile appears as a device in Home Assistant with status and capture count sensors
- **Restart-safe** - active job IDs are persisted and restored across HA restarts

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** > top-right menu > **Custom repositories**
3. Add `https://github.com/kernelkaribou/ha-chronosnap` as an **Integration**
4. Click **Install**
5. Restart Home Assistant

### Manual

1. Copy `custom_components/chronosnap/` to your HA `config/custom_components/` directory
2. Restart Home Assistant

## Setup

### Connecting to ChronoSnap

1. Go to **Settings** > **Devices & Services** > **Add Integration**
2. Search for **ChronoSnap**
3. Enter your ChronoSnap server URL and API key
4. Click **Submit** to validate the connection

### Adding a Timelapse Profile

1. Go to **Settings** > **Devices & Services** > **ChronoSnap** > &#9881; (configure)
2. Select **Add new profile**
3. Complete the profile setup form (see [Configuration Reference](#configuration-reference) below)
4. Complete the capture and video settings form
5. If using target duration mode, complete the target duration form
6. The profile is saved automatically and begins listening for state changes immediately

### Editing or Deleting a Profile

1. Go to **Settings** > **Devices & Services** > **ChronoSnap** > &#9881; (configure)
2. Select the profile name from the list
3. Choose **Edit** or **Delete**

## Configuration Reference

### Profile Setup (Step 1)

| Field | Description | Default |
|---|---|---|
| **Profile name** | A friendly name for this timelapse profile | Required |
| **Camera stream URL** | The RTSP, HTTP, or device URL for the camera | Required |
| **Stream type** | Protocol type: RTSP, HTTP/HTTPS, or Local device | RTSP |
| **Trigger entity** | The Home Assistant entity to watch for state changes | Required |
| **Active state** | The state value that starts capturing. Capturing stops when the entity leaves this state. | Required |
| **Exclude states** | Comma-separated list of states that should NOT trigger a stop. Useful for temporary intermediate states. | Empty |
| **Start delay** | Seconds to wait after the entity enters the active state before creating the job. If the entity leaves the active state during this window, the job is never created. | 0 |
| **Stop debounce** | Seconds to wait after the entity leaves the active state before stopping the job. Prevents false triggers from brief state fluctuations. | 10 |

### Capture & Video Settings (Step 2)

| Field | Description | Default |
|---|---|---|
| **Interval mode** | **Fixed** uses a static capture interval. **Target duration** calculates the interval from a duration entity to produce a video of a specific length. | Fixed |
| **Capture interval** | Seconds between frame captures (fixed mode only, minimum 10s) | 30 |
| **Video framerate** | Frames per second for the output timelapse video | 30 |
| **Video quality** | Encoding quality for the output video: Low, Medium, High, Maximum | High |
| **Capture image quality** | Image quality for each captured frame: Low, Medium, High, Maximum | High |
| **Video resolution** | Output video resolution as WxH | 1920x1080 |
| **Auto-cleanup** | When enabled, the capture job and its raw frames are deleted after the video is built. The video is always preserved. | On |
| **Tags** | ChronoSnap tags to apply to the job and video. Only shown if tags exist on your ChronoSnap server. | None |

### Target Duration Settings (Step 3, if applicable)

| Field | Description | Default |
|---|---|---|
| **Target video duration** | Desired length of the final timelapse video in seconds | 30 |
| **Duration source entity** | Entity whose state represents either the remaining time in seconds or an estimated finish time (datetime). The integration auto-detects the format. | Required |

The capture interval is calculated as:

```
interval = duration_entity_value / (target_duration x fps)
```

The minimum interval is always 10 seconds (enforced by ChronoSnap). Short-duration processes may produce videos shorter than the target.

## Sensors

Each profile registers as a device and creates two sensors:

| Sensor | Description |
|---|---|
| `sensor.chronosnap_<name>_status` | Current status: `idle`, `capturing`, `building_video`, `error` |
| `sensor.chronosnap_<name>_captures` | Number of frames captured in the current job |

## How It Works

```
Entity enters active state
        |
        v
  Wait start delay (if configured)
        |
        +-- Entity leaves? -> Cancel, no job created
        |
        v
  Calculate interval (fixed or from duration entity)
        |
        v
  Create ChronoSnap job --> Frames captured automatically
        |
        |  Entity leaves active state
        |  (after stop debounce delay)
        v
  Complete the job
        |
        v
  Build timelapse video
        |
        v
  Poll until complete
        |
        v
  Delete job if auto-cleanup is enabled (video preserved)
```

## Troubleshooting

### Profile stuck in error state

If the ChronoSnap API was temporarily unavailable when a stop was triggered, the integration retries up to 3 times. If all retries fail, the profile enters an error state but the job remains tracked. Restarting Home Assistant will re-evaluate the job and attempt to manage it. You can also manually complete or delete the job from the ChronoSnap UI.

### Orphaned jobs on ChronoSnap

If Home Assistant restarts while a job is active, the integration will attempt to restore and manage it on startup. If the API is unreachable at startup, stored job IDs are preserved until connectivity is restored. If a job is orphaned, you can delete it manually from the ChronoSnap UI.

### Target duration mode falling back to fixed interval

If the duration source entity is unavailable, returns an unparseable value, or the remaining time is zero/negative, the integration logs a warning and falls back to the fixed capture interval setting. Check **Settings** > **System** > **Logs** and filter for `chronosnap` to see details.

### Viewing logs

All integration log entries are prefixed with `chronosnap`. To view them, go to **Settings** > **System** > **Logs** and search for `chronosnap`. For more verbose output, add this to your `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.chronosnap: debug
```

## Requirements

- [ChronoSnap](https://github.com/kernelkaribou/chronosnap) instance with API key configured
- Camera accessible via RTSP, HTTP, or local device from the ChronoSnap server
- Home Assistant 2024.1+

## License

MIT
