from kia2mqtt.status import FIELDS, kia_utc_to_iso, parse_status

EV = "lastVehicleInfo.vehicleStatusRpt.vehicleStatus.evStatus"


def test_parses_probe_payload(gvi):
    assert parse_status(gvi) == {
        "soc": "66",
        "charging": "true",
        "plugged": "true",
        "power_kw": "1.4",
        "remain_min": "650",
        "range": "220.59",
        "odometer": "13259",
        "distance_unit": "mi",
        "locked": "true",
        "climate": "false",
        "target_soc_ac": "80",
        "target_soc_dc": "100",
        "updated": "2026-11-08T13:41:47Z",
    }


def test_only_known_fields(gvi):
    assert set(parse_status(gvi)) <= set(FIELDS)


def test_missing_values_are_left_out_not_false(gvi):
    ev = gvi["lastVehicleInfo"]["vehicleStatusRpt"]["vehicleStatus"]["evStatus"]
    del ev["batteryCharge"]
    del ev["batteryPlugin"]
    del ev["realTimePower"]
    del gvi["lastVehicleInfo"]["vehicleStatusRpt"]["vehicleStatus"]["doorLock"]
    status = parse_status(gvi)
    for field in ("charging", "plugged", "power_kw", "locked"):
        assert field not in status


def test_empty_payload_publishes_nothing():
    assert parse_status({}) == {}


def test_unplugged(gvi):
    ev = gvi["lastVehicleInfo"]["vehicleStatusRpt"]["vehicleStatus"]["evStatus"]
    ev["batteryPlugin"] = 0
    assert parse_status(gvi)["plugged"] == "false"


def test_out_of_range_soc_is_dropped(gvi):
    ev = gvi["lastVehicleInfo"]["vehicleStatusRpt"]["vehicleStatus"]["evStatus"]
    ev["batteryStatus"] = 255
    assert "soc" not in parse_status(gvi)


def test_range_ignores_non_mile_units_and_falls_back(gvi):
    vs = gvi["lastVehicleInfo"]["vehicleStatusRpt"]["vehicleStatus"]
    vs["evStatus"]["drvDistance"][0]["rangeByFuel"]["totalAvailableRange"]["unit"] = 1
    vs["distanceToEmpty"] = {"value": 200, "unit": 3}
    assert parse_status(gvi)["range"] == "200"
    vs["distanceToEmpty"]["unit"] = 1
    assert "range" not in parse_status(gvi)


def test_target_soc_fallback_from_evc_gts(gvi):
    ev = gvi["lastVehicleInfo"]["vehicleStatusRpt"]["vehicleStatus"]["evStatus"]
    del ev["targetSOC"]
    status = parse_status(gvi, fallback_ac=90, fallback_dc=None)
    assert status["target_soc_ac"] == "90"
    assert "target_soc_dc" not in status


def test_zero_target_soc_is_no_data(gvi):
    ev = gvi["lastVehicleInfo"]["vehicleStatusRpt"]["vehicleStatus"]["evStatus"]
    ev["targetSOC"][0]["targetSOClevel"] = 0
    assert "target_soc_ac" not in parse_status(gvi)


def test_timestamps():
    assert kia_utc_to_iso("20261108134147") == "2026-11-08T13:41:47Z"
    assert kia_utc_to_iso("garbage") is None
    assert kia_utc_to_iso(None) is None


def test_location_only_when_enabled(gvi):
    gvi["lastVehicleInfo"]["location"] = {
        "coord": {"lat": 12.345, "lon": -67.89},
        "syncDate": {"utc": "20261108134147"},
    }
    assert "latitude" not in parse_status(gvi)
    status = parse_status(gvi, location=True)
    assert (status["latitude"], status["longitude"]) == ("12.345", "-67.89")
    assert status["location_updated"] == "2026-11-08T13:41:47Z"


def test_zero_location_is_unknown(gvi):
    gvi["lastVehicleInfo"]["location"] = {"coord": {"lat": 0, "lon": 0}}
    assert "latitude" not in parse_status(gvi, location=True)
