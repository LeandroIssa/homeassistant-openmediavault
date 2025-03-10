"""OpenMediaVault Controller."""

import asyncio
import pytz
from datetime import datetime, timedelta

from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN
from .apiparser import parse_api
from .omv_api import OpenMediaVaultAPI

DEFAULT_TIME_ZONE = None


def utc_from_timestamp(timestamp: float) -> datetime:
    """Return a UTC time from a timestamp."""
    return pytz.utc.localize(datetime.utcfromtimestamp(timestamp))


def as_local(dattim: datetime) -> datetime:
    """Convert a UTC datetime object to local time zone."""
    if dattim.tzinfo == DEFAULT_TIME_ZONE:
        return dattim
    if dattim.tzinfo is None:
        dattim = pytz.utc.localize(dattim)

    return dattim.astimezone(DEFAULT_TIME_ZONE)


# ---------------------------
#   OMVControllerData
# ---------------------------
class OMVControllerData(object):
    """OMVControllerData Class."""

    def __init__(self, hass, config_entry):
        """Initialize OMVController."""
        self.hass = hass
        self.config_entry = config_entry
        self.name = config_entry.data[CONF_NAME]
        self.host = config_entry.data[CONF_HOST]

        self.data = {
            "hwinfo": {},
            "plugin": {},
            "disk": {},
            "fs": {},
            "service": {},
        }

        self.listeners = []
        self.lock = asyncio.Lock()

        self.api = OpenMediaVaultAPI(
            hass,
            config_entry.data[CONF_HOST],
            config_entry.data[CONF_USERNAME],
            config_entry.data[CONF_PASSWORD],
            config_entry.data[CONF_SSL],
            config_entry.data[CONF_VERIFY_SSL],
        )

        self._force_update_callback = None
        self._force_hwinfo_update_callback = None

    # ---------------------------
    #   async_init
    # ---------------------------
    async def async_init(self):
        self._force_update_callback = async_track_time_interval(
            self.hass, self.force_update, timedelta(seconds=60)
        )
        self._force_hwinfo_update_callback = async_track_time_interval(
            self.hass, self.force_hwinfo_update, timedelta(seconds=3600)
        )

    # ---------------------------
    #   signal_update
    # ---------------------------
    @property
    def signal_update(self):
        """Event to signal new data."""
        return f"{DOMAIN}-update-{self.name}"

    # ---------------------------
    #   async_reset
    # ---------------------------
    async def async_reset(self):
        """Reset dispatchers."""
        for unsub_dispatcher in self.listeners:
            unsub_dispatcher()

        self.listeners = []
        return True

    # ---------------------------
    #   connected
    # ---------------------------
    def connected(self):
        """Return connected state."""
        return self.api.connected()

    # ---------------------------
    #   force_hwinfo_update
    # ---------------------------
    @callback
    async def force_hwinfo_update(self, _now=None):
        """Trigger update by timer."""
        await self.async_hwinfo_update()

    # ---------------------------
    #   async_hwinfo_update
    # ---------------------------
    async def async_hwinfo_update(self):
        """Update OpenMediaVault hardware info."""
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=30)
        except Exception:
            return

        await self.hass.async_add_executor_job(self.get_hwinfo)
        if self.api.connected():
            await self.hass.async_add_executor_job(self.get_plugin)
        if self.api.connected():
            await self.hass.async_add_executor_job(self.get_disk)

        self.lock.release()

    # ---------------------------
    #   force_update
    # ---------------------------
    @callback
    async def force_update(self, _now=None):
        """Trigger update by timer."""
        await self.async_update()

    # ---------------------------
    #   async_update
    # ---------------------------
    async def async_update(self):
        """Update OMV data."""
        if self.api.has_reconnected():
            await self.async_hwinfo_update()

        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=10)
        except Exception:
            return

        await self.hass.async_add_executor_job(self.get_hwinfo)
        if self.api.connected():
            await self.hass.async_add_executor_job(self.get_fs)
        if self.api.connected():
            await self.hass.async_add_executor_job(self.get_smart)
        if self.api.connected():
            await self.hass.async_add_executor_job(self.get_service)

        async_dispatcher_send(self.hass, self.signal_update)
        self.lock.release()

    # ---------------------------
    #   get_hwinfo
    # ---------------------------
    def get_hwinfo(self):
        """Get hardware info from OMV."""
        self.data["hwinfo"] = parse_api(
            data=self.data["hwinfo"],
            source=self.api.query("System", "getInformation"),
            vals=[
                {"name": "hostname", "default": "unknown"},
                {"name": "version", "default": "unknown"},
                {"name": "cpuUsage", "default": 0},
                {"name": "memTotal", "default": 0},
                {"name": "memUsed", "default": 0},
                {"name": "uptime", "default": "0 days 0 hours 0 minutes 0 seconds"},
                {"name": "configDirty", "type": "bool", "default": False},
                {"name": "rebootRequired", "type": "bool", "default": False},
                {"name": "pkgUpdatesAvailable", "type": "bool", "default": False},
            ],
            ensure_vals=[{"name": "memUsage", "default": 0}],
        )

        if not self.api.connected():
            return

        tmp_uptime = 0
        if int(self.data["hwinfo"]["version"].split(".")[0]) > 5:
            tmp = self.data["hwinfo"]["uptime"]
            pos = abs(int(tmp))
            day = pos / (3600 * 24)
            rem = pos % (3600 * 24)
            hour = rem / 3600
            rem = rem % 3600
            mins = rem / 60
            secs = rem % 60
            res = "%d days %02d hours %02d minutes %02d seconds" % (
                day,
                hour,
                mins,
                secs,
            )
            if int(tmp) < 0:
                res = "-%s" % res
            tmp = res.split(" ")
        else:
            tmp = self.data["hwinfo"]["uptime"].split(" ")

        tmp_uptime += int(tmp[0]) * 86400  # days
        tmp_uptime += int(tmp[2]) * 3600  # hours
        tmp_uptime += int(tmp[4]) * 60  # minutes
        tmp_uptime += int(tmp[6])  # seconds
        now = datetime.now().replace(microsecond=0)
        uptime_tm = datetime.timestamp(now - timedelta(seconds=tmp_uptime))
        self.data["hwinfo"]["uptimeEpoch"] = str(
            as_local(utc_from_timestamp(uptime_tm)).isoformat()
        )

        self.data["hwinfo"]["cpuUsage"] = round(self.data["hwinfo"]["cpuUsage"], 1)
        mem = (
            (int(self.data["hwinfo"]["memUsed"]) / int(self.data["hwinfo"]["memTotal"]))
            * 100
            if int(self.data["hwinfo"]["memTotal"]) > 0
            else 0
        )
        self.data["hwinfo"]["memUsage"] = round(mem, 1)

    # ---------------------------
    #   get_disk
    # ---------------------------
    def get_disk(self):
        """Get all filesystems from OMV."""
        self.data["disk"] = parse_api(
            data=self.data["disk"],
            source=self.api.query("DiskMgmt", "enumerateDevices"),
            key="devicename",
            vals=[
                {"name": "devicename"},
                {"name": "canonicaldevicefile"},
                {"name": "size", "default": "unknown"},
                {"name": "israid", "type": "bool", "default": False},
                {"name": "isroot", "type": "bool", "default": False},
            ],
            ensure_vals=[
                {"name": "devicemodel", "default": "unknown"},
                {"name": "serialnumber", "default": "unknown"},
                {"name": "firmwareversion", "default": "unknown"},
                {"name": "sectorsize", "default": "unknown"},
                {"name": "rotationrate", "default": "unknown"},
                {"name": "writecacheis", "default": "unknown"},
                {"name": "smartsupportis", "default": "unknown"},
                {"name": "Raw_Read_Error_Rate", "default": "unknown"},
                {"name": "Spin_Up_Time", "default": "unknown"},
                {"name": "Start_Stop_Count", "default": "unknown"},
                {"name": "Reallocated_Sector_Ct", "default": "unknown"},
                {"name": "Seek_Error_Rate", "default": "unknown"},
                {"name": "Load_Cycle_Count", "default": "unknown"},
                {"name": "Temperature_Celsius", "default": "unknown"},
                {"name": "UDMA_CRC_Error_Count", "default": "unknown"},
                {"name": "Multi_Zone_Error_Rate", "default": "unknown"},
            ],
        )

    # ---------------------------
    #   get_smart
    # ---------------------------
    def get_smart(self):
        for uid in self.data["disk"]:
            if self.data["disk"][uid]["devicename"].startswith("mmcblk"):
                continue

            if self.data["disk"][uid]["devicename"].startswith("sr"):
                continue

            if self.data["disk"][uid]["devicename"].startswith("bcache"):
                continue

            tmp_data = parse_api(
                data={},
                source=self.api.query(
                    "Smart",
                    "getInformation",
                    {"devicefile": self.data["disk"][uid]["canonicaldevicefile"]},
                ),
                vals=[
                    {"name": "devicemodel", "default": "unknown"},
                    {"name": "serialnumber", "default": "unknown"},
                    {"name": "firmwareversion", "default": "unknown"},
                    {"name": "sectorsize", "default": "unknown"},
                    {"name": "rotationrate", "default": "unknown"},
                    {"name": "writecacheis", "type": "bool", "default": False},
                    {"name": "smartsupportis", "type": "bool", "default": False},
                ],
            )

            if not tmp_data:
                continue

            self.data["disk"][uid]["devicemodel"] = tmp_data["devicemodel"]
            self.data["disk"][uid]["serialnumber"] = tmp_data["serialnumber"]
            self.data["disk"][uid]["firmwareversion"] = tmp_data["firmwareversion"]
            self.data["disk"][uid]["sectorsize"] = tmp_data["sectorsize"]
            self.data["disk"][uid]["rotationrate"] = tmp_data["rotationrate"]
            self.data["disk"][uid]["writecacheis"] = tmp_data["writecacheis"]
            self.data["disk"][uid]["smartsupportis"] = tmp_data["smartsupportis"]

            tmp_data = parse_api(
                data={},
                source=self.api.query(
                    "Smart",
                    "getAttributes",
                    {"devicefile": self.data["disk"][uid]["canonicaldevicefile"]},
                ),
                key="attrname",
                vals=[
                    {"name": "attrname"},
                    {"name": "threshold", "default": 0},
                    {"name": "rawvalue", "default": 0},
                ],
            )
            if not tmp_data:
                continue

            vals = [
                "Raw_Read_Error_Rate",
                "Spin_Up_Time",
                "Start_Stop_Count",
                "Reallocated_Sector_Ct",
                "Seek_Error_Rate",
                "Load_Cycle_Count",
                "Temperature_Celsius",
                "UDMA_CRC_Error_Count",
                "Multi_Zone_Error_Rate",
            ]

            for tmp_val in vals:
                if tmp_val in tmp_data:
                    if (
                        isinstance(tmp_data[tmp_val]["rawvalue"], str)
                        and " " in tmp_data[tmp_val]["rawvalue"]
                    ):
                        tmp_data[tmp_val]["rawvalue"] = tmp_data[tmp_val][
                            "rawvalue"
                        ].split(" ")[0]

                    self.data["disk"][uid][tmp_val] = tmp_data[tmp_val]["rawvalue"]

    # ---------------------------
    #   get_fs
    # ---------------------------
    def get_fs(self):
        """Get all filesystems from OMV."""
        self.data["fs"] = parse_api(
            data=self.data["fs"],
            source=self.api.query("FileSystemMgmt", "enumerateFilesystems"),
            key="uuid",
            vals=[
                {"name": "uuid"},
                {"name": "parentdevicefile", "default": "unknown"},
                {"name": "label", "default": "unknown"},
                {"name": "type", "default": "unknown"},
                {"name": "mountpoint", "default": "unknown"},
                {"name": "available", "default": "unknown"},
                {"name": "size", "default": "unknown"},
                {"name": "percentage", "default": "unknown"},
                {"name": "_readonly", "type": "bool", "default": False},
                {"name": "_used", "type": "bool", "default": False},
            ],
            skip=[
                {"name": "type", "value": "swap"},
                {"name": "type", "value": "iso9660"},
            ],
        )

        for uid in self.data["fs"]:
            self.data["fs"][uid]["size"] = round(
                int(self.data["fs"][uid]["size"]) / 1073741824, 1
            )
            self.data["fs"][uid]["available"] = round(
                int(self.data["fs"][uid]["available"]) / 1073741824, 1
            )

    # ---------------------------
    #   get_service
    # ---------------------------
    def get_service(self):
        """Get OMV services status"""
        tmp = self.api.query("Services", "getStatus")
        if "data" in tmp:
            tmp = tmp["data"]

        self.data["service"] = parse_api(
            data=self.data["service"],
            source=tmp,
            key="name",
            vals=[
                {"name": "name"},
                {"name": "title", "default": "unknown"},
                {"name": "enabled", "type": "bool", "default": False},
                {"name": "running", "type": "bool", "default": False},
            ],
        )

    # ---------------------------
    #   get_plugin
    # ---------------------------
    def get_plugin(self):
        """Get OMV plugin status"""
        self.data["plugin"] = parse_api(
            data=self.data["plugin"],
            source=self.api.query("Plugin", "enumeratePlugins"),
            key="name",
            vals=[
                {"name": "name"},
                {"name": "installed", "type": "bool", "default": False},
            ],
        )
