"""KiaClient against the real library with only HTTP faked.

These tests pin the exact requests kia2mqtt makes through
hyundai_kia_connect_api, so a library bump that changes what is sent to Kia
(an extra call, a different endpoint or body) fails CI instead of reaching the
car. When one fails after a bump, read the library diff before updating the
expected request.
"""

import datetime as dt
from unittest.mock import MagicMock

import pytest
from hyundai_kia_connect_api.const import OTP_NOTIFY_TYPE
from hyundai_kia_connect_api.Token import Token

from kia2mqtt import store
from kia2mqtt.kia import KiaApi, KiaClient, OtpRequired

KEY = "VEHICLE-KEY"
XID = "XID-1"
# Endpoints that change the car. Polls must never call these.
WRITES = (
    "rems/door/lock",
    "rems/door/unlock",
    "rems/start",
    "rems/stop",
    "rems/rvs",
    "evc/charge",
    "evc/cancel",
    "evc/sts",
)


class FakeKia:
    """Stands in for requests.Session: records calls, answers like Kia."""

    def __init__(self, api, gvi):
        self.api = api
        self.gvi = gvi
        self.calls = []  # (method, path, json body or None, headers)
        self.login_payload = None  # set to an OTP payload to ask for a code
        api.session.get = self.get
        api.session.post = self.post

    def path(self, url):
        assert url.startswith(self.api.API_URL), url
        return url[len(self.api.API_URL) :]

    def respond(self, payload=None, headers=None):
        resp = MagicMock()
        resp.json.return_value = {"status": {"statusCode": 0}, "payload": payload}
        resp.headers = headers or {}
        resp.text = ""
        return resp

    def get(self, url, headers=None):
        path = self.path(url)
        self.calls.append(("GET", path, None, headers))
        if path == "ownr/gvl":
            return self.respond(
                {
                    "vehicleSummary": [
                        {
                            "vehicleIdentifier": "1",
                            "nickName": "My EV9",
                            "modelName": "EV9",
                            "vehicleKey": KEY,
                            "fuelType": 4,
                        }
                    ]
                }
            )
        if path == "evc/gts":
            return self.respond(
                {
                    "targetSOClist": [
                        {"plugType": 0, "targetSOClevel": 100},
                        {"plugType": 1, "targetSOClevel": 80},
                    ]
                }
            )
        return self.respond({}, {"Xid": XID})

    def post(self, url, json=None, headers=None):
        path = self.path(url)
        self.calls.append(("POST", path, json, headers))
        if path == "prof/authUser":
            if self.login_payload:
                return self.respond(self.login_payload, {"xid": "OTP-XID"})
            return self.respond({}, {"sid": "SID-2"})
        if path == "cmm/verifyOTP":
            return self.respond({}, {"sid": "SID-OTP", "rmtoken": "RM-2"})
        if path == "cmm/gvi":
            return self.respond({"vehicleInfoList": [self.gvi]})
        if path == "cmm/gts":
            return self.respond({"remoteStatus": 0, "evStatus": 0})
        return self.respond({}, {"Xid": XID})

    def requests(self):
        return [(m, p, body) for m, p, body, _ in self.calls]


def make_token(hours=10):
    return Token(
        username="you@example.com",
        password="pw",
        access_token="SID-1",
        refresh_token="RM-1",
        device_id="DEV-1",
        valid_until=dt.datetime.now(dt.UTC) + dt.timedelta(hours=hours),
    )


@pytest.fixture
def kia(settings, gvi):
    """A logged-in KiaClient (saved session) with HTTP faked."""
    store.save_token(settings.token_file, make_token())
    api = KiaApi(3, 1, "en")
    fake = FakeKia(api, gvi)
    client = KiaClient(settings, api=api)
    client.ensure_session()
    assert fake.requests() == [("GET", "ownr/gvl", None)]
    fake.calls.clear()
    return client, fake


def test_poll_only_reads(kia):
    client, fake = kia
    info, ac, dc = client.fetch_status()
    assert [(m, p) for m, p, _ in fake.requests()] == [
        ("POST", "cmm/gvi"),
        ("GET", "evc/gts"),
    ]
    assert not [p for _, p, _ in fake.requests() if p in WRITES]
    assert fake.calls[0][2]["vehicleInfoReq"]["location"] == "0"
    assert fake.calls[0][2]["vinKey"] == [KEY]
    assert (ac, dc) == (80, 100)
    assert info["vehicleConfig"]["vehicleDetail"]["vehicle"]["mileage"]


def test_force_refresh_wakes_car_once_then_reads(kia):
    client, fake = kia
    client.force_refresh()
    assert [(m, p) for m, p, _ in fake.requests()] == [
        ("POST", "rems/rvs"),
        ("POST", "cmm/gvi"),
        ("GET", "evc/gts"),
    ]


CONFIRM = ("POST", "cmm/gts", {"xid": XID})

