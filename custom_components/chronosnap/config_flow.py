"""Config flow and options flow for ChronoSnap integration."""

from __future__ import annotations

import logging
import re
import uuid
from copy import deepcopy
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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
from homeassistant.helpers.storage import Store
from yarl import URL

from .api import (
    ChronoSnapAuthError,
    ChronoSnapClient,
    ChronoSnapConnectionError,
    ChronoSnapError,
)
from .const import (
    CONF_ACTIVE_STATE,
    CONF_API_KEY,
    CONF_AUTO_CLEANUP,
    CONF_CAPTURE_QUALITY,
    CONF_DEBOUNCE_SECONDS,
    CONF_DURATION_ENTITY,
    CONF_ENABLED,
    CONF_EXCLUDE_STATES,
    CONF_FPS,
    CONF_INSTANCE_NAME,
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
    DEFAULT_STREAM_TYPE,
    DEFAULT_TARGET_DURATION,
    DOMAIN,
    INTERVAL_MODE_FIXED,
    INTERVAL_MODE_TARGET,
    QUALITY_HIGH,
    QUALITY_LOW,
    QUALITY_MAXIMUM,
    QUALITY_MEDIUM,
    STORAGE_KEY,
    STORAGE_VERSION,
    STREAM_TYPE_DEVICE,
    STREAM_TYPE_HTTP,
    STREAM_TYPE_RTSP,
)

_LOGGER = logging.getLogger(__name__)


def _connection_schema(
    data: dict[str, Any], *, include_name: bool = False
) -> vol.Schema:
    fields = {}
    if include_name:
        fields[
            vol.Optional(
                CONF_INSTANCE_NAME, default=data.get(CONF_INSTANCE_NAME, "ChronoSnap")
            )
        ] = TextSelector(TextSelectorConfig())
    fields[vol.Required(CONF_URL, default=data.get(CONF_URL, "http://"))] = (
        TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
    )
    fields[vol.Required(CONF_API_KEY)] = TextSelector(
        TextSelectorConfig(type=TextSelectorType.PASSWORD)
    )
    return vol.Schema(fields)


async def _async_check_connection(
    hass: HomeAssistant, data: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    values = dict(data)
    errors = {}
    try:
        url = URL(values[CONF_URL].strip().rstrip("/"))
        if (
            url.scheme not in ("http", "https")
            or not url.host
            or url.user
            or url.query_string
            or url.fragment
        ):
            raise ValueError("Invalid server URL")
        values[CONF_URL] = str(url)
        values[CONF_API_KEY] = values[CONF_API_KEY].strip()
        if not values[CONF_API_KEY]:
            errors[CONF_API_KEY] = "required"
            return values, errors
        client = ChronoSnapClient(
            values[CONF_URL],
            values[CONF_API_KEY],
            session=async_get_clientsession(hass),
        )
        await client.test_connection()
    except ValueError:
        errors[CONF_URL] = "invalid_server_url"
    except ChronoSnapAuthError:
        errors["base"] = "invalid_auth"
    except ChronoSnapConnectionError:
        errors["base"] = "cannot_connect"
    except ChronoSnapError as err:
        _LOGGER.warning("ChronoSnap connection validation failed: %s", err)
        errors["base"] = "cannot_connect"
    return values, errors


async def _async_has_recordings(
    hass: HomeAssistant, entry: config_entries.ConfigEntry
) -> bool:
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime:
        return runtime["coordinator"].has_pending_work
    data = await Store(
        hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry.entry_id}"
    ).async_load()
    if data is not None:
        return bool(
            data.get("recordings") or any(data.get("failed_recordings", {}).values())
        )
    legacy = await Store(hass, STORAGE_VERSION, STORAGE_KEY).async_load() or {}
    return bool(legacy.get(entry.entry_id))


