"""Config flow for Netro Watering integration."""

from __future__ import annotations

import logging
from typing import Any

from pynetro import NetroClient, NetroConfig, NetroException, NetroInvalidKey
from pynetro.client import mask
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult, section
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_API_KEY,               # NEW: replaces CONF_SERIAL_NUMBER as the auth credential
    CONF_CTRL_REFRESH_INTERVAL,
    CONF_DEFAULT_WATERING_DELAY,
    CONF_DELAY_BEFORE_REFRESH,
    CONF_DEVICE_HW_VERSION,
    CONF_DEVICE_NAME,
    CONF_DEVICE_SW_VERSION,
    CONF_DEVICE_TYPE,
    CONF_DURATION,
    CONF_MONTHS_AFTER_SCHEDULES,
    CONF_MONTHS_BEFORE_SCHEDULES,
    CONF_SENS_REFRESH_INTERVAL,
    CONF_SENSOR_VALUE_DAYS_BEFORE_TODAY,
    CONF_SERIAL_NUMBER,         # KEPT: read from API response, used as device unique_id
    CONTROLLER_ADVANCED_OPTIONS_COLLAPSED,
    CONTROLLER_DEVICE_TYPE,
    CTRL_REFRESH_INTERVAL_MN,
    DEFAULT_SENSOR_VALUE_DAYS_BEFORE_TODAY,
    DEFAULT_WATERING_DELAY,
    DEFAULT_WATERING_DURATION,
    DELAY_BEFORE_REFRESH,
    DOMAIN,
    GLOBAL_PARAMETERS,
    MAX_DELAY_BEFORE_REFRESH,
    MAX_MONTHS_AFTER_SCHEDULES,
    MAX_MONTHS_BEFORE_SCHEDULES,
    MAX_REFRESH_INTERVAL_MN,
    MAX_SENSOR_VALUE_DAYS_BEFORE_TODAY,
    MAX_WATERING_DELAY,
    MAX_WATERING_DURATION,
    MIN_DELAY_BEFORE_REFRESH,
    MIN_MONTHS_AFTER_SCHEDULES,
    MIN_MONTHS_BEFORE_SCHEDULES,
    MIN_REFRESH_INTERVAL_MN,
    MIN_SENSOR_VALUE_DAYS_BEFORE_TODAY,
    MIN_WATERING_DELAY,
    MIN_WATERING_DURATION,
    MONTHS_AFTER_SCHEDULES,
    MONTHS_BEFORE_SCHEDULES,
    SENS_REFRESH_INTERVAL_MN,
    SENSOR_ADVANCED_OPTIONS_COLLAPSED,
    SENSOR_DEVICE_TYPE,
)
from .http_client import AiohttpClient

_LOGGER = logging.getLogger(__name__)

# mypy: disable-error-code="return"

# V2: the user enters the 32-char encrypted API key generated at
# netrohome.com → Account → API Key.  We render it as a password field
# so the key is masked in the UI.
DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)


class PlaceholderHub:
    """Probe the Netro Public API v2 with the supplied API key.

    V2: authenticates with the 32-char encrypted API key rather than the
    device serial number.  The serial number is read back from the API
    response and stored separately so it can be used as the stable HA
    device identifier independently of any future key rotation.
    """

    def __init__(self, api_key: str) -> None:
        """Initialize with the V2 API key."""
        self.api_key = api_key
        self.info: dict[str, Any] | None = None

    async def check(self, hass: HomeAssistant) -> bool:
        """Verify the API key and retrieve device info from the Netro API."""
        session = async_get_clientsession(hass)
        client = NetroClient(http=AiohttpClient(session), config=NetroConfig())
        # V2: api_key is the auth credential; serial is returned inside the response
        self.info = await client.get_info(self.api_key)
        return self.info is not None

    def is_a_controller(self) -> bool:
        """Return True if the API key belongs to a controller device."""
        return self.info["data"].get("device") is not None

    def is_a_sensor(self) -> bool:
        """Return True if the API key belongs to a soil sensor."""
        return self.info["data"].get("sensor") is not None

    def get_device_type(self) -> str | None:
        """Return the device type string: CONTROLLER_DEVICE_TYPE or SENSOR_DEVICE_TYPE."""
        if self.is_a_sensor():
            return SENSOR_DEVICE_TYPE
        if self.is_a_controller():
            return CONTROLLER_DEVICE_TYPE
        return None

    def get_serial(self) -> str:
        """Return the device serial number as reported by the API.

        In V2 the serial is embedded in the info response body rather than
        being derived from the auth credential.
        """
        if self.is_a_sensor():
            return self.info["data"]["sensor"]["serial"]
        return self.info["data"]["device"]["serial"]

    def get_name(self) -> str:
        """Return the device name."""
        if self.is_a_sensor():
            return self.info["data"]["sensor"]["name"]
        return self.info["data"]["device"]["name"]

    def get_hw_version(self) -> str:
        """Return the hardware version of the device."""
        if self.is_a_sensor():
            return self.info["data"]["sensor"]["version"]
        return self.info["data"]["device"]["version"]

    def get_sw_version(self) -> str:
        """Return the firmware version of the device."""
        if self.is_a_sensor():
            return self.info["data"]["sensor"]["sw_version"]
        return self.info["data"]["device"]["sw_version"]


