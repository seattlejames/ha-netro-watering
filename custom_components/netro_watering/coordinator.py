"""Support for Netro watering system."""

from __future__ import annotations

import datetime
import logging
from datetime import timedelta
from time import gmtime, strftime

from dateutil.relativedelta import relativedelta
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pynetro import NetroClient, NetroConfig

from .const import (
    DOMAIN,
    MANUFACTURER,
    NETRO_CONTROLLER_BATTERY_LEVEL,
    NETRO_CONTROLLER_STATUS,
    NETRO_CONTROLLER_ZONENUM,
    NETRO_CONTROLLER_ZONES,
    NETRO_DEFAULT_SENSOR_MODEL,
    NETRO_DEFAULT_ZONE_MODEL,
    NETRO_METADATA_LAST_ACTIVE,
    NETRO_METADATA_TID,
    NETRO_METADATA_TIME,
    NETRO_METADATA_TOKEN_LIMIT,
    NETRO_METADATA_TOKEN_REMAINING,
    NETRO_METADATA_TOKEN_RESET,
    NETRO_METADATA_VERSION,
    NETRO_MOISTURE_MOISTURE,
    NETRO_MOISTURE_ZONE,
    NETRO_PIXIE_CONTROLLER_MODEL,
    NETRO_SCHEDULE_END_TIME,
    NETRO_SCHEDULE_EXECUTED,
    NETRO_SCHEDULE_EXECUTING,
    NETRO_SCHEDULE_FIX,
    NETRO_SCHEDULE_MANUAL,
    NETRO_SCHEDULE_SMART,
    NETRO_SCHEDULE_SOURCE,
    NETRO_SCHEDULE_START_TIME,
    NETRO_SCHEDULE_STATUS,
    NETRO_SCHEDULE_VALID,
    NETRO_SCHEDULE_ZONE,
    NETRO_SENSOR_BATTERY_LEVEL,
    NETRO_SENSOR_CELSIUS,
    NETRO_SENSOR_FAHRENHEIT,
    NETRO_SENSOR_ID,
    NETRO_SENSOR_LOCAL_DATE,
    NETRO_SENSOR_LOCAL_TIME,
    NETRO_SENSOR_MOISTURE,
    NETRO_SENSOR_SUNLIGHT,
    NETRO_SENSOR_TIME,
    NETRO_SPRITE_CONTROLLER_MODEL,
    NETRO_STATUS_DISABLE,
    NETRO_STATUS_ENABLE,
    NETRO_STATUS_ONLINE,
    NETRO_STATUS_SETUP,
    NETRO_STATUS_WATERING,
    NETRO_ZONE_ENABLED,
    NETRO_ZONE_ITH,
    NETRO_ZONE_NAME,
    NETRO_ZONE_SMART,
    TZ_OFFSET,
)
from .http_client import AiohttpClient

_LOGGER = logging.getLogger(__name__)

# pylint: disable=attribute-defined-outside-init,consider-using-dict-items,chained-comparison
# mypy: disable-error-code="var-annotated,arg-type"


def prepare_slowdown_factors(slowdown_factor: list) -> list | None:
    """Convert 'from' and 'to' fields of the slowdown factor table into decimal time values."""
    if slowdown_factor is not None:
        def hhmm_to_decimal(hhmm: str) -> float:
            fields = hhmm.split(":")
            hours = fields[0] if len(fields) > 0 else 0.0
            minutes = fields[1] if len(fields) > 1 else 0.0
            seconds = fields[2] if len(fields) > 2 else 0.0
            return float(hours) + float(minutes) / 60.0 + float(seconds) / pow(60.0, 2)

        for slot in slowdown_factor:
            slot["from"] = hhmm_to_decimal(slot["from"])
            slot["to"] = hhmm_to_decimal(slot["to"])
            if slot["from"] > slot["to"]:
                slot["from"] = slot["from"] - 24

    return slowdown_factor


