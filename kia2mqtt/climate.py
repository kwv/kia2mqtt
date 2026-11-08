"""climate/set payload -> the library's ClimateRequestOptions.

Payload: "off", or a JSON object (all keys but temp/duration_min optional;
anything left out is off):

    {"temp": 72, "temp_unit": "F", "duration_min": 10, "defrost": true,
     "rear_window_heat": true, "side_mirror_heat": true,
     "steering_wheel": "high",
     "seats": {"driver": "heat_medium", "passenger": "off"}}

Which options a given car applies varies by model.
"""

from typing import Any

from hyundai_kia_connect_api.ApiImpl import ClimateRequestOptions

TEMP_F = range(62, 83)  # the US API's settable range, in °F
DURATION_MIN = range(1, 31)

# heatingAccessory.steeringWheelStep (2 = high per another client of this API;
# 1 = low is unverified).
STEERING_WHEEL = {"off": 0, "low": 1, "high": 2}

# The library's const.SEAT_STATUS levels.
SEAT_LEVELS = {
    "off": 0,
    "cool_low": 3,
    "cool_medium": 4,
    "cool_high": 5,
    "heat_low": 6,
    "heat_medium": 7,
    "heat_high": 8,
}

# US cars are left-hand drive: driver = front left.
SEATS = {
    "driver": "front_left_seat",
    "passenger": "front_right_seat",
    "rear_left": "rear_left_seat",
    "rear_right": "rear_right_seat",
}

KEYS = {
    "temp",
    "temp_unit",
    "duration_min",
    "defrost",
    "rear_window_heat",
    "side_mirror_heat",
    "steering_wheel",
    "seats",
}


def validate(body: Any) -> str | None:
    """Returns an error message, or None if the request is valid."""
    if not isinstance(body, dict):
        return 'climate payload must be "off" or a JSON object'
    unknown = body.keys() - KEYS
    if unknown:
        return f"unknown climate keys: {sorted(unknown)}"
    if body.get("temp_unit", "F") not in ("F", "C"):
        return 'temp_unit must be "F" or "C"'
    if temp_f(body) not in TEMP_F:
        return "temp is required: 62-82 °F or 17-28 °C"
    if _int(body.get("duration_min")) not in DURATION_MIN:
        return "duration_min is required, 1-30"
    for key in ("defrost", "rear_window_heat", "side_mirror_heat"):
        if key in body and not isinstance(body[key], bool):
            return f"{key} must be true or false"
    if body.get("side_mirror_heat") and not body.get("rear_window_heat"):
        # The API's heating code has no "mirrors only" value.
        return "side_mirror_heat needs rear_window_heat"
    if body.get("steering_wheel", "off") not in STEERING_WHEEL:
        return f"steering_wheel must be one of {list(STEERING_WHEEL)}"
    seats = body.get("seats", {})
    if not isinstance(seats, dict):
        return "seats must be an object"
    for seat, level in seats.items():
        if seat not in SEATS:
            return f"unknown seat {seat!r}; use {list(SEATS)}"
        if level not in SEAT_LEVELS:
            return f"seat level must be one of {list(SEAT_LEVELS)}"
    return None


def to_options(body: dict[str, Any]) -> ClimateRequestOptions:
    """body must have passed validate()."""
    rear = body.get("rear_window_heat", False)
    mirrors = body.get("side_mirror_heat", False)
    options = ClimateRequestOptions(
        set_temp=temp_f(body),
        duration=body["duration_min"],
        climate=True,
        defrost=body.get("defrost", False),
        # KiaUvoApiUSA: 1 = rear window + side mirrors, 2 = rear window only.
        heating=1 if rear and mirrors else 2 if rear else 0,
        steering_wheel=STEERING_WHEEL[body.get("steering_wheel", "off")],
    )
    if "seats" in body:
        # Once any seat is given, the request sets all four; unnamed seats off.
        for seat, field in SEATS.items():
            setattr(options, field, SEAT_LEVELS[body["seats"].get(seat, "off")])
    return options


def temp_f(body: dict[str, Any]) -> int | None:
    """The requested temperature in whole °F, or None if not a number."""
    temp = body.get("temp")
    if isinstance(temp, bool) or not isinstance(temp, (int, float)):
        return None
    if body.get("temp_unit", "F") == "C":
        return round(temp * 9 / 5 + 32)
    return temp if isinstance(temp, int) else None


def _int(value: Any) -> int | None:
    return value if type(value) is int else None
