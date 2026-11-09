"""Map a cmm/gvi vehicleInfoList entry to the published status fields.

parse_status() returns {field: value} with JSON-ready values (int, float,
bool, ISO time string). A field whose source value is missing or unusable is
left out, so it is never published as false or 0.
"""

import datetime as dt
from typing import Any

from hyundai_kia_connect_api.utils import get_child_value

FIELDS = (
    "soc",
    "charging",
    "plugged",
    "power_kw",
    "remain_min",
    "range",
    "odometer",
    "distance_unit",
    "locked",
    "climate",
    "target_soc_ac",
    "target_soc_dc",
    "updated",
)

# Published only when location is enabled (KIA_LOCATION=true).
LOCATION_FIELDS = ("latitude", "longitude", "location_updated")

_STATUS = "lastVehicleInfo.vehicleStatusRpt.vehicleStatus"
_EV = _STATUS + ".evStatus"

# Kia distance unit codes that mean miles (const.DISTANCE_UNITS in the library).
_MILE_UNITS = (2, 3)
# timeInterval.unit 4 = minutes.
_MINUTES_UNIT = 4

PLUG_AC = 1
PLUG_DC = 0


def kia_utc_to_iso(value: Any) -> str | None:
    """'20261108134147' (a *.utc field) -> '2026-11-08T13:41:47Z'.

    Only use *.utc fields: the local timestamps (e.g. mileageSyncDate) are at a
    fixed -8 h offset, not the car's time zone.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.strptime(value, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    if float(value).is_integer():
        return int(value)
    return float(value)


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _distance_mi(info: dict[str, Any], path: str) -> int | float | None:
    if get_child_value(info, path + ".unit") not in _MILE_UNITS:
        return None
    return _number(get_child_value(info, path + ".value"))


def target_socs(info: dict[str, Any]) -> dict[int, int]:
    """{plugType: targetSOClevel} from evStatus.targetSOC (0 = no data)."""
    result: dict[int, int] = {}
    for entry in get_child_value(info, _EV + ".targetSOC") or []:
        if not isinstance(entry, dict):
            continue
        level = _number(entry.get("targetSOClevel"))
        plug = entry.get("plugType")
        if isinstance(level, int) and level > 0 and plug in (PLUG_AC, PLUG_DC):
            result[plug] = level
    return result


def parse_status(
    info: dict[str, Any],
    fallback_ac: int | None = None,
    fallback_dc: int | None = None,
    location: bool = False,
) -> dict[str, Any]:
    """fallback_ac/dc: charge targets from evc/gts, used when gvi lacks them.
    location: include LOCATION_FIELDS (otherwise they are never returned)."""
    values: dict[str, Any] = {}

    soc = _number(get_child_value(info, _EV + ".batteryStatus"))
    if soc is not None and 0 <= soc <= 100:
        values["soc"] = soc

    values["charging"] = _bool(get_child_value(info, _EV + ".batteryCharge"))

    plugin = _number(get_child_value(info, _EV + ".batteryPlugin"))
    if plugin is not None:
        values["plugged"] = plugin != 0

    values["power_kw"] = _number(get_child_value(info, _EV + ".realTimePower"))

    remain = get_child_value(info, _EV + ".remainChargeTime.0.timeInterval")
    if isinstance(remain, dict) and remain.get("unit") == _MINUTES_UNIT:
        values["remain_min"] = _number(remain.get("value"))

    # Distances share one unit, published as distance_unit. The US API
    # reports miles; mileage has no unit field (miles, checked against the
    # service schedule), and a range in any other unit is dropped.
    range_ = _distance_mi(info, _EV + ".drvDistance.0.rangeByFuel.totalAvailableRange")
    if range_ is None:
        range_ = _distance_mi(info, _STATUS + ".distanceToEmpty")
    values["range"] = range_
    values["odometer"] = _number(
        get_child_value(info, "vehicleConfig.vehicleDetail.vehicle.mileage")
    )
    if values["range"] is not None or values["odometer"] is not None:
        values["distance_unit"] = "mi"

    values["locked"] = _bool(get_child_value(info, _STATUS + ".doorLock"))
    values["climate"] = _bool(get_child_value(info, _STATUS + ".climate.airCtrl"))

    targets = target_socs(info)
    values["target_soc_ac"] = targets.get(PLUG_AC, fallback_ac)
    values["target_soc_dc"] = targets.get(PLUG_DC, fallback_dc)

    values["updated"] = kia_utc_to_iso(
        get_child_value(info, _EV + ".syncDate.utc")
        or get_child_value(info, _STATUS + ".syncDate.utc")
    )

    if location:
        lat = _number(get_child_value(info, "lastVehicleInfo.location.coord.lat"))
        lon = _number(get_child_value(info, "lastVehicleInfo.location.coord.lon"))
        if lat is not None and lon is not None and (lat, lon) != (0, 0):
            values["latitude"] = lat
            values["longitude"] = lon
            values["location_updated"] = kia_utc_to_iso(
                get_child_value(info, "lastVehicleInfo.location.syncDate.utc")
            )

    return {field: value for field, value in values.items() if value is not None}