def get_slowdown_factor(slowdown_factors, this_time: datetime.time) -> int:
    """Return the slowdown factor applicable to the given time, or 1 if none matches."""
    selected_factor = 1

    if slowdown_factors is not None and slowdown_factors:
        positive_this_time = (
            this_time.hour + this_time.minute / 60.0 + this_time.second / pow(60.0, 2)
        )
        negative_this_time = positive_this_time - 24

        for slot in slowdown_factors:
            if (
                positive_this_time >= slot["from"] and positive_this_time <= slot["to"]
            ) or (
                negative_this_time >= slot["from"] and negative_this_time <= slot["to"]
            ):
                selected_factor = slot["sdf"]
                break

    return selected_factor


class Meta:
    """Meta data returned by any Netro service call."""

    def __init__(
        self,
        last_active: str,
        time: str,
        tid: str,
        version: str,
        token_limit: int,
        token_remaining: int,
        token_reset: str,
    ) -> None:
        """Create a meta data object."""
        self.version = version
        self.token_limit = token_limit
        self.token_remaining = token_remaining
        self.tid = tid
        self.last_active_date = datetime.datetime.fromisoformat(last_active)
        self.time = datetime.datetime.fromisoformat(time)
        self.token_reset_date = datetime.datetime.fromisoformat(token_reset)


class NetroSensorUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator for Netro sensor (Whisperer) NPA v2 calls."""

    # Sensor measure attributes — pre-declared to prevent AttributeError before first refresh
    id = None
    celsius = None
    moisture = None
    sunlight = None
    fahrenheit = None
    battery_level = None
    time = None
    local_date = None
    local_time = None
    _metadata = None

    def __init__(
        self,
        hass: HomeAssistant,
        refresh_interval: int,
        sensor_value_days_before_today: int,
        api_key: str,           # V2: 32-char encrypted key used to authenticate API calls
        serial_number: str,     # V2: device serial from API response, used as HA unique_id
        device_type: str,
        device_name: str,
        hw_version: str,
        sw_version: str,
    ) -> None:
        """Initialize the sensor coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=device_name,
            update_interval=timedelta(minutes=refresh_interval),
        )
        self.api_key = api_key          # auth credential for all API calls
        self.serial_number = serial_number  # stable device identifier (not the auth key)
        self.device_type = device_type
        self.device_name = device_name
        self.hw_version = hw_version
        self.sw_version = sw_version
        self.sensor_value_days_before_today = sensor_value_days_before_today

    @property
    def device_info(self) -> DeviceInfo:
        """Return information about the sensor device."""
        return DeviceInfo(
            name=f"{self.device_name}",
            identifiers={(DOMAIN, self.serial_number)},  # serial is the stable identifier
            manufacturer=MANUFACTURER,
            hw_version=self.hw_version,
            sw_version=self.sw_version,
            model=NETRO_DEFAULT_SENSOR_MODEL,
        )

    @property
    def metadata(self) -> Meta | None:
        """Return the meta data from the last API response."""
        return self._metadata if self._metadata else None

    @property
    def token_remaining(self) -> int | None:
        """Return the remaining API call tokens for today."""
        return self.metadata.token_remaining if self.metadata is not None else None

    async def _async_update_data(self):
        """Fetch sensor data from the Netro Public API v2."""
        _LOGGER.info(
            "Polling info for %s sensor (repeated every %d minutes)",
            self.name,
            self.update_interval.total_seconds() / 60,
        )

        session = async_get_clientsession(self.hass)
        client = NetroClient(http=AiohttpClient(session), config=NetroConfig())
        # V2: authenticate with api_key, NOT serial_number
        res = await client.get_sensor_data(
            self.api_key,
            start_date=(
                datetime.date.today()
                - timedelta(days=self.sensor_value_days_before_today)
            ).strftime("%Y-%m-%d"),
            end_date=datetime.date.today().strftime("%Y-%m-%d"),
        )

        meta_data = res["meta"]
        self._metadata = Meta(
            meta_data[NETRO_METADATA_LAST_ACTIVE],
            meta_data[NETRO_METADATA_TIME],
            meta_data[NETRO_METADATA_TID],
            meta_data[NETRO_METADATA_VERSION],
            meta_data[NETRO_METADATA_TOKEN_LIMIT],
            meta_data[NETRO_METADATA_TOKEN_REMAINING],
            meta_data[NETRO_METADATA_TOKEN_RESET],
        )

        if len(res["data"]["sensor_data"]) > 0:
            sensor_data = res["data"]["sensor_data"][0]
            self.id = sensor_data[NETRO_SENSOR_ID]
            self.time = datetime.datetime.fromisoformat(
                sensor_data[NETRO_SENSOR_TIME] + TZ_OFFSET
            )
            self.local_date = datetime.date.fromisoformat(
                sensor_data[NETRO_SENSOR_LOCAL_DATE]
            )
            self.local_time = datetime.time.fromisoformat(
                sensor_data[NETRO_SENSOR_LOCAL_TIME]
            )
            self.moisture = sensor_data[NETRO_SENSOR_MOISTURE]
            self.sunlight = sensor_data[NETRO_SENSOR_SUNLIGHT]
            self.celsius = sensor_data[NETRO_SENSOR_CELSIUS]
            self.fahrenheit = sensor_data[NETRO_SENSOR_FAHRENHEIT]
            self.battery_level = sensor_data[NETRO_SENSOR_BATTERY_LEVEL]

    def __str__(self) -> str:
        """String representation for logging."""
        return f'sensor coordinator "{self.name}" ({NETRO_DEFAULT_SENSOR_MODEL})'


class NetroControllerUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator for Netro controller NPA v2 calls."""

    class Zone:
        """One zone (valve) managed by a Netro controller."""

        past_schedules = []
        coming_schedules = []
        moistures = []

        def __init__(
            self,
            controller: "NetroControllerUpdateCoordinator",
            ith: int,
            enabled: bool,
            smart: str,
            name: str,
            serial_number: str,
        ) -> None:
            """Create a zone.

            ``serial_number`` here is the CONTROLLER's serial, used to build
            the zone's virtual identifier (<controller_serial>_<ith>).  It is
            NOT used as an API auth credential.
            """
            self.ith = ith
            self.enabled = enabled
            self.smart = smart
            self.name = name
            self.serial_number = serial_number + "_" + str(ith)  # virtual device id
            self.parent_controller = controller

        async def start_watering(
            self, duration: int, delay: int, start_time: datetime.time
        ) -> None:
            """Start watering this zone for the given duration (minutes)."""
            session = async_get_clientsession(self.parent_controller.hass)
            client = NetroClient(http=AiohttpClient(session), config=NetroConfig())
            # V2: authenticate with the controller's api_key
            await client.water(
                self.parent_controller.api_key,
                duration_minutes=duration,
                zones=[str(self.ith)],
                delay_minutes=delay,
                start_time=(
                    start_time.strftime("%Y-%m-%d %H:%M")
                    if start_time is not None
                    else None
                ),
            )

        async def stop_watering(self) -> None:
            """Stop watering (stops all zones on the controller)."""
            session = async_get_clientsession(self.parent_controller.hass)
            client = NetroClient(http=AiohttpClient(session), config=NetroConfig())
            # V2: authenticate with the controller's api_key
            await client.stop_water(self.parent_controller.api_key)

        @property
        def watering(self) -> bool | None:
            """Return True if this zone is currently watering."""
            if self.last_run:
                return self.last_run[NETRO_SCHEDULE_STATUS] == NETRO_SCHEDULE_EXECUTING
            return False

        @property
        def last_watering_status(self) -> str | None:
            """Return the status of the last/current watering."""
            if self.last_run:
                return self.last_run[NETRO_SCHEDULE_STATUS]
            return None

        @property
        def last_watering_start(self) -> datetime.datetime | None:
            """Return the start datetime of the last/current watering."""
            if self.last_run:
                return datetime.datetime.fromisoformat(
                    self.last_run[NETRO_SCHEDULE_START_TIME] + TZ_OFFSET
                )
            return None

        @property
        def last_watering_end(self) -> datetime.datetime | None:
            """Return the end datetime of the last/current watering."""
            if self.last_run:
                return datetime.datetime.fromisoformat(
                    self.last_run[NETRO_SCHEDULE_END_TIME] + TZ_OFFSET
                )
            return None

        @property
        def last_watering_source(self) -> str | None:
            """Return the source of the last/current watering."""
            if self.last_run:
                return self.last_run[NETRO_SCHEDULE_SOURCE]
            return None

        @property
        def next_watering_status(self) -> str | None:
            """Return the status of the next planned watering."""
            if self.next_run:
                return self.next_run[NETRO_SCHEDULE_STATUS]
            return None

        @property
        def next_watering_start(self) -> datetime.datetime | None:
            """Return the start datetime of the next planned watering."""
            if self.next_run:
                return datetime.datetime.fromisoformat(
                    self.next_run[NETRO_SCHEDULE_START_TIME] + TZ_OFFSET
                )
            return None

        @property
        def next_watering_end(self) -> datetime.datetime | None:
            """Return the end datetime of the next planned watering."""
            if self.next_run:
                return datetime.datetime.fromisoformat(
                    self.next_run[NETRO_SCHEDULE_END_TIME] + TZ_OFFSET
                )
            return None

        @property
        def next_watering_source(self) -> str | None:
            """Return the source of the next planned watering."""
            if self.next_run:
                return self.next_run[NETRO_SCHEDULE_SOURCE]
            return None

        @property
        def last_run(self) -> dict | None:
            """Return the most recent executed/executing schedule."""
            return self.past_schedules[0] if self.past_schedules else None

        @property
        def next_run(self) -> dict | None:
            """Return the next upcoming valid schedule."""
            return self.coming_schedules[0] if self.coming_schedules else None

        @property
        def moisture(self) -> dict | None:
            """Return the most recently reported moisture level."""
            return self.moistures[0][NETRO_MOISTURE_MOISTURE] if self.moistures else None

        @property
        def token_remaining(self) -> int | None:
            """Return the remaining API tokens (delegated to the parent controller)."""
            return (
                self.parent_controller.metadata.token_remaining
                if self.parent_controller.metadata is not None
                else None
            )

        @property
        def device_info(self) -> DeviceInfo:
            """Return HA device info for this zone."""
            return DeviceInfo(
                name=(
                    f"{self.name}"
                    if self.name
                    else f"{self.parent_controller.name} {self.ith}"
                ),
                identifiers={(DOMAIN, self.serial_number)},  # <controller_serial>_<ith>
                manufacturer=MANUFACTURER,
                model=NETRO_DEFAULT_ZONE_MODEL,
                via_device=(DOMAIN, self.parent_controller.serial_number),
            )

    _schedules = []
    _moistures = []

    def __init__(
        self,
        hass: HomeAssistant,
        refresh_interval: int,
        slowdown_factors: list,
        schedules_months_before: int,
        schedules_months_after: int,
        api_key: str,           # V2: 32-char encrypted key used to authenticate API calls
        serial_number: str,     # V2: device serial from API response, used as HA unique_id
        device_type: str,
        device_name: str,
        hw_version: str,
        sw_version: str,
    ) -> None:
        """Initialize the controller coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=device_name,
            update_interval=datetime.timedelta(minutes=refresh_interval),
        )
        self.api_key = api_key          # auth credential for all API calls
        self.serial_number = serial_number  # stable device identifier (not the auth key)
        self.device_type = device_type
        self.device_name = device_name
        self.hw_version = hw_version
        self.sw_version = sw_version
        self.refresh_interval = refresh_interval
        self.slowdown_factors = slowdown_factors
        self.current_slowdown_factor = 1
        self.schedules_months_before = schedules_months_before
        self.schedules_months_after = schedules_months_after
        self._active_zones = {}

    @property
    def device_info(self) -> DeviceInfo:
        """Return HA device info for the controller (identifier only; details set in __init__.py)."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.serial_number)},
        )

    def _update_from_schedules(self, schedules):
        """Distribute schedules across active zones."""
        self._schedules = sorted(
            schedules,
            key=(lambda s: s[NETRO_SCHEDULE_START_TIME]),
            reverse=False,
        )

        for zone_key in self._active_zones:
            past = sorted(
                [
                    s for s in schedules
                    if s[NETRO_SCHEDULE_ZONE] == zone_key
                    and s[NETRO_SCHEDULE_STATUS] in (NETRO_SCHEDULE_EXECUTED, NETRO_SCHEDULE_EXECUTING)
                ],
                key=(lambda s: s[NETRO_SCHEDULE_START_TIME]),
                reverse=True,
            )
            self._active_zones[zone_key].past_schedules = past

            coming = sorted(
                [
                    s for s in schedules
                    if s[NETRO_SCHEDULE_ZONE] == zone_key
                    and s[NETRO_SCHEDULE_STATUS] == NETRO_SCHEDULE_VALID
                    and s[NETRO_SCHEDULE_START_TIME] > strftime("%Y-%m-%dT%H:%M:%S", gmtime())
                ],
                key=(lambda s: s[NETRO_SCHEDULE_START_TIME]),
                reverse=False,
            )
            self._active_zones[zone_key].coming_schedules = coming

    def _update_from_moistures(self, moistures):
        """Distribute moisture readings across active zones."""
        self._moistures = moistures
        for zone_key in self._active_zones:
            self._active_zones[zone_key].moistures = [
                m for m in moistures if m[NETRO_MOISTURE_ZONE] == zone_key
            ]

    @property
    def enabled(self) -> bool:
        """Return True when the controller is not in standby."""
        return self.status in (NETRO_STATUS_ONLINE, NETRO_STATUS_WATERING, NETRO_STATUS_SETUP)

    @property
    def watering(self) -> bool:
        """Return True when the controller is actively watering."""
        return self.status == NETRO_STATUS_WATERING

    @property
    def active_zones(self) -> dict:
        """Return the dict of active zones keyed by zone index."""
        return self._active_zones

    @property
    def number_of_active_zones(self) -> int | None:
        """Return the count of active zones."""
        return len(self._active_zones) if self._active_zones else None

    @property
    def metadata(self) -> Meta | None:
        """Return the meta data from the last API response."""
        return self._metadata if self._metadata else None

    @property
    def token_remaining(self) -> int | None:
        """Return the remaining API call tokens for today."""
        return self.metadata.token_remaining if self.metadata is not None else None

    def calendar_schedules(
        self,
        start_date: datetime.date | None = None,
        end_date: datetime.date | None = None,
    ):
        """Return calendar events optionally filtered to a date range."""
        return [
            self._calendar_schedule(s)
            for s in self._schedules
            if (
                datetime.datetime.fromisoformat(s[NETRO_SCHEDULE_END_TIME] + TZ_OFFSET)
                > start_date if start_date is not None else True
            )
            and (
                datetime.datetime.fromisoformat(s[NETRO_SCHEDULE_START_TIME] + TZ_OFFSET)
                < end_date if end_date is not None else True
            )
        ]

    @property
    def current_calendar_schedule(self) -> dict | None:
        """Return the current or next upcoming schedule, if any."""
        for s in self._schedules:
            if s[NETRO_SCHEDULE_END_TIME] > strftime("%Y-%m-%dT%H:%M:%S", gmtime()):
                return self._calendar_schedule(s)
        return None

    def _calendar_schedule(self, schedule):
        """Build a calendar-entry dict from a raw Netro schedule dict."""
        return {
            "start": datetime.datetime.fromisoformat(
                schedule[NETRO_SCHEDULE_START_TIME] + TZ_OFFSET
            ),
            "end": datetime.datetime.fromisoformat(
                schedule[NETRO_SCHEDULE_END_TIME] + TZ_OFFSET
            ),
            "summary": f"{self.active_zones[schedule[NETRO_SCHEDULE_ZONE]].name}",
            "description": f"Duration: {round(
                (
                    datetime.datetime.fromisoformat(schedule[NETRO_SCHEDULE_END_TIME] + TZ_OFFSET)
                    - datetime.datetime.fromisoformat(schedule[NETRO_SCHEDULE_START_TIME] + TZ_OFFSET)
                ).seconds / 60
            )} minutes, {
                {
                    NETRO_SCHEDULE_FIX: "schedule from programs",
                    NETRO_SCHEDULE_SMART: "Netro generated schedule",
                    NETRO_SCHEDULE_MANUAL: "manual watering",
                }.get(schedule[NETRO_SCHEDULE_SOURCE],
                      f"unknown source({schedule[NETRO_SCHEDULE_SOURCE]})")
            }, {
                {
                    NETRO_SCHEDULE_EXECUTED: "has been executed",
                    NETRO_SCHEDULE_EXECUTING: "currently being executed",
                    NETRO_SCHEDULE_VALID: "is planned",
                }.get(schedule[NETRO_SCHEDULE_STATUS],
                      f"unknown status({schedule[NETRO_SCHEDULE_STATUS]})")
            }.",
        }

    async def _async_update_data(self):
        """Fetch device info, moistures, and schedules from the Netro Public API v2."""

        # Recalculate polling interval with current slowdown factor
        self.current_slowdown_factor = get_slowdown_factor(
            self.slowdown_factors, datetime.datetime.now()
        )
        self.update_interval = datetime.timedelta(
            minutes=self.refresh_interval * self.current_slowdown_factor
        )

        _LOGGER.debug(
            "Current time is %s, slowdown factor=%d, next update in %d minutes",
            datetime.datetime.now().time().strftime("%H:%M:%S"),
            self.current_slowdown_factor,
            self.update_interval.total_seconds() / 60,
        )
        _LOGGER.info(
            "Polling info for %s controller (every %d minutes%s)",
            self.name,
            self.update_interval.total_seconds() / 60,
            (
                f", slowdown factor={self.current_slowdown_factor}"
                if self.current_slowdown_factor > 1
                else ""
            ),
        )

        # ── GET /npa/v2/info.json ─────────────────────────────────────────
        session = async_get_clientsession(self.hass)
        client = NetroClient(http=AiohttpClient(session), config=NetroConfig())
        # V2: authenticate with api_key, NOT serial_number
        res = await client.get_info(self.api_key)

        device_data = res["data"]["device"]
        meta_data = res["meta"]

        self.zone_num = device_data[NETRO_CONTROLLER_ZONENUM]
        self.status = device_data[NETRO_CONTROLLER_STATUS]
        self._metadata = Meta(
            meta_data[NETRO_METADATA_LAST_ACTIVE],
            meta_data[NETRO_METADATA_TIME],
            meta_data[NETRO_METADATA_TID],
            meta_data[NETRO_METADATA_VERSION],
            meta_data[NETRO_METADATA_TOKEN_LIMIT],
            meta_data[NETRO_METADATA_TOKEN_REMAINING],
            meta_data[NETRO_METADATA_TOKEN_RESET],
        )
        if device_data.get(NETRO_CONTROLLER_BATTERY_LEVEL):
            self.battery_level = device_data[NETRO_CONTROLLER_BATTERY_LEVEL] * 100

        # Rebuild active-zone dict from the fresh info response
        self._active_zones.clear()
        for zone in device_data[NETRO_CONTROLLER_ZONES]:
            if zone[NETRO_ZONE_ENABLED]:
                self._active_zones[zone[NETRO_ZONE_ITH]] = self.Zone(
                    self,
                    zone[NETRO_ZONE_ITH],
                    zone[NETRO_ZONE_ENABLED],
                    zone[NETRO_ZONE_SMART],
                    (
                        zone[NETRO_ZONE_NAME]
                        if zone[NETRO_ZONE_NAME] and len(zone[NETRO_ZONE_NAME]) > 0
                        else self.device_name + "-" + str(zone[NETRO_ZONE_ITH])
                    ),
                    # Pass serial_number (not api_key) — used as the zone identifier prefix
                    self.serial_number,
                )

        # ── GET /npa/v2/moistures.json ────────────────────────────────────
        session = async_get_clientsession(self.hass)
        client = NetroClient(http=AiohttpClient(session), config=NetroConfig())
        # V2: authenticate with api_key
        res = await client.get_moistures(self.api_key)
        self._update_from_moistures(res["data"]["moistures"])

        # ── GET /npa/v2/schedules.json ────────────────────────────────────
        session = async_get_clientsession(self.hass)
        client = NetroClient(http=AiohttpClient(session), config=NetroConfig())
        # V2: authenticate with api_key
        res = await client.get_schedules(
            self.api_key,
            start_date=str(
                datetime.date.today() - relativedelta(months=self.schedules_months_before)
            ),
            end_date=str(
                datetime.date.today() + relativedelta(months=self.schedules_months_after)
            ),
        )
        self._update_from_schedules(res["data"]["schedules"])

    async def enable(self):
        """Enable the controller (set status to ONLINE)."""
        session = async_get_clientsession(self.hass)
        client = NetroClient(http=AiohttpClient(session), config=NetroConfig())
        # V2: authenticate with api_key
        return await client.set_status(self.api_key, enabled=NETRO_STATUS_ENABLE)

    async def disable(self):
        """Disable the controller (set status to STANDBY)."""
        session = async_get_clientsession(self.hass)
        client = NetroClient(http=AiohttpClient(session), config=NetroConfig())
        # V2: authenticate with api_key
        return await client.set_status(self.api_key, enabled=NETRO_STATUS_DISABLE)

    async def no_water(self, days: int | None = None) -> None:
        """Suspend watering for the given number of days (default 1)."""
        session = async_get_clientsession(self.hass)
        client = NetroClient(http=AiohttpClient(session), config=NetroConfig())
        # V2: authenticate with api_key
        await client.no_water(self.api_key, days=days if days is not None else 1)

    async def start_watering(
        self, duration: int, delay: int, start_time: datetime.time
    ) -> None:
        """Start watering all zones for the given duration (minutes)."""
        session = async_get_clientsession(self.hass)
        client = NetroClient(http=AiohttpClient(session), config=NetroConfig())
        # V2: authenticate with api_key
        await client.water(
            self.api_key,
            duration_minutes=duration,
            delay_minutes=delay,
            start_time=(
                start_time.strftime("%Y-%m-%d %H:%M") if start_time is not None else None
            ),
        )

    async def stop_watering(self) -> None:
        """Stop all active watering on this controller."""
        session = async_get_clientsession(self.hass)
        client = NetroClient(http=AiohttpClient(session), config=NetroConfig())
        # V2: authenticate with api_key
        await client.stop_water(self.api_key)

    def __str__(self) -> str:
        """String representation for logging."""
        return (
            f'controller coordinator "{self.name}" '
            f"({NETRO_PIXIE_CONTROLLER_MODEL if hasattr(self, NETRO_CONTROLLER_BATTERY_LEVEL) else NETRO_SPRITE_CONTROLLER_MODEL})"
        )