CLIMATE_BODY = {
    "temp": 72,
    "duration_min": 10,
    "defrost": True,
    "rear_window_heat": True,
    "side_mirror_heat": True,
    "steering_wheel": "high",
    "seats": {"driver": "heat_medium"},
}


@pytest.mark.parametrize(
    "call, expected",
    [
        (lambda c: c.lock(True), ("GET", "rems/door/lock", None)),
        (lambda c: c.lock(False), ("GET", "rems/door/unlock", None)),
        (lambda c: c.charge(True), ("POST", "evc/charge", {"chargeRatio": 100})),
        (lambda c: c.charge(False), ("GET", "evc/cancel", None)),
        (
            lambda c: c.set_charge_limits(90, 80),
            (
                "POST",
                "evc/sts",
                {
                    "targetSOClist": [
                        {"plugType": 0, "targetSOClevel": 80},
                        {"plugType": 1, "targetSOClevel": 90},
                    ]
                },
            ),
        ),
        (lambda c: c.climate("off"), ("GET", "rems/stop", None)),
        (
            lambda c: c.climate({"temp": 72, "duration_min": 10}),
            (
                "POST",
                "rems/start",
                {
                    "remoteClimate": {
                        "airTemp": {"unit": 1, "value": "72"},
                        "airCtrl": True,
                        "defrost": False,
                        "heatingAccessory": {
                            "rearWindow": 0,
                            "sideMirror": 0,
                            "steeringWheel": 0,
                            "steeringWheelStep": 0,
                        },
                        "ignitionOnDuration": {"unit": 4, "value": 10},
                    }
                },
            ),
        ),
    ],
    ids=[
        "lock",
        "unlock",
        "charge_start",
        "charge_stop",
        "charge_limits",
        "climate_off",
        "climate_on",
    ],
)
def test_command_sends_one_request_then_confirms(kia, call, expected):
    client, fake = kia
    result = call(client)
    assert fake.requests() == [expected, CONFIRM]
    assert result.status == "SUCCESS" and result.xid == XID
    method, path, body, headers = fake.calls[0]
    assert headers["vinkey"] == KEY and headers["sid"] == "SID-1"


def test_climate_with_all_options(kia):
    client, fake = kia
    client.climate(CLIMATE_BODY)
    method, path, body = fake.requests()[0]
    assert (method, path) == ("POST", "rems/start")
    climate = body["remoteClimate"]
    assert climate["defrost"] is True
    assert climate["heatingAccessory"] == {
        "rearWindow": 1,
        "sideMirror": 1,
        "steeringWheel": 1,
        "steeringWheelStep": 2,
    }
    seats = climate["heatVentSeat"]
    assert set(seats) == {
        "driverSeat",
        "passengerSeat",
        "rearLeftSeat",
        "rearRightSeat",
    }
    # heat_medium on the driver; the other seats are sent as off.
    assert seats["driverSeat"] != seats["passengerSeat"]
    assert seats["passengerSeat"] == seats["rearLeftSeat"] == seats["rearRightSeat"]


def test_expired_session_logs_in_with_rmtoken(settings, gvi):
    store.save_token(settings.token_file, make_token(hours=-1))
    api = KiaApi(3, 1, "en")
    fake = FakeKia(api, gvi)
    client = KiaClient(settings, api=api)
    client.ensure_session()
    assert [(m, p) for m, p, _ in fake.requests()] == [
        ("POST", "prof/authUser"),
        ("GET", "ownr/gvl"),
    ]
    headers = fake.calls[0][3]
    assert headers["rmtoken"] == "RM-1" and headers["deviceid"] == "DEV-1"
    assert client.token.access_token == "SID-2"
    assert client.token.refresh_token == "RM-1"
    assert client.vehicle.key == KEY
    assert store.load_token(settings.token_file, "you@example.com", "pw", None)


def test_otp_login_flow(settings, gvi):
    api = KiaApi(3, 1, "en")
    fake = FakeKia(api, gvi)
    fake.login_payload = {"otpKey": "OTP-KEY", "hasEmail": True, "hasPhone": False}
    client = KiaClient(settings, api=api)
    with pytest.raises(OtpRequired):
        client.ensure_session()
    assert client.otp_request.otp_key == "OTP-KEY"
    assert client.otp_request.request_id == "OTP-XID"

    client.send_otp()
    method, path, body, headers = fake.calls[-1]
    assert (method, path) == ("POST", "cmm/sendOTP")
    assert headers["otpkey"] == "OTP-KEY" and headers["xid"] == "OTP-XID"
    assert headers["notifytype"] == OTP_NOTIFY_TYPE[settings.otp_method].value

    fake.login_payload = None
    fake.calls.clear()
    client.verify_otp("123456")
    assert fake.requests()[0] == ("POST", "cmm/verifyOTP", {"otp": "123456"})
    assert fake.requests()[1][:2] == ("POST", "prof/authUser")
    assert fake.calls[1][3]["rmtoken"] == "RM-2"
    assert client.token.access_token == "SID-2"
    assert client.token.refresh_token == "RM-2"
