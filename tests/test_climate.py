import json

import pytest

from kia2mqtt.bridge import Topics, parse_command
from kia2mqtt.climate import to_options, validate

# A typical "winter" preset a consumer might send.
WINTER = {
    "temp": 72,
    "duration_min": 10,
    "defrost": True,
    "rear_window_heat": True,
    "side_mirror_heat": True,
    "steering_wheel": "high",
    "seats": {"driver": "heat_medium"},
}


def test_winter_example_maps_to_library_options():
    assert validate(WINTER) is None
    o = to_options(WINTER)
    assert (o.set_temp, o.duration, o.climate, o.defrost) == (72, 10, True, True)
    assert o.heating == 1 and o.steering_wheel == 2
    assert (o.front_left_seat, o.front_right_seat, o.rear_left_seat) == (7, 0, 0)


def test_minimal_request_turns_everything_else_off():
    o = to_options({"temp": 68, "duration_min": 5})
    assert (o.defrost, o.heating, o.steering_wheel) == (False, 0, 0)
    # No seats key: the library leaves heatVentSeat out of the request.
    assert o.front_left_seat is None


def test_rear_window_only():
    assert (
        to_options({"temp": 70, "duration_min": 5, "rear_window_heat": True}).heating
        == 2
    )


@pytest.mark.parametrize(
    "body",
    [
        {"duration_min": 10},
        {"temp": 90, "duration_min": 10},
        {"temp": 72},
        {"temp": 72, "duration_min": 60},
        {"temp": "72", "duration_min": 10},
        {"temp": 72, "duration_min": 10, "defrost": "yes"},
        {"temp": 72, "duration_min": 10, "side_mirror_heat": True},
        {"temp": 72, "duration_min": 10, "steering_wheel": "max"},
        {"temp": 72, "duration_min": 10, "seats": {"driver": "toasty"}},
        {"temp": 72, "duration_min": 10, "seats": {"middle": "off"}},
        {"temp": 72, "duration_min": 10, "colour": "blue"},
        {"temp": 72.5, "duration_min": 10},
        {"temp": 30, "temp_unit": "C", "duration_min": 10},
        {"temp": 72, "temp_unit": "K", "duration_min": 10},
        ["off"],
    ],
)
def test_invalid_requests(body):
    assert validate(body) is not None


def test_parse_command():
    topics = Topics("kia", "testcar")
    assert parse_command(topics, topics.climate_set, "OFF").value == "off"
    assert parse_command(topics, topics.climate_set, json.dumps(WINTER)).value == WINTER
    assert isinstance(parse_command(topics, topics.climate_set, "winter"), str)


def test_celsius_is_converted():
    assert to_options({"temp": 22, "temp_unit": "C", "duration_min": 5}).set_temp == 72
    assert validate({"temp": 21.5, "temp_unit": "C", "duration_min": 5}) is None
