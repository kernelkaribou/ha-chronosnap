"""Constants for the ChronoSnap integration."""

DOMAIN = "chronosnap"

# Config entry keys
CONF_URL = "url"
CONF_API_KEY = "api_key"
CONF_PROFILES = "profiles"

# Profile keys
CONF_PROFILE_NAME = "name"
CONF_STREAM_URL = "stream_url"
CONF_STREAM_TYPE = "stream_type"
CONF_TRIGGER_ENTITY = "trigger_entity"
CONF_ACTIVE_STATE = "active_state"
CONF_INTERVAL_MODE = "interval_mode"
CONF_INTERVAL_SECONDS = "interval_seconds"
CONF_TARGET_DURATION = "target_duration"
CONF_DURATION_ENTITY = "duration_entity"
CONF_FPS = "fps"
CONF_QUALITY = "quality"
CONF_RESOLUTION = "resolution"
CONF_AUTO_CLEANUP = "auto_cleanup"
CONF_DEBOUNCE_SECONDS = "debounce_seconds"
CONF_CAPTURE_QUALITY = "capture_quality"

# Interval modes
INTERVAL_MODE_FIXED = "fixed"
INTERVAL_MODE_TARGET = "target_duration"

# Stream types
STREAM_TYPE_RTSP = "rtsp"
STREAM_TYPE_HTTP = "http"
STREAM_TYPE_DEVICE = "device"

# Profile status values
STATUS_IDLE = "idle"
STATUS_CAPTURING = "capturing"
STATUS_BUILDING = "building_video"
STATUS_ERROR = "error"

# Video quality options
QUALITY_LOW = "low"
QUALITY_MEDIUM = "medium"
QUALITY_HIGH = "high"
QUALITY_MAXIMUM = "maximum"

# Defaults
DEFAULT_FPS = 30
DEFAULT_INTERVAL_SECONDS = 30
DEFAULT_TARGET_DURATION = 30
DEFAULT_QUALITY = QUALITY_HIGH
DEFAULT_RESOLUTION = "1920x1080"
DEFAULT_DEBOUNCE_SECONDS = 10
DEFAULT_AUTO_CLEANUP = True
DEFAULT_CAPTURE_QUALITY = QUALITY_HIGH
DEFAULT_STREAM_TYPE = STREAM_TYPE_RTSP

# Minimum capture interval enforced by ChronoSnap
MIN_INTERVAL_SECONDS = 10

# Video polling
VIDEO_POLL_INTERVAL = 30  # seconds between status checks
VIDEO_POLL_TIMEOUT = 3600  # max seconds to wait for video build (1 hour)

# Storage
STORAGE_KEY = f"{DOMAIN}_active_jobs"
STORAGE_VERSION = 1
