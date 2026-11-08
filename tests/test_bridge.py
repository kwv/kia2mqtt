import json
import time

import pytest
from hyundai_kia_connect_api.ApiImpl import OTPRequest

from kia2mqtt import store
from kia2mqtt.bridge import Bridge, Command
from kia2mqtt.kia import ActionResult, OtpRequired


class FakeKia:
    """Stands in for KiaClient; records calls."""

    def __init__(self, gvi, otp=False):
        self.gvi = gvi
        self.needs_otp = otp
        self.otp_request = None
        self.token = object()
        self.vehicle = None
        self.calls = []
        self.fail_fetch = False

    @property
    def has_session(self):
        return self.vehicle is not None

    def ensure_session(self):
        self.calls.append("login")
        if self.needs_otp:
            self.otp_request = OTPRequest("xid-1", "key-1", True, False, None, None)
            raise OtpRequired()
        self.vehicle = "car"

    def drop_session(self):
        self.vehicle = None

    def send_otp(self):
        self.calls.append("send_otp")

    def verify_otp(self, code):
        self.calls.append(("verify", code, self.otp_request.otp_key))
        if code != "123456":
            raise RuntimeError("bad code")
        self.needs_otp = False
        self.otp_request = None

    def save_token(self):
        pass

    def fetch_status(self):
        self.calls.append("gvi")
        if self.fail_fetch:
            raise RuntimeError("Kia down")
        return self.gvi, None, None

    def lock(self, lock):
        self.calls.append(("lock", lock))
        return ActionResult("SUCCESS", "x")

    def climate(self, request):
        self.calls.append(("climate", json.dumps(request)))
        return ActionResult("SUCCESS", "x")

    def charge(self, start):
        self.calls.append(("charge", start))
        return ActionResult("SUCCESS", "x")

    def force_refresh(self):
        self.calls.append("rvs")
        return self.gvi, None, None

    def set_charge_limits(self, ac, dc):
        self.calls.append(("charge_limit", ac, dc))
        return ActionResult("TIMEOUT", "x")


class Recorder:
    def __init__(self):
        self.messages = []

    def __call__(self, topic, payload, retain):
        self.messages.append((topic, payload, retain))

    def last(self, topic):
        for t, payload, retain in reversed(self.messages):
            if t == topic:
                return payload, retain
        return None

    def count(self, topic):
        return sum(1 for t, _, _ in self.messages if t == topic)


@pytest.fixture
def make(settings, gvi):
    def _make(otp=False, **overrides):
        kia = FakeKia(gvi, otp=otp)
        rec = Recorder()
        bridge = Bridge(settings.__class__(**{**vars(settings), **overrides}), kia, rec)
        return bridge, kia, rec

    return _make


def started(make, **kw):
    bridge, kia, rec = make(**kw)
    bridge._start_session()
    return bridge, kia, rec


def result(rec):
    payload, retain = rec.last("kia/testcar/result")
    assert retain is False
    return json.loads(payload)


def test_poll_publishes_retained_status(make):
    bridge, kia, rec = started(make)
    assert bridge.poll()
    assert rec.last("kia/testcar/status/soc") == ("66", True)
    assert rec.last("kia/testcar/status/updated") == ("2026-11-08T13:41:47Z", True)
    assert rec.last("kia/testcar/auth/state") == ("ok", True)
    assert rec.last("kia/testcar/bridge/last_poll")[1] is True


def test_unchanged_values_not_republished_but_replayed_on_reconnect(make):
    bridge, kia, rec = started(make)
    bridge.poll()
    bridge.poll()
    assert rec.count("kia/testcar/status/soc") == 1
    rec.messages.clear()
    bridge.on_mqtt_connect()
    assert rec.last("kia/testcar/bridge/online") == ("true", True)
    assert rec.last("kia/testcar/status/soc") == ("66", True)


