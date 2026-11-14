"""Small HA boundary doubles; production lifecycle and API code run unchanged."""

from __future__ import annotations

import asyncio
import sys
from copy import deepcopy
from importlib import import_module
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock


def module(name):
    result = ModuleType(name)
    sys.modules[name] = result
    if "." in name:
        parent, child = name.rsplit(".", 1)
        setattr(sys.modules[parent], child, result)
    return result


for name in (
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.event",
    "homeassistant.helpers.storage",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.entity_registry",
    "homeassistant.helpers.entity",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.selector",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.components",
    "homeassistant.components.sensor",
    "homeassistant.components.switch",
    "homeassistant.components.button",
    "homeassistant.components.persistent_notification",
):
    module(name)


class HomeAssistantError(Exception):
    pass


class ConfigEntryAuthFailed(HomeAssistantError):
    pass


class Flow:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    def __init__(self):
        self.context = {}

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def async_abort(self, **kwargs):
        return {"type": "abort", **kwargs}

    async def async_set_unique_id(self, value):
        self.unique_id = value

    def _abort_if_unique_id_configured(self):
        pass


class Entry:
    def __init__(self, profiles=None, entry_id="entry"):
        self.entry_id = entry_id
        self.data = {"url": "http://chronosnap", "api_key": "test-key"}
        self.options = {"profiles": deepcopy(profiles or {})}
        self.disabled_by = None
        self.unload_callbacks = []

    def async_on_unload(self, fn):
        self.unload_callbacks.append(fn)

    def add_update_listener(self, fn):
        return lambda: None


class Store:
    def __init__(self, hass, version, key):
        self.hass, self.key = hass, key

    async def async_load(self):
        return deepcopy(self.hass.storage.get(self.key))

    async def async_save(self, data):
        self.hass.storage[self.key] = deepcopy(data)

    async def async_remove(self):
        self.hass.storage.pop(self.key, None)


class Entity:
    def async_on_remove(self, fn):
        self.__dict__.setdefault("_remove_callbacks", []).append(fn)

    async def async_added_to_hass(self):
        pass

    async def async_will_remove_from_hass(self):
        for fn in self.__dict__.get("_remove_callbacks", []):
            fn()

    def async_write_ha_state(self):
        self._writes = getattr(self, "_writes", 0) + 1


class CoordinatorEntity(Entity):
    def __init__(self, coordinator):
        self.coordinator = coordinator


class DataUpdateCoordinator:
    def __init__(self, *args, update_method, **kwargs):
        self.update_method = update_method
        self.data = None

    async def async_config_entry_first_refresh(self):
        self.data = await self.update_method()


class Selector:
    def __init__(self, config=None):
        self.config = config or {}

    def __call__(self, value):
        return value


class Registry:
    def __init__(self):
        self.devices = {}
        self.entities = []
        self.removed = []

    def async_get_device(self, identifiers):
        return next(
            (d for d in self.devices.values() if d.identifiers & identifiers), None
        )

    def async_get(self, device_id):
        return self.devices.get(device_id)

    def async_get_or_create(self, config_entry_id, **kwargs):
        device = SimpleNamespace(id="server", disabled_by=None, **kwargs)
        self.devices[device.id] = device
        return device

    def async_remove(self, entity_id):
        self.removed.append(entity_id)

    def async_remove_device(self, device_id):
        self.removed.append(device_id)


class Bus:
    def __init__(self):
        self.listeners = []

    def async_listen(self, event, fn):
        item = (event, fn)
        self.listeners.append(item)
        return lambda: self.listeners.remove(item)

    def async_listen_once(self, event, fn):
        return self.async_listen(event, fn)


def track_state(hass, entities, handler):
    return hass.bus.async_listen(tuple(entities), handler)


def track_interval(hass, handler, interval):
    return hass.bus.async_listen(interval, handler)


