import datetime as dt
import json
import stat

import pytest
from hyundai_kia_connect_api.Token import Token

from kia2mqtt import config, store


def test_token_roundtrip_drops_credentials(tmp_path):
    path = tmp_path / "token.json"
    token = Token(
        username="you@example.com",
        password="secret",
        access_token="sid",
        refresh_token="rm",
        device_id="DEVICE",
        valid_until=dt.datetime(2030, 1, 1, tzinfo=dt.UTC),
        pin="1234",
    )
    store.save_token(path, token)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = json.loads(path.read_text())
    assert "password" not in raw and "username" not in raw and "pin" not in raw

    loaded = store.load_token(path, "you@example.com", "secret", "1234")
    assert loaded.refresh_token == "rm"
    assert loaded.device_id == "DEVICE"
    assert loaded.password == "secret"
    assert loaded.valid_until == token.valid_until


def test_missing_or_broken_token_file(tmp_path):
    path = tmp_path / "token.json"
    assert store.load_token(path, "u", "p", "") is None
    path.write_text("{not json")
    assert store.load_token(path, "u", "p", "") is None


BASE = {
    "CAR_NAME": "My-EV9",
    "KIA_USERNAME": "you@example.com",
    "KIA_PASSWORD": "pw",
    "MQTT_URL": "mqtt://user:p%40ss@broker.example",
}


def test_config_defaults():
    s = config.load(BASE)
    assert s.car_name == "my-ev9"
    assert s.poll_minutes == 30
    assert s.otp_method == "EMAIL"
    assert (s.topic_prefix, s.location, s.climate_debounce_s) == ("kia", False, 60)
    assert (s.mqtt.host, s.mqtt.port, s.mqtt.username, s.mqtt.password) == (
        "broker.example",
        1883,
        "user",
        "p@ss",
    )


@pytest.mark.parametrize(
    "override",
    [
        {"POLL_MINUTES": "4"},
        {"TOPIC_PREFIX": "kia/#"},
        {"KIA_LOCATION": "maybe"},
        {"CLIMATE_DEBOUNCE_S": "-1"},
        {"CAR_NAME": "my/ev9"},
        {"MQTT_URL": "http://broker"},
        {"KIA_OTP_METHOD": "PIGEON"},
        {"KIA_PASSWORD": ""},
    ],
)
def test_config_rejects(override):
    with pytest.raises(config.ConfigError):
        config.load({**BASE, **override})