async def _async_update_connection(
    hass: HomeAssistant, entry: config_entries.ConfigEntry, values: dict[str, Any]
) -> dict[str, str]:
    if values[CONF_URL] != entry.data[CONF_URL]:
        if await _async_has_recordings(hass, entry):
            return {"base": "recordings_pending"}
        runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if runtime:
            runtime["coordinator"].teardown_listeners()
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, **values}, unique_id=values[CONF_URL]
    )
    if not await hass.config_entries.async_reload(entry.entry_id):
        return {"base": "reload_failed"}
    return {}


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
            values, errors = await _async_check_connection(self.hass, user_input)
            instance_name = (
                user_input.get(CONF_INSTANCE_NAME, "ChronoSnap").strip() or "ChronoSnap"
            )

            if not errors:
                url = values[CONF_URL]
                await self.async_set_unique_id(url)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=instance_name,
                    data={
                        CONF_URL: url,
                        CONF_API_KEY: values[CONF_API_KEY],
                        CONF_INSTANCE_NAME: instance_name,
                    },
                    options={CONF_PROFILES: {}},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_connection_schema(user_input or {}, include_name=True),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="entry_missing")
        errors = {}
        if user_input is not None:
            values, errors = await _async_check_connection(
                self.hass, {**entry.data, CONF_API_KEY: user_input[CONF_API_KEY]}
            )
            if not errors:
                self.hass.config_entries.async_update_entry(entry, data=values)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="entry_missing")
        errors = {}
        if user_input is not None:
            values, errors = await _async_check_connection(self.hass, user_input)
            if (
                not errors
                and values[CONF_URL] != entry.data[CONF_URL]
                and await _async_has_recordings(self.hass, entry)
            ):
                errors["base"] = "recordings_pending"
            if not errors:
                await self.async_set_unique_id(values[CONF_URL])
                if any(
                    other.entry_id != entry.entry_id
                    and other.data[CONF_URL] == values[CONF_URL]
                    for other in self.hass.config_entries.async_entries(DOMAIN)
                ):
                    return self.async_abort(reason="already_configured")
                errors = await _async_update_connection(self.hass, entry, values)
                if not errors:
                    return self.async_abort(reason="reconfigure_successful")
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(user_input or entry.data),
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
        self._entry = config_entry
        self._profiles: dict[str, dict[str, Any]] = deepcopy(
            config_entry.options.get(CONF_PROFILES, {})
        )
        self._original_profiles = deepcopy(self._profiles)
        self._editing_profile_id: str | None = None
        self._available_tags: list[dict[str, Any]] = []
        self._confirmed_enabled: set[str] = set()

    def _save_and_finish(self) -> config_entries.ConfigFlowResult:
        """Save profiles and close the options flow."""
        for pid, profile in self._profiles.items():
            original = self._original_profiles.get(pid, {})
            live = self._entry.options.get(CONF_PROFILES, {}).get(pid, {})
            if pid in self._confirmed_enabled:
                continue
            if (
                pid == self._editing_profile_id
                and pid in self._original_profiles
                and live.get(CONF_ENABLED, True) != original.get(CONF_ENABLED, True)
                and profile.get(CONF_ENABLED, True) != live.get(CONF_ENABLED, True)
            ):
                return self._enabled_conflict_form()
            if profile.get(CONF_ENABLED, True) == original.get(CONF_ENABLED, True):
                profile[CONF_ENABLED] = live.get(
                    CONF_ENABLED, profile.get(CONF_ENABLED, True)
                )
        return self.async_create_entry(
            data={**self._entry.options, CONF_PROFILES: self._profiles}
        )

    def _enabled_conflict_form(self) -> config_entries.ConfigFlowResult:
        profile = self._entry.options.get(CONF_PROFILES, {}).get(
            self._editing_profile_id, {}
        )
        return self.async_show_form(
            step_id="profile_enabled_conflict",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ENABLED, default=profile.get(CONF_ENABLED, True)
                    ): bool
                }
            ),
        )

    async def async_step_profile_enabled_conflict(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Resolve a live switch change explicitly without discarding other edits."""
        if user_input is None:
            return self._enabled_conflict_form()
        profile_id = self._editing_profile_id
        assert profile_id is not None
        self._profiles[profile_id][CONF_ENABLED] = user_input[CONF_ENABLED]
        self._confirmed_enabled.add(profile_id)
        return self._save_and_finish()

    # ── Main menu ───────────────────────────────────────────

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show profile management menu."""
        if user_input is not None:
            action = user_input.get("action")
            if action == "add":
                self._editing_profile_id = None
                return await self.async_step_profile_basic()
            if action == "connection":
                return await self.async_step_connection()
            # action is a profile ID to edit/delete
            self._editing_profile_id = action
            return await self.async_step_profile_action()

        # Build menu: list existing profiles + add option
        menu_options = [{"value": "add", "label": "Add new profile"}]
        live = self._entry.options.get(CONF_PROFILES, {})
        for pid, profile in self._profiles.items():
            name = profile.get(CONF_PROFILE_NAME, pid)
            enabled = live.get(pid, profile).get(CONF_ENABLED, True)
            menu_options.append(
                {
                    "value": pid,
                    "label": f"{name} ({'enabled' if enabled else 'disabled'})",
                }
            )
        menu_options.append(
            {"value": "connection", "label": "Update server connection"}
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
                return await self.async_step_profile_delete()
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

    async def async_step_profile_delete(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            if not user_input["confirm"]:
                return await self.async_step_profile_action()
            runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
            if runtime is None:
                errors["base"] = "integration_not_loaded"
            else:
                try:
                    await runtime["coordinator"].async_remove_profile(
                        self._editing_profile_id
                    )
                except HomeAssistantError:
                    errors["base"] = "cannot_stop"
                else:
                    self._profiles.pop(self._editing_profile_id)
                    self._editing_profile_id = None
                    return self._save_and_finish()
        return self.async_show_form(
            step_id="profile_delete",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            errors=errors,
        )

    async def async_step_connection(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            values, errors = await _async_check_connection(self.hass, user_input)
            if (
                not errors
                and values[CONF_URL] != self._entry.data[CONF_URL]
                and await _async_has_recordings(self.hass, self._entry)
            ):
                errors["base"] = "recordings_pending"
            if not errors:
                if any(
                    entry.entry_id != self._entry.entry_id
                    and entry.data[CONF_URL] == values[CONF_URL]
                    for entry in self.hass.config_entries.async_entries(DOMAIN)
                ):
                    errors["base"] = "already_configured"
                else:
                    errors = await _async_update_connection(
                        self.hass, self._entry, values
                    )
                    if not errors:
                        return self.async_create_entry(data=dict(self._entry.options))
        return self.async_show_form(
            step_id="connection",
            data_schema=_connection_schema(user_input or self._entry.data),
            errors=errors,
        )

    # ── Profile setup step 1: basic info + trigger ──────────

    async def async_step_profile_basic(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Configure profile name, stream, and trigger entity."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate stream URL format
            user_input = dict(user_input)
            for key in (CONF_PROFILE_NAME, CONF_STREAM_URL, CONF_ACTIVE_STATE):
                user_input[key] = user_input.get(key, "").strip()
                if not user_input[key]:
                    errors[key] = "required"
            name = user_input[CONF_PROFILE_NAME]
            if name and (len(name) > 80 or not name.strip(' .\\/:*?"<>|')):
                errors[CONF_PROFILE_NAME] = "invalid_name"
            active = user_input[CONF_ACTIVE_STATE].lower()
            if active in {
                state.strip().lower()
                for state in user_input.get(CONF_EXCLUDE_STATES, "").split(",")
            }:
                errors[CONF_EXCLUDE_STATES] = "active_excluded"
            stream_url = user_input[CONF_STREAM_URL]
            stream_type = user_input.get(CONF_STREAM_TYPE, DEFAULT_STREAM_TYPE)
            try:
                parsed = URL(stream_url)
                valid_stream = (
                    (
                        stream_type == STREAM_TYPE_RTSP
                        and parsed.scheme in ("rtsp", "rtsps")
                        and bool(parsed.host)
                    )
                    or (
                        stream_type == STREAM_TYPE_HTTP
                        and parsed.scheme in ("http", "https")
                        and bool(parsed.host)
                    )
                    or (
                        stream_type == STREAM_TYPE_DEVICE
                        and re.fullmatch(r"/dev/video\d+", stream_url)
                    )
                )
            except ValueError:
                valid_stream = False
            if not valid_stream or len(stream_url) > 2048:
                errors[CONF_STREAM_URL] = "invalid_stream_url"

            if not errors:
                # Store basic info and continue to capture settings
                if self._editing_profile_id is None:
                    self._editing_profile_id = uuid.uuid4().hex
                self._profiles.setdefault(self._editing_profile_id, {})
                self._profiles[self._editing_profile_id].update(user_input)
                return await self.async_step_profile_capture()

        # Pre-fill with existing values if editing
        existing = {}
        if self._editing_profile_id and self._editing_profile_id in self._profiles:
            existing = self._profiles[self._editing_profile_id]
        existing = {**existing, **(user_input or {})}

        return self.async_show_form(
            step_id="profile_basic",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ENABLED, default=existing.get(CONF_ENABLED, True)
                    ): bool,
                    vol.Required(
                        CONF_PROFILE_NAME,
                        default=existing.get(CONF_PROFILE_NAME, ""),
                    ): TextSelector(TextSelectorConfig()),
                    vol.Required(
                        CONF_STREAM_URL,
                        default=existing.get(CONF_STREAM_URL, ""),
                    ): TextSelector(TextSelectorConfig()),
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
                        default=existing.get(CONF_START_DELAY, DEFAULT_START_DELAY),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            max=600,
                            step=1,
                            mode=NumberSelectorMode.BOX,
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
        errors = {}
        if user_input is not None:
            user_input = dict(user_input)
            if not re.fullmatch(
                r"[1-9]\d*x[1-9]\d*", user_input.get(CONF_RESOLUTION, "")
            ):
                errors[CONF_RESOLUTION] = "invalid_resolution"
            # Convert tag_ids from list of strings to list of ints
            raw_tags = user_input.get(CONF_TAG_IDS, [])
            user_input[CONF_TAG_IDS] = [int(t) for t in raw_tags]
            if not errors:
                self._profiles[self._editing_profile_id].update(user_input)
            interval_mode = user_input.get(CONF_INTERVAL_MODE, INTERVAL_MODE_FIXED)

            if not errors and interval_mode == INTERVAL_MODE_TARGET:
                return await self.async_step_profile_target()

            if not errors:
                return self._save_and_finish()

        existing = {
            **self._profiles.get(self._editing_profile_id, {}),
            **(user_input or {}),
        }

        # Fetch available tags from ChronoSnap
        tag_options = []
        tag_warning = ""
        try:
            client = ChronoSnapClient(
                self._entry.data[CONF_URL],
                self._entry.data[CONF_API_KEY],
                session=async_get_clientsession(self.hass),
            )
            self._available_tags = await client.get_tags()
            tag_options = [
                {"value": str(t["id"]), "label": t["name"]}
                for t in self._available_tags
            ]
        except ChronoSnapError as err:
            _LOGGER.warning("Could not fetch tags from ChronoSnap: %s", err)
            tag_warning = "Tags could not be loaded from ChronoSnap. Existing selections are retained; check your connection and API key."
        known_ids = {tag["value"] for tag in tag_options}
        for tag_id in existing.get(CONF_TAG_IDS, []):
            if str(tag_id) not in known_ids:
                tag_options.append(
                    {"value": str(tag_id), "label": f"Unavailable tag ({tag_id})"}
                )

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
                default=existing.get(CONF_INTERVAL_SECONDS, DEFAULT_INTERVAL_SECONDS),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=10,
                    max=3600,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="seconds",
                )
            ),
            vol.Required(
                CONF_FPS,
                default=existing.get(CONF_FPS, DEFAULT_FPS),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=60,
                    step=1,
                    mode=NumberSelectorMode.BOX,
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
                default=existing.get(CONF_CAPTURE_QUALITY, DEFAULT_CAPTURE_QUALITY),
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
                default=existing.get(CONF_AUTO_CLEANUP, DEFAULT_AUTO_CLEANUP),
            ): bool,
            vol.Required(
                CONF_PRESERVE_FAILED_FRAMES,
                default=existing.get(
                    CONF_PRESERVE_FAILED_FRAMES, DEFAULT_PRESERVE_FAILED_FRAMES
                ),
            ): bool,
        }

        # Only show tag selector if tags exist in ChronoSnap
        if tag_options:
            existing_tags = [str(t) for t in existing.get(CONF_TAG_IDS, [])]
            schema_fields[
                vol.Optional(
                    CONF_TAG_IDS,
                    default=existing_tags,
                )
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=tag_options,
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )

        return self.async_show_form(
            step_id="profile_capture",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
            description_placeholders={"tag_warning": tag_warning},
        )

    # ── Profile setup step 3 (optional): target duration settings ─

    async def async_step_profile_target(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Configure target duration mode — entity to read total time from."""
        if user_input is not None:
            self._profiles[self._editing_profile_id].update(user_input)
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
                            min=5,
                            max=300,
                            step=1,
                            mode=NumberSelectorMode.BOX,
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