ce = sys.modules["homeassistant.config_entries"]
ce.ConfigEntry = Entry
ce.ConfigFlow = Flow
ce.OptionsFlow = Flow
ce.ConfigFlowResult = dict
core = sys.modules["homeassistant.core"]
core.CALLBACK_TYPE = object
core.HomeAssistant = object
core.Event = object
core.callback = lambda fn: fn
constants = sys.modules["homeassistant.const"]
constants.Platform = SimpleNamespace(SENSOR="sensor", SWITCH="switch", BUTTON="button")
constants.UnitOfInformation = SimpleNamespace(GIGABYTES="GB")
constants.EntityCategory = SimpleNamespace(DIAGNOSTIC="diagnostic")
constants.EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
constants.CONF_URL = "url"
exceptions = sys.modules["homeassistant.exceptions"]
exceptions.HomeAssistantError = HomeAssistantError
exceptions.ConfigEntryAuthFailed = ConfigEntryAuthFailed
event_helpers = sys.modules["homeassistant.helpers.event"]
event_helpers.async_track_state_change_event = track_state
event_helpers.async_track_time_interval = track_interval
sys.modules["homeassistant.helpers.storage"].Store = Store
dr = sys.modules["homeassistant.helpers.device_registry"]
dr.DeviceInfo = dict
dr.EVENT_DEVICE_REGISTRY_UPDATED = "device_registry_updated"
dr.async_get = lambda hass: hass.devices
dr.async_entries_for_config_entry = lambda reg, entry_id: list(reg.devices.values())
er = sys.modules["homeassistant.helpers.entity_registry"]
er.async_get = lambda hass: hass.entities
er.async_entries_for_config_entry = lambda reg, entry_id: reg.entities
sys.modules["homeassistant.helpers.entity"].Entity = Entity
sys.modules["homeassistant.helpers.entity_platform"].AddEntitiesCallback = object
sys.modules["homeassistant.helpers.aiohttp_client"].async_get_clientsession = (
    lambda hass: hass.session
)
updates = sys.modules["homeassistant.helpers.update_coordinator"]
updates.CoordinatorEntity = CoordinatorEntity
updates.DataUpdateCoordinator = DataUpdateCoordinator
updates.UpdateFailed = type("UpdateFailed", (Exception,), {})
sys.modules["homeassistant.components.sensor"].SensorEntity = type(
    "SensorEntity", (Entity,), {}
)
sys.modules["homeassistant.components.sensor"].SensorStateClass = SimpleNamespace(
    MEASUREMENT="measurement"
)
sys.modules["homeassistant.components.switch"].SwitchEntity = type(
    "SwitchEntity", (Entity,), {}
)
sys.modules["homeassistant.components.button"].ButtonEntity = type(
    "ButtonEntity", (Entity,), {}
)
sys.modules["homeassistant.components.persistent_notification"].async_create = (
    lambda hass, message, **kwargs: hass.notifications.append(
        {"message": message, **kwargs}
    )
)
selectors = sys.modules["homeassistant.helpers.selector"]
for name in ("Entity", "Number", "Select", "Text"):
    setattr(selectors, f"{name}Selector", Selector)
    setattr(selectors, f"{name}SelectorConfig", dict)
selectors.NumberSelectorMode = SimpleNamespace(BOX="box")
selectors.SelectSelectorMode = SimpleNamespace(LIST="list", DROPDOWN="dropdown")
selectors.TextSelectorType = SimpleNamespace(URL="url", PASSWORD="password")

package = module("_chronosnap_test")
package.__path__ = [
    str(Path(__file__).resolve().parents[1] / "custom_components" / "chronosnap")
]
api = import_module("_chronosnap_test.api")
coordinator = import_module("_chronosnap_test.coordinator")
flow = import_module("_chronosnap_test.config_flow")
integration = import_module("_chronosnap_test.__init__")
sensors = import_module("_chronosnap_test.sensor")
switches = import_module("_chronosnap_test.switch")
buttons = import_module("_chronosnap_test.button")
diagnostics = import_module("_chronosnap_test.diagnostics")

PROFILE = {
    "name": "Printer",
    "stream_url": "rtsp://camera/stream",
    "stream_type": "rtsp",
    "trigger_entity": "sensor.printer",
    "active_state": "printing",
    "exclude_states": "paused",
    "debounce_seconds": 0,
    "enabled": True,
}


class Hass:
    def __init__(self, entry):
        self.loop = asyncio.get_running_loop()
        self.storage = {}
        self.devices = Registry()
        self.entities = Registry()
        self.data = {}
        self.notifications = []
        self.bus = Bus()
        self.state_values = {}
        self.states = SimpleNamespace(get=self.state_values.get)
        self.is_running = True
        self.session = object()
        self.config = SimpleNamespace(time_zone="UTC")
        self.config_entries = SimpleNamespace(
            async_update_entry=self.update_entry,
            async_get_entry=lambda _: entry,
            async_entries=lambda _: [entry],
            async_reload=AsyncMock(return_value=True),
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        )

    def update_entry(self, entry, **kwargs):
        for key, value in kwargs.items():
            setattr(entry, key, value)

    def async_create_task(self, operation):
        return asyncio.create_task(operation)

    def set_state(self, value, entity_id="sensor.printer", **attrs):
        self.state_values[entity_id] = SimpleNamespace(state=value, attributes=attrs)


def client():
    return SimpleNamespace(
        create_job=AsyncMock(return_value={"id": 1}),
        complete_job=AsyncMock(return_value={"id": 1, "status": "completed"}),
        create_video=AsyncMock(return_value={"id": 2}),
        get_job=AsyncMock(return_value={"id": 1, "status": "active"}),
        get_video=AsyncMock(return_value={"id": 2, "status": "processing"}),
        get_capture_count=AsyncMock(return_value=12),
        delete_job=AsyncMock(),
        get_tags=AsyncMock(return_value=[]),
        test_connection=AsyncMock(return_value={"version": "test"}),
        get_storage_stats=AsyncMock(return_value={}),
        get_jobs=AsyncMock(return_value=[]),
    )


def make_coordinator(profiles=None):
    entry = Entry({"p": PROFILE} if profiles is None else profiles)
    hass = Hass(entry)
    hass.set_state("printing")
    instance = coordinator.ProfileCoordinator(hass, client(), entry)
    instance._running = instance._ready = True
    hass.data["chronosnap"] = {entry.entry_id: {"coordinator": instance}}
    return instance
