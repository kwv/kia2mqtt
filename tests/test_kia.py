import datetime as dt
from unittest.mock import MagicMock

import pytest
from hyundai_kia_connect_api.ApiImpl import OTPRequest
from hyundai_kia_connect_api.const import ORDER_STATUS
from hyundai_kia_connect_api.Token import Token
from hyundai_kia_connect_api.Vehicle import Vehicle

from kia2mqtt import store
from kia2mqtt.kia import KiaApi, KiaClient, OtpRequired


def test_status_request_asks_for_location_only_when_enabled():
    api = KiaApi(3, 1, "en")
    response = MagicMock()
    response.json.return_value = {"payload": {"vehicleInfoList": [{"x": 1}]}}
    api.post_request_with_logging_and_active_session = MagicMock(return_value=response)
    vehicle = Vehicle(id="1", key="KEY")
    assert api._get_cached_vehicle_state(Token("u", "p"), vehicle) == {"x": 1}
    body = api.post_request_with_logging_and_active_session.call_args.kwargs[
        "json_body"
    ]
    assert body["vehicleInfoReq"]["location"] == "0"
    assert body["vinKey"] == ["KEY"]

    api.request_location = True
    api._get_cached_vehicle_state(Token("u", "p"), vehicle)
    body = api.post_request_with_logging_and_active_session.call_args.kwargs[
        "json_body"
    ]
    assert body["vehicleInfoReq"]["location"] == "1"


def make_token(hours=10):
    return Token(
        username="you@example.com",
        password="pw",
        access_token="sid",
        refresh_token="rm",
        device_id="DEV-1",
        valid_until=dt.datetime.now(dt.UTC) + dt.timedelta(hours=hours),
    )


def fake_api():
    api = MagicMock()
    api.get_vehicles.return_value = [Vehicle(id="1", name="My EV9", key="KEY")]
    return api


def test_reuses_saved_token_and_device_id(settings):
    store.save_token(settings.token_file, make_token())
    api = fake_api()
    client = KiaClient(settings, api=api)
    assert api.device_id == "DEV-1"
    client.ensure_session()
    api.login.assert_not_called()
    assert client.vehicle.key == "KEY"


def test_expired_session_logs_in_with_rmtoken(settings):
    store.save_token(settings.token_file, make_token(hours=-1))
    api = fake_api()
    api.login.return_value = make_token()
    client = KiaClient(settings, api=api)
    client.ensure_session()
    assert api.login.call_args.kwargs["token"].refresh_token == "rm"


def test_otp_request_raises(settings):
    api = fake_api()
    api.login.return_value = OTPRequest("xid", "key", True, False, None, None)
    client = KiaClient(settings, api=api)
    with pytest.raises(OtpRequired):
        client.ensure_session()
    assert client.otp_request.otp_key == "key"
    api.send_otp.assert_not_called()  # sending is the bridge's decision


def test_vehicle_selection(settings, tmp_path):
    api = fake_api()
    api.get_vehicles.return_value = [
        Vehicle(id="1", name="My EV9", key="A"),
        Vehicle(id="2", name="Other", key="B"),
    ]
    store.save_token(settings.token_file, make_token())
    with pytest.raises(LookupError):
        KiaClient(settings, api=api).ensure_session()

    named = settings.__class__(**{**vars(settings), "kia_vehicle": "my ev9"})
    client = KiaClient(named, api=api)
    client.ensure_session()
    assert client.vehicle.key == "A"


def test_climate_start_and_stop(settings):
    store.save_token(settings.token_file, make_token())
    api = fake_api()
    api.start_climate.return_value = "XID"
    api.check_action_status.return_value = ORDER_STATUS.SUCCESS
    client = KiaClient(settings, api=api)
    client.ensure_session()

    assert client.climate({"temp": 72, "duration_min": 10}).status == "SUCCESS"
    assert api.start_climate.call_args.args[2].set_temp == 72

    client.climate("off")
    api.stop_climate.assert_called_once()


def test_library_status_path_uses_override_end_to_end(settings, gvi):
    """Runs the library's own update path with only HTTP mocked, so a library
    bump that renames the overridden method (and re-enables location) fails."""
    store.save_token(settings.token_file, make_token())
    api = KiaApi(3, 1, "en")
    posts = []

    def post(url, json=None, headers=None):
        posts.append((url, json))
        resp = MagicMock()
        resp.json.return_value = {
            "status": {"statusCode": 0},
            "payload": {"vehicleInfoList": [gvi]},
        }
        return resp

    def get(url, headers=None):
        resp = MagicMock()
        resp.json.return_value = {
            "status": {"statusCode": 0},
            "payload": {"targetSOClist": [{"plugType": 1, "targetSOClevel": 80}]},
        }
        return resp

    api.session.post = post
    api.session.get = get
    api.get_vehicles = MagicMock(return_value=[Vehicle(id="1", key="KEY")])
    client = KiaClient(settings, api=api)
    client.ensure_session()
    info, ac, dc = client.fetch_status()

    assert [u for u, _ in posts] == [api.API_URL + "cmm/gvi"]
    assert posts[0][1]["vehicleInfoReq"]["location"] == "0"
    assert info is gvi or info == gvi
    assert ac == 80