def test_field_that_disappears_is_cleared(make, gvi):
    bridge, kia, rec = started(make)
    bridge.poll()
    del kia.gvi["lastVehicleInfo"]["vehicleStatusRpt"]["vehicleStatus"]["doorLock"]
    bridge.poll()
    assert rec.last("kia/testcar/status/locked") == ("", True)
    rec.messages.clear()
    bridge.on_mqtt_connect()
    assert rec.last("kia/testcar/status/locked") is None


def test_failed_poll_keeps_values_and_reports_error(make):
    bridge, kia, rec = started(make)
    bridge.poll()
    kia.fail_fetch = True
    assert not bridge.poll()
    assert json.loads(rec.last("kia/testcar/error")[0])["type"] == "poll"
    assert rec.count("kia/testcar/status/soc") == 1
    # It logged in again once, but did not poll again.
    assert kia.calls.count("gvi") == 2


def test_retained_commands_are_ignored(make):
    bridge, kia, rec = started(make)
    bridge.on_mqtt_message("kia/testcar/lock/set", b"UNLOCK", retained=True)
    assert bridge.commands.empty()


def test_bad_payload_rejected(make):
    bridge, kia, rec = started(make)
    bridge.on_mqtt_message("kia/testcar/lock/set", b"OPEN", retained=False)
    assert bridge.commands.empty()
    assert result(rec)["status"] == "REJECTED"


def test_unlock_rejected_by_default(make):
    bridge, kia, rec = started(make)
    bridge.on_mqtt_message("kia/testcar/lock/set", b"UNLOCK", retained=False)
    bridge.handle(bridge.commands.get_nowait())
    assert not [c for c in kia.calls if c[0] == "lock"]
    body = result(rec)
    assert body["status"] == "REJECTED"
    assert "ALLOW_UNLOCK" in body["error"]


def test_lock_allowed_without_unlock_setting(make):
    bridge, kia, rec = started(make)
    bridge.on_mqtt_message("kia/testcar/lock/set", b"LOCK", retained=False)
    bridge.handle(bridge.commands.get_nowait())
    assert kia.calls.count(("lock", True)) == 1
    assert result(rec)["status"] == "SUCCESS"


def test_lock_command_sent_once_and_confirmed(make):
    bridge, kia, rec = started(make, allow_unlock=True)
    bridge.on_mqtt_message("kia/testcar/lock/set", b"unlock", retained=False)
    bridge.handle(bridge.commands.get_nowait())
    assert kia.calls.count(("lock", False)) == 1
    body = result(rec)
    assert body["status"] == "SUCCESS"
    assert body["state"]["locked"] == "true"  # the car's report, not optimistic
    assert "gvi" in kia.calls


def test_climate_debounce(make):
    bridge, kia, rec = started(make)
    bridge.handle(Command("climate", {"temp": 72, "duration_min": 10}))
    bridge.handle(Command("climate", "off"))
    assert len([c for c in kia.calls if c[0] == "climate"]) == 1
    assert ("climate", '"off"') not in kia.calls
    assert result(rec)["status"] == "REJECTED"


def test_expired_command_dropped(make):
    bridge, kia, rec = started(make)
    bridge.handle(Command("lock", "LOCK", received=time.monotonic() - 600))
    assert ("lock", True) not in kia.calls


def test_charge_limit_fills_other_side_from_status(make):
    bridge, kia, rec = started(make)
    bridge.poll()
    bridge.on_mqtt_message("kia/testcar/charge_limit/set", b'{"ac": 100}', False)
    bridge.handle(bridge.commands.get_nowait())
    assert ("charge_limit", 100, 100) in kia.calls
    assert result(rec)["status"] == "TIMEOUT"


@pytest.mark.parametrize(
    "payload", [b'{"ac": 101}', b'{"ac": 40}', b"100", b'{"ac": "90"}']
)
def test_charge_limit_validation(make, payload):
    bridge, kia, rec = started(make)
    bridge.on_mqtt_message("kia/testcar/charge_limit/set", payload, False)
    assert bridge.commands.empty()


def test_charge_limit_needs_known_current_value(make):
    bridge, kia, rec = started(make)  # no poll yet
    bridge.handle(Command("charge_limit", {"ac": 100}))
    assert result(rec)["status"] == "REJECTED"


