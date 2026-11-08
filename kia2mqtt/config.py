"""Settings from environment variables."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

# Kia rate-limits accounts; polling faster than this risks a lockout.
MIN_POLL_MINUTES = 5


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class MqttSettings:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    tls: bool = False


@dataclass(frozen=True)
class Settings:
    car_name: str
    topic_prefix: str
    kia_username: str
    kia_password: str
    kia_pin: str
    kia_vehicle: str | None
    otp_method: str
    mqtt: MqttSettings
    data_dir: Path
    poll_minutes: int
    climate_debounce_s: int
    location: bool
    health_port: int
    log_level: str

    @property
    def token_file(self) -> Path:
        return self.data_dir / "token.json"

    @property
    def otp_file(self) -> Path:
        return self.data_dir / "otp.json"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"


def parse_mqtt_url(url: str) -> MqttSettings:
    """mqtt://user:pass@host:1883 or mqtts://host:8883."""
    parsed = urlparse(url)
    if parsed.scheme not in ("mqtt", "mqtts") or not parsed.hostname:
        raise ConfigError("MQTT_URL must look like mqtt://[user:pass@]host[:port]")
    tls = parsed.scheme == "mqtts"
    return MqttSettings(
        host=parsed.hostname,
        port=parsed.port or (8883 if tls else 1883),
        username=unquote(parsed.username) if parsed.username else None,
        password=unquote(parsed.password) if parsed.password else None,
        tls=tls,
    )


def _required(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _bool(env: dict[str, str], name: str) -> bool:
    value = env.get(name, "false").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("", "0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false")


def load(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)

    car_name = _required(env, "CAR_NAME").lower()
    if not re.fullmatch(r"[a-z0-9_-]+", car_name):
        raise ConfigError("CAR_NAME may only contain a-z, 0-9, '_' and '-'")

    otp_method = env.get("KIA_OTP_METHOD", "EMAIL").strip().upper()
    if otp_method not in ("EMAIL", "SMS"):
        raise ConfigError("KIA_OTP_METHOD must be EMAIL or SMS")

    topic_prefix = env.get("TOPIC_PREFIX", "kia").strip().strip("/")
    if not topic_prefix or any(c in topic_prefix for c in "+#"):
        raise ConfigError("TOPIC_PREFIX must be a non-empty topic without + or #")

    try:
        poll_minutes = int(env.get("POLL_MINUTES", "30"))
        climate_debounce_s = int(env.get("CLIMATE_DEBOUNCE_S", "60"))
        health_port = int(env.get("HEALTH_PORT", "8080"))
    except ValueError as err:
        raise ConfigError(f"expected an integer: {err}")
    if climate_debounce_s < 0:
        raise ConfigError("CLIMATE_DEBOUNCE_S must be 0 or more")
    if poll_minutes < MIN_POLL_MINUTES:
        raise ConfigError(f"POLL_MINUTES must be at least {MIN_POLL_MINUTES}")

    return Settings(
        car_name=car_name,
        topic_prefix=topic_prefix,
        kia_username=_required(env, "KIA_USERNAME"),
        kia_password=_required(env, "KIA_PASSWORD"),
        kia_pin=env.get("KIA_PIN", "").strip(),
        kia_vehicle=env.get("KIA_VEHICLE", "").strip() or None,
        otp_method=otp_method,
        mqtt=parse_mqtt_url(_required(env, "MQTT_URL")),
        data_dir=Path(env.get("DATA_DIR", "/data")),
        poll_minutes=poll_minutes,
        climate_debounce_s=climate_debounce_s,
        location=_bool(env, "KIA_LOCATION"),
        health_port=health_port,
        log_level=env.get("LOG_LEVEL", "INFO").upper(),
    )
