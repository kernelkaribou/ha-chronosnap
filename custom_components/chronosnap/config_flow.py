"""Config flow and options flow for ChronoSnap integration."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_URL
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import ChronoSnapAuthError, ChronoSnapClient, ChronoSnapConnectionError, ChronoSnapError
from .const import (
    CONF_ACTIVE_STATE,
    CONF_API_KEY,
    CONF_AUTO_CLEANUP,
    CONF_CAPTURE_QUALITY,
    CONF_DEBOUNCE_SECONDS,
    CONF_DURATION_ENTITY,
    CONF_EXCLUDE_STATES,
    CONF_FPS,
    CONF_INSTANCE_NAME,
    CONF_INTERVAL_MODE,
    CONF_INTERVAL_SECONDS,
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
    DEFAULT_QUALITY,
    DEFAULT_RESOLUTION,
    DEFAULT_START_DELAY,
    DEFAULT_STREAM_TYPE,
    DEFAULT_TARGET_DURATION,
    DOMAIN,
    INTERVAL_MODE_FIXED,
    INTERVAL_MODE_TARGET,
    QUALITY_HIGH,
    QUALITY_LOW,
    QUALITY_MAXIMUM,
    QUALITY_MEDIUM,
    STREAM_TYPE_DEVICE,
    STREAM_TYPE_HTTP,
    STREAM_TYPE_RTSP,
)

_LOGGER = logging.getLogger(__name__)


# ── Config Flow (initial connection setup) ──────────────────────


class ChronoSnapConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle initial setup: ChronoSnap server URL + API key."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial user step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            api_key = user_input[CONF_API_KEY]
            instance_name = user_input.get(
                CONF_INSTANCE_NAME, "ChronoSnap"
            ).strip() or "ChronoSnap"

            client = ChronoSnapClient(url, api_key)
            try:
                await client.test_connection()
            except ChronoSnapAuthError:
                errors["base"] = "invalid_auth"
            except ChronoSnapConnectionError:
                errors["base"] = "cannot_connect"
            except ChronoSnapError as err:
                _LOGGER.error("ChronoSnap API error during setup: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during setup")
                errors["base"] = "unknown"
            finally:
                await client.close()

            if not errors:
                await self.async_set_unique_id(url)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=instance_name,
                    data={
                        CONF_URL: url,
                        CONF_API_KEY: api_key,
                        CONF_INSTANCE_NAME: instance_name,
                    },
                    options={CONF_PROFILES: {}},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_INSTANCE_NAME, default="ChronoSnap"
                    ): TextSelector(TextSelectorConfig()),
                    vol.Required(CONF_URL, default="http://"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.URL)
                    ),
                    vol.Required(CONF_API_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> ChronoSnapOptionsFlow:
        """Get the options flow handler."""
        return ChronoSnapOptionsFlow(config_entry)


# ── Options Flow (profile management) ───────────────────────────


class ChronoSnapOptionsFlow(config_entries.OptionsFlow):
    """Handle timelapse profile management."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._profiles: dict[str, dict[str, Any]] = dict(
            config_entry.options.get(CONF_PROFILES, {})
        )
        self._editing_profile_id: str | None = None
        self._available_tags: list[dict[str, Any]] = []

    def _save_and_finish(self) -> config_entries.ConfigFlowResult:
        """Save profiles and close the options flow."""
        return self.async_create_entry(
            data={CONF_PROFILES: self._profiles}
        )

    # ── Main menu ───────────────────────────────────────────

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show profile management menu."""
        if user_input is not None:
            action = user_input.get("action")
            if action == "add":
                return await self.async_step_profile_basic()
            # action is a profile ID to edit/delete
            self._editing_profile_id = action
            return await self.async_step_profile_action()

        # Build menu: list existing profiles + add option
        menu_options = [{"value": "add", "label": "Add new profile"}]
        for pid, profile in self._profiles.items():
            name = profile.get(CONF_PROFILE_NAME, pid)
            menu_options.append(
                {"value": pid, "label": name}
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("action"): SelectSelector(
                        SelectSelectorConfig(
                            options=menu_options,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    # ── Edit/Delete action for existing profile ─────────────

    async def async_step_profile_action(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Choose to edit or delete an existing profile."""
        if user_input is not None:
            action = user_input.get("action")
            if action == "delete":
                self._profiles.pop(self._editing_profile_id, None)
                self._editing_profile_id = None
                return self._save_and_finish()
            if action == "edit":
                return await self.async_step_profile_basic()
            return await self.async_step_init()

        profile = self._profiles.get(self._editing_profile_id, {})
        name = profile.get(CONF_PROFILE_NAME, "Unknown")

        return self.async_show_form(
            step_id="profile_action",
            data_schema=vol.Schema(
                {
                    vol.Required("action"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": "edit", "label": f"Edit '{name}'"},
                                {"value": "delete", "label": f"Delete '{name}'"},
                                {"value": "back", "label": "Back to menu"},
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    # ── Profile setup step 1: basic info + trigger ──────────

    async def async_step_profile_basic(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Configure profile name, stream, and trigger entity."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate stream URL format
            stream_url = user_input.get(CONF_STREAM_URL, "")
            stream_type = user_input.get(CONF_STREAM_TYPE, DEFAULT_STREAM_TYPE)
            if stream_type == STREAM_TYPE_RTSP and not stream_url.startswith(
                ("rtsp://", "rtsps://")
            ):
                errors[CONF_STREAM_URL] = "invalid_stream_url"
            elif stream_type == STREAM_TYPE_HTTP and not stream_url.startswith(
                ("http://", "https://")
            ):
                errors[CONF_STREAM_URL] = "invalid_stream_url"

            if not errors:
                # Store basic info and continue to capture settings
                if self._editing_profile_id is None:
                    self._editing_profile_id = str(uuid.uuid4())[:8]
                self._profiles.setdefault(self._editing_profile_id, {})
                self._profiles[self._editing_profile_id].update(user_input)
                return await self.async_step_profile_capture()

        # Pre-fill with existing values if editing
        existing = {}
        if self._editing_profile_id and self._editing_profile_id in self._profiles:
            existing = self._profiles[self._editing_profile_id]

        return self.async_show_form(
            step_id="profile_basic",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PROFILE_NAME,
                        default=existing.get(CONF_PROFILE_NAME, ""),
                    ): TextSelector(TextSelectorConfig()),
                    vol.Required(
                        CONF_STREAM_URL,
                        default=existing.get(CONF_STREAM_URL, ""),
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                    vol.Required(
                        CONF_STREAM_TYPE,
                        default=existing.get(CONF_STREAM_TYPE, DEFAULT_STREAM_TYPE),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": STREAM_TYPE_RTSP, "label": "RTSP"},
                                {"value": STREAM_TYPE_HTTP, "label": "HTTP/HTTPS"},
                                {"value": STREAM_TYPE_DEVICE, "label": "Local device"},
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_TRIGGER_ENTITY,
                        default=existing.get(CONF_TRIGGER_ENTITY),
                    ): EntitySelector(EntitySelectorConfig()),
                    vol.Required(
                        CONF_ACTIVE_STATE,
                        default=existing.get(CONF_ACTIVE_STATE, ""),
                    ): TextSelector(TextSelectorConfig()),
                    vol.Optional(
                        CONF_EXCLUDE_STATES,
                        default=existing.get(CONF_EXCLUDE_STATES, ""),
                    ): TextSelector(TextSelectorConfig()),
                    vol.Required(
                        CONF_START_DELAY,
                        default=existing.get(
                            CONF_START_DELAY, DEFAULT_START_DELAY
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0, max=600, step=1, mode=NumberSelectorMode.BOX,
                            unit_of_measurement="seconds",
                        )
                    ),
                    vol.Required(
                        CONF_DEBOUNCE_SECONDS,
                        default=existing.get(
                            CONF_DEBOUNCE_SECONDS, DEFAULT_DEBOUNCE_SECONDS
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0, max=300, step=1, mode=NumberSelectorMode.BOX
                        )
                    ),
                }
            ),
            errors=errors,
        )

    # ── Profile setup step 2: capture & video settings ──────

    async def async_step_profile_capture(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Configure capture interval and video settings."""
        if user_input is not None:
            # Convert tag_ids from list of strings to list of ints
            raw_tags = user_input.get(CONF_TAG_IDS, [])
            if raw_tags:
                user_input[CONF_TAG_IDS] = [int(t) for t in raw_tags]
            self._profiles[self._editing_profile_id].update(user_input)
            interval_mode = user_input.get(CONF_INTERVAL_MODE, INTERVAL_MODE_FIXED)

            if interval_mode == INTERVAL_MODE_TARGET:
                return await self.async_step_profile_target()

            self._editing_profile_id = None
            return self._save_and_finish()

        existing = self._profiles.get(self._editing_profile_id, {})

        # Fetch available tags from ChronoSnap
        tag_options = []
        try:
            url = self.config_entry.data[CONF_URL]
            api_key = self.config_entry.data[CONF_API_KEY]
            client = ChronoSnapClient(url, api_key)
            try:
                self._available_tags = await client.get_tags()
                tag_options = [
                    {"value": str(t["id"]), "label": t["name"]}
                    for t in self._available_tags
                ]
            finally:
                await client.close()
        except Exception:
            _LOGGER.warning("Could not fetch tags from ChronoSnap")

        # Build schema fields
        schema_fields: dict[vol.Marker, Any] = {
            vol.Required(
                CONF_INTERVAL_MODE,
                default=existing.get(CONF_INTERVAL_MODE, INTERVAL_MODE_FIXED),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        {
                            "value": INTERVAL_MODE_FIXED,
                            "label": "Fixed interval",
                        },
                        {
                            "value": INTERVAL_MODE_TARGET,
                            "label": "Target video duration (calculate interval from entity)",
                        },
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_INTERVAL_SECONDS,
                default=existing.get(
                    CONF_INTERVAL_SECONDS, DEFAULT_INTERVAL_SECONDS
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=10, max=3600, step=1, mode=NumberSelectorMode.BOX,
                    unit_of_measurement="seconds",
                )
            ),
            vol.Required(
                CONF_FPS,
                default=existing.get(CONF_FPS, DEFAULT_FPS),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1, max=60, step=1, mode=NumberSelectorMode.BOX,
                    unit_of_measurement="fps",
                )
            ),
            vol.Required(
                CONF_QUALITY,
                default=existing.get(CONF_QUALITY, DEFAULT_QUALITY),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        {"value": QUALITY_LOW, "label": "Low"},
                        {"value": QUALITY_MEDIUM, "label": "Medium"},
                        {"value": QUALITY_HIGH, "label": "High"},
                        {"value": QUALITY_MAXIMUM, "label": "Maximum"},
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_CAPTURE_QUALITY,
                default=existing.get(
                    CONF_CAPTURE_QUALITY, DEFAULT_CAPTURE_QUALITY
                ),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        {"value": QUALITY_LOW, "label": "Low"},
                        {"value": QUALITY_MEDIUM, "label": "Medium"},
                        {"value": QUALITY_HIGH, "label": "High"},
                        {"value": QUALITY_MAXIMUM, "label": "Maximum"},
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_RESOLUTION,
                default=existing.get(CONF_RESOLUTION, DEFAULT_RESOLUTION),
            ): TextSelector(TextSelectorConfig()),
            vol.Required(
                CONF_AUTO_CLEANUP,
                default=existing.get(
                    CONF_AUTO_CLEANUP, DEFAULT_AUTO_CLEANUP
                ),
            ): bool,
        }

        # Only show tag selector if tags exist in ChronoSnap
        if tag_options:
            existing_tags = [str(t) for t in existing.get(CONF_TAG_IDS, [])]
            schema_fields[vol.Optional(
                CONF_TAG_IDS,
                default=existing_tags,
            )] = SelectSelector(
                SelectSelectorConfig(
                    options=tag_options,
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )

        return self.async_show_form(
            step_id="profile_capture",
            data_schema=vol.Schema(schema_fields),
        )

    # ── Profile setup step 3 (optional): target duration settings ─

    async def async_step_profile_target(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Configure target duration mode — entity to read total time from."""
        if user_input is not None:
            self._profiles[self._editing_profile_id].update(user_input)
            self._editing_profile_id = None
            return self._save_and_finish()

        existing = self._profiles.get(self._editing_profile_id, {})

        return self.async_show_form(
            step_id="profile_target",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TARGET_DURATION,
                        default=existing.get(
                            CONF_TARGET_DURATION, DEFAULT_TARGET_DURATION
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=5, max=300, step=1, mode=NumberSelectorMode.BOX,
                            unit_of_measurement="seconds",
                        )
                    ),
                    vol.Required(
                        CONF_DURATION_ENTITY,
                        default=existing.get(CONF_DURATION_ENTITY),
                    ): EntitySelector(EntitySelectorConfig()),
                }
            ),
            description_placeholders={
                "explanation": (
                    "Select an entity whose state represents the total "
                    "duration in seconds (e.g., print_time_remaining). "
                    "The capture interval will be calculated as: "
                    "entity_value / (target_duration × fps), minimum 10s."
                )
            },
        )