def test_otp_flow_sends_once_pauses_polling_and_resumes(make, settings):
    bridge, kia, rec = started(make, otp=True)
    assert kia.calls.count("send_otp") == 1
    assert json.loads(rec.last("kia/testcar/auth/otp_required")[0])["sent"] is True
    assert rec.last("kia/testcar/auth/state") == ("otp_required", True)
    assert not bridge.poll_due()
    pending = store.read_json(settings.otp_file)
    assert pending["otp_key"] == "key-1"

    bridge.handle(Command("lock", "LOCK"))
    assert result(rec)["status"] == "REJECTED"

    bridge.handle(Command("otp", "000000"))
    assert result(rec)["status"] == "FAILED"
    assert kia.calls.count("send_otp") == 1  # a wrong code never resends

    bridge.handle(Command("otp", "123456"))
    body = result(rec)
    assert body["status"] == "SUCCESS" and "value" not in body
    assert bridge.auth_state == "ok" and bridge.poll_due()
    assert store.read_json(settings.otp_file) is None


def test_restart_while_otp_pending_does_not_resend(make, settings):
    store.write_private_json(
        settings.otp_file,
        {
            "otp_key": "old-key",
            "request_id": "old-xid",
            "sent_at": "2026-11-08T12:00:00Z",
        },
    )
    bridge, kia, rec = started(make, otp=True)
    assert "send_otp" not in kia.calls
    assert json.loads(rec.last("kia/testcar/auth/otp_required")[0])["sent"] is False
    bridge.handle(Command("otp", "123456"))
    assert ("verify", "123456", "old-key") in kia.calls


def test_otp_resend_on_request_is_rate_limited(make):
    bridge, kia, rec = started(make, otp=True)
    bridge.handle(Command("otp_request", None))
    assert result(rec)["status"] == "REJECTED"
    assert kia.calls.count("send_otp") == 1


def test_restart_soon_after_poll_delays_first_poll(make, settings):
    from kia2mqtt.bridge import iso, utc_now

    store.write_private_json(settings.state_file, {"last_poll_attempt": iso(utc_now())})
    bridge, kia, rec = started(make)
    assert not bridge.poll_due()


def test_error_messages_are_redacted():
    from kia2mqtt.bridge import redact

    msg = redact('APIError: Response: {"userId": "someone@example.org"}' + "x" * 500)
    assert "someone@example.org" not in msg and "<email>" in msg
    assert len(msg) <= 300


def test_charge_start_stop(make):
    bridge, kia, rec = started(make)
    bridge.on_mqtt_message("kia/testcar/charge/set", b"stop", False)
    bridge.handle(bridge.commands.get_nowait())
    assert ("charge", False) in kia.calls
    assert result(rec)["state"]["charging"] == "true"


def test_refresh_only_on_request(make):
    bridge, kia, rec = started(make)
    bridge.poll()
    assert "rvs" not in kia.calls
    bridge.on_mqtt_message("kia/testcar/refresh/set", b"", False)
    bridge.handle(bridge.commands.get_nowait())
    assert kia.calls.count("rvs") == 1
    assert result(rec)["status"] == "SUCCESS"


def test_topic_prefix_and_location_settings(settings, gvi):
    custom = settings.__class__(
        **{**vars(settings), "topic_prefix": "cars/kia", "location": True}
    )
    gvi["lastVehicleInfo"]["location"] = {"coord": {"lat": 12.3, "lon": 45.6}}
    rec = Recorder()
    bridge = Bridge(custom, FakeKia(gvi), rec)
    bridge._start_session()
    bridge.poll()
    assert rec.last("cars/kia/testcar/status/latitude") == ("12.3", True)
    assert "cars/kia/testcar/lock/set" in bridge.topics.inbound


def test_location_never_published_by_default(make, gvi):
    gvi["lastVehicleInfo"]["location"] = {"coord": {"lat": 12.3, "lon": 45.6}}
    bridge, kia, rec = started(make)
    bridge.poll()
    assert not [t for t, _, _ in rec.messages if "latitude" in t]