def _normalize_api_key(value: str) -> str:
    """Strip surrounding whitespace from a V2 API key.

    V2 keys are base64url-encoded and CASE-SENSITIVE — do not uppercase them.
    """
    return str(value).strip()


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user-supplied API key and return the device metadata.

    1. Probe the Netro Public API v2 with the supplied key.
    2. Determine the device type (controller or sensor).
    3. Return a dict that stores BOTH the api_key (for all future API calls)
       and the serial_number (read from the response, used as the stable HA
       device identifier so the device survives key rotation).
    """
    api_key = _normalize_api_key(data[CONF_API_KEY])
    hub = PlaceholderHub(api_key)
    ok = await hub.check(hass)
    if not ok:
        raise CannotConnect

    return {
        CONF_API_KEY: api_key,                  # persisted for all future API calls
        CONF_SERIAL_NUMBER: hub.get_serial(),   # from the response; used as unique_id
        CONF_DEVICE_TYPE: hub.get_device_type(),
        CONF_DEVICE_NAME: hub.get_name(),
        CONF_DEVICE_HW_VERSION: hub.get_hw_version(),
        CONF_DEVICE_SW_VERSION: hub.get_sw_version(),
    }


class NetroConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Netro Watering."""

    VERSION = 1

    def is_matching(self, other_flow: dict[str, Any]) -> bool:
        """This integration does not support automatic discovery."""
        return False

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OptionsFlowHandler:
        """Get the options flow for this handler."""
        return OptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key = _normalize_api_key(user_input[CONF_API_KEY])

            # Fast-path: abort without hitting the network if this exact key
            # is already registered.
            for entry in self._async_current_entries():
                if _normalize_api_key(entry.data.get(CONF_API_KEY, "")) == api_key:
                    return self.async_abort(reason="already_configured")

            try:
                config_item = await validate_input(self.hass, user_input)
            except NetroInvalidKey:
                _LOGGER.warning("Invalid API key supplied: %s", mask(api_key))
                errors["base"] = "invalid_api_key"
            except NetroException:
                _LOGGER.exception(
                    "Unexpected Netro API exception for key: %s", mask(api_key)
                )
                errors["base"] = "netro_error_occurred"
            except CannotConnect:
                _LOGGER.exception("Cannot connect for key: %s", mask(api_key))
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception for key: %s", mask(api_key))
                errors["base"] = "unknown"
            else:
                # Secondary check: abort if the physical device (by serial) is
                # already registered, e.g. added previously with a different key.
                serial = config_item[CONF_SERIAL_NUMBER]
                for entry in self._async_current_entries():
                    if entry.data.get(CONF_SERIAL_NUMBER, "") == serial:
                        return self.async_abort(reason="already_configured")

                return self.async_create_entry(
                    title=config_item[CONF_DEVICE_NAME], data=config_item
                )

        return self.async_show_form(
            step_id="user", data_schema=DEVICE_SCHEMA, errors=errors
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate a connection failure."""


class OptionsFlowHandler(config_entries.OptionsFlowWithReload):
    """Netro Watering options flow with automatic reload."""

    def _gp(self) -> dict[str, Any]:
        """Retrieve already loaded YAML parameters (fallback for defaults)."""
        return self.hass.data.get(DOMAIN, {}).get(GLOBAL_PARAMETERS, {})

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step of the options flow."""
        if user_input is not None:
            advanced = user_input.pop("advanced", {})
            if isinstance(advanced, dict):
                user_input.update(advanced)
            new_options = {**self.config_entry.options, **user_input}
            return self.async_create_entry(title="", data=new_options)

        gp = self._gp()
        opt = self.config_entry.options

        if self.config_entry.data[CONF_DEVICE_TYPE] == CONTROLLER_DEVICE_TYPE:
            advanced_schema = vol.Schema(
                {
                    vol.Optional(
                        CONF_DELAY_BEFORE_REFRESH,
                        default=opt.get(
                            CONF_DELAY_BEFORE_REFRESH,
                            gp.get(CONF_DELAY_BEFORE_REFRESH, DELAY_BEFORE_REFRESH),
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_DELAY_BEFORE_REFRESH,
                            max=MAX_DELAY_BEFORE_REFRESH,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_DEFAULT_WATERING_DELAY,
                        default=opt.get(
                            CONF_DEFAULT_WATERING_DELAY,
                            gp.get(CONF_DEFAULT_WATERING_DELAY, DEFAULT_WATERING_DELAY),
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_WATERING_DELAY,
                            max=MAX_WATERING_DELAY,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_MONTHS_BEFORE_SCHEDULES,
                        default=self.config_entry.options.get(
                            CONF_MONTHS_BEFORE_SCHEDULES, MONTHS_BEFORE_SCHEDULES
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_MONTHS_BEFORE_SCHEDULES,
                            max=MAX_MONTHS_BEFORE_SCHEDULES,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_MONTHS_AFTER_SCHEDULES,
                        default=self.config_entry.options.get(
                            CONF_MONTHS_AFTER_SCHEDULES, MONTHS_AFTER_SCHEDULES
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_MONTHS_AFTER_SCHEDULES,
                            max=MAX_MONTHS_AFTER_SCHEDULES,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                }
            )
            schema = vol.Schema(
                {
                    vol.Optional(
                        CONF_CTRL_REFRESH_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_CTRL_REFRESH_INTERVAL, CTRL_REFRESH_INTERVAL_MN
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_REFRESH_INTERVAL_MN,
                            max=MAX_REFRESH_INTERVAL_MN,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_DURATION,
                        default=self.config_entry.options.get(
                            CONF_DURATION, DEFAULT_WATERING_DURATION
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_WATERING_DURATION,
                            max=MAX_WATERING_DURATION,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required("advanced"): section(
                        advanced_schema,
                        {"collapsed": CONTROLLER_ADVANCED_OPTIONS_COLLAPSED},
                    ),
                }
            )
            return self.async_show_form(step_id="init", data_schema=schema)

        if self.config_entry.data[CONF_DEVICE_TYPE] == SENSOR_DEVICE_TYPE:
            advanced_schema = vol.Schema(
                {
                    vol.Optional(
                        CONF_SENSOR_VALUE_DAYS_BEFORE_TODAY,
                        default=opt.get(
                            CONF_SENSOR_VALUE_DAYS_BEFORE_TODAY,
                            gp.get(
                                CONF_SENSOR_VALUE_DAYS_BEFORE_TODAY,
                                DEFAULT_SENSOR_VALUE_DAYS_BEFORE_TODAY,
                            ),
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_SENSOR_VALUE_DAYS_BEFORE_TODAY,
                            max=MAX_SENSOR_VALUE_DAYS_BEFORE_TODAY,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                }
            )
            schema = vol.Schema(
                {
                    vol.Optional(
                        CONF_SENS_REFRESH_INTERVAL,
                        default=opt.get(
                            CONF_SENS_REFRESH_INTERVAL, SENS_REFRESH_INTERVAL_MN
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_REFRESH_INTERVAL_MN,
                            max=MAX_REFRESH_INTERVAL_MN,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required("advanced"): section(
                        advanced_schema,
                        {"collapsed": SENSOR_ADVANCED_OPTIONS_COLLAPSED},
                    ),
                }
            )
            return self.async_show_form(step_id="init", data_schema=schema)

        return self.async_abort(reason="unknown_device_type")
