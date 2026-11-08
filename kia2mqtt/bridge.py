"""MQTT side of the bridge. Topic contract: see README.md.

Every Kia call runs on one worker thread (run()). MQTT callbacks only parse
messages and queue them, so commands, polls and OTP handling never overlap.
"""

import datetime as dt
import json
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from hyundai_kia_connect_api.ApiImpl import OTPRequest

from . import climate, store
from .config import Settings
from .kia import KiaClient, OtpRequired
from .status import FIELDS, LOCATION_FIELDS, parse_status

_LOGGER = logging.getLogger(__name__)

# Commands that waited longer than this in the queue are dropped, not sent.
COMMAND_MAX_AGE_S = 120
# A user-requested OTP resend is refused if one was sent this recently.
OTP_RESEND_MIN_S = 60
# After a restart, wait at least this long since the last poll.
MIN_POLL_GAP_S = 10 * 60
# Charge limits the API accepts (chargeFeature minTargetSOC/maxTargetSOC).
# Some cars only take 10 % steps; they reject other values themselves.
CHARGE_LIMIT_VALUES = range(50, 101)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Command:
    kind: str  # lock | climate | charge | charge_limit | refresh | otp | otp_request
    value: Any
    received: float = field(default_factory=time.monotonic)


class Topics:
    def __init__(self, prefix: str, name: str) -> None:
        base = f"{prefix}/{name}"
        self.base = base
        self.status = f"{base}/status"
        self.result = f"{base}/result"
        self.error = f"{base}/error"
        self.online = f"{base}/bridge/online"
        self.last_poll = f"{base}/bridge/last_poll"
        self.auth_state = f"{base}/auth/state"
        self.otp_required = f"{base}/auth/otp_required"
        self.otp_set = f"{base}/auth/otp/set"
        self.otp_request = f"{base}/auth/otp/request"
        self.lock_set = f"{base}/lock/set"
        self.climate_set = f"{base}/climate/set"
        self.charge_set = f"{base}/charge/set"
        self.charge_limit_set = f"{base}/charge_limit/set"
        self.refresh_set = f"{base}/refresh/set"

    def status_field(self, name: str) -> str:
        return f"{self.status}/{name}"

    @property
    def inbound(self) -> list[str]:
        return [
            self.lock_set,
            self.climate_set,
            self.charge_set,
            self.charge_limit_set,
            self.refresh_set,
            self.otp_set,
            self.otp_request,
        ]


def parse_command(topics: Topics, topic: str, payload: str) -> Command | str:
    """Returns a Command, or a reason string if the message is rejected."""
    text = payload.strip()
    if topic == topics.lock_set:
        if text.upper() not in ("LOCK", "UNLOCK"):
            return "lock payload must be LOCK or UNLOCK"
        return Command("lock", text.upper())
    if topic == topics.climate_set:
        if text.lower() == "off":
            return Command("climate", "off")
        try:
            body = json.loads(text)
        except ValueError:
            return 'climate payload must be "off" or a JSON object'
        error = climate.validate(body)
        return error if error else Command("climate", body)
    if topic == topics.charge_set:
        if text.upper() not in ("START", "STOP"):
            return "charge payload must be START or STOP"
        return Command("charge", text.upper())
    if topic == topics.refresh_set:
        return Command("refresh", None)
    if topic == topics.charge_limit_set:
        try:
            body = json.loads(text)
        except ValueError:
            return 'charge_limit payload must be JSON like {"ac": 100}'
        if not isinstance(body, dict) or not ({"ac", "dc"} & body.keys()):
            return 'charge_limit payload must be JSON like {"ac": 100}'
        limits = {}
        for key in ("ac", "dc"):
            if key in body:
                level = body[key]
                if type(level) is not int or level not in CHARGE_LIMIT_VALUES:
                    return f"{key} must be an integer from 50 to 100"
                limits[key] = level
        return Command("charge_limit", limits)
    if topic == topics.otp_set:
        if not text.isdigit():
            return "OTP code must be digits"
        return Command("otp", text)
    if topic == topics.otp_request:
        return Command("otp_request", None)
    return f"unknown topic {topic}"


class Bridge:
    def __init__(
        self,
        settings: Settings,
        client: KiaClient,
        publish: Callable[[str, str, bool], None],
    ) -> None:
        """publish(topic, payload, retain) sends one MQTT message."""
        self.settings = settings
        self.kia = client
        self._publish = publish
        self.topics = Topics(settings.topic_prefix, settings.car_name)
        self.fields = FIELDS + (LOCATION_FIELDS if settings.location else ())
        self.commands: queue.Queue[Command] = queue.Queue()
        self.stop_event = threading.Event()

        # Retained messages, replayed on every MQTT (re)connect.
        self.retained: dict[str, str] = {}
        self.status: dict[str, str] = {}
        self.auth_state = "starting"
        self.last_poll_ok: dt.datetime | None = None
        self.last_error: str | None = None
        self.mqtt_connected = False
        self._last_climate = -float("inf")
        self._otp_sent_at: dt.datetime | None = None
        self._next_poll = time.monotonic()

        state = store.read_json(settings.state_file) or {}
        last = _parse_iso(state.get("last_poll_attempt"))
        if last:
            wait = MIN_POLL_GAP_S - (utc_now() - last).total_seconds()
            if wait > 0:
                _LOGGER.info("Last poll was recent; first poll in %d s", wait)
                self._next_poll += wait

    # --- publishing ----------------------------------------------------------

    def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        if retain:
            if payload:
                self.retained[topic] = payload
            else:
                self.retained.pop(topic, None)
        self._publish(topic, payload, retain)

    def publish_event(self, topic: str, body: dict[str, Any]) -> None:
        self.publish(topic, json.dumps({**body, "at": iso(utc_now())}))

    def on_mqtt_connect(self) -> None:
        self.mqtt_connected = True
        self._publish(self.topics.online, "true", True)
        for topic, payload in list(self.retained.items()):
            self._publish(topic, payload, True)

    def on_mqtt_message(self, topic: str, payload: bytes, retained: bool) -> None:
        if retained:
            _LOGGER.warning("Ignoring retained message on %s", topic)
            return
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        parsed = parse_command(self.topics, topic, text)
        if isinstance(parsed, str):
            self.publish_event(
                self.topics.result,
                {
                    "command": topic.removeprefix(self.topics.base + "/"),
                    "status": "REJECTED",
                    "error": parsed,
                },
            )
            return
        self.commands.put(parsed)

    def _set_auth_state(self, state: str) -> None:
        self.auth_state = state
        self.publish(self.topics.auth_state, state, retain=True)

    def _error(self, kind: str, err: BaseException | str) -> None:
        message = err if isinstance(err, str) else f"{type(err).__name__}: {err}"
        message = redact(message)
        self.last_error = message
        _LOGGER.error("%s error: %s", kind, message)
        self.publish_event(self.topics.error, {"type": kind, "error": message})

    # --- main loop -----------------------------------------------------------

    def run(self) -> None:
        self._start_session()
        while not self.stop_event.is_set():
            try:
                command = self.commands.get(timeout=1.0)
            except queue.Empty:
                command = None
            if command:
                self.handle(command)
            elif self.poll_due():
                self.poll()

    def poll_due(self) -> bool:
        """Polling pauses while an OTP is pending."""
        return (
            self.auth_state in ("ok", "error") and time.monotonic() >= self._next_poll
        )

    def _start_session(self) -> bool:
        """Log in if needed. Returns True when the session is usable."""
        try:
            self.kia.ensure_session()
        except OtpRequired:
            self._otp_required()
            return False
        except Exception as err:  # network, Kia outage, bad credentials
            self._set_auth_state("error")
            self._error("auth", err)
            self._schedule_poll()
            return False
        store.delete(self.settings.otp_file)
        self._set_auth_state("ok")
        return True

    def _schedule_poll(self) -> None:
        self._next_poll = time.monotonic() + self.settings.poll_minutes * 60

    def poll(self, fetch: Callable[[], tuple] | None = None) -> bool:
        """One status read (cached unless fetch says otherwise).
        Returns True if status was published."""
        fetch = fetch or self.kia.fetch_status
        self._schedule_poll()
        store.write_private_json(
            self.settings.state_file, {"last_poll_attempt": iso(utc_now())}
        )
        if not self.kia.has_session and not self._start_session():
            return False
        try:
            info, ac, dc = fetch()
        except OtpRequired:
            self._otp_required()
            return False
        except Exception as err:
            # The library re-logs in on an invalid session itself. If the call
            # still fails, log in again once now (that surfaces an OTP request
            # straight away); the poll itself waits for the next interval.
            self.kia.drop_session()
            self._error("poll", err)
            self._start_session()
            return False
        self.kia.save_token()
        self._publish_status(
            parse_status(info, ac, dc, location=self.settings.location)
        )
        self.last_poll_ok = utc_now()
        self.last_error = None
        self.publish(self.topics.last_poll, iso(self.last_poll_ok), retain=True)
        return True

    def _publish_status(self, status: dict[str, str]) -> None:
        for name in self.fields:
            topic = self.topics.status_field(name)
            if name in status:
                if self.status.get(name) != status[name] or topic not in self.retained:
                    self.publish(topic, status[name], retain=True)
            elif name in self.status or topic in self.retained:
                # No longer reported: clear the retained value (unknown).
                self.publish(topic, "", retain=True)
        self.status = dict(status)

    # --- OTP -----------------------------------------------------------------

    def _otp_required(self) -> None:
        """Kia wants a code. Send it at most once; wait for kia/.../otp/set."""
        self._set_auth_state("otp_required")
        pending = store.read_json(self.settings.otp_file) or {}
        sent_at = _parse_iso(pending.get("sent_at"))
        if sent_at and pending.get("otp_key"):
            # A code was sent before a restart. Verify against that request
            # and don't send another; kia/{name}/auth/otp/request resends.
            self.kia.otp_request = _restore_otp_request(pending)
            self._otp_sent_at = sent_at
            self.publish_event(
                self.topics.otp_required,
                {
                    "sent": False,
                    "reason": "already sent",
                    "sent_at": iso(sent_at),
                    "method": self.settings.otp_method,
                },
            )
            return
        self._send_otp()

    def _send_otp(self) -> None:
        req = self.kia.otp_request
        if req is None:
            self._error("auth", "no OTP request to send")
            return
        try:
            self.kia.send_otp()
        except Exception as err:
            self._error("auth", err)
            self.publish_event(
                self.topics.otp_required, {"sent": False, "reason": "send failed"}
            )
            return
        self._otp_sent_at = utc_now()
        store.write_private_json(
            self.settings.otp_file,
            {
                "otp_key": req.otp_key,
                "request_id": req.request_id,
                "sent_at": iso(self._otp_sent_at),
            },
        )
        self.publish_event(
            self.topics.otp_required, {"sent": True, "method": self.settings.otp_method}
        )

    def _handle_otp_code(self, code: str) -> dict[str, Any]:
        if self.auth_state != "otp_required" or self.kia.otp_request is None:
            return {"status": "REJECTED", "error": "no OTP pending"}
        try:
            self.kia.verify_otp(code)
        except Exception as err:
            self._error("auth", err)
            return {"status": "FAILED", "error": redact(str(err))}
        store.delete(self.settings.otp_file)
        self._otp_sent_at = None
        if self._start_session():
            self._next_poll = time.monotonic()
            return {"status": "SUCCESS"}
        return {"status": "FAILED", "error": self.last_error}

    def _handle_otp_request(self) -> dict[str, Any]:
        """User asked for a new code (e.g. the first one expired)."""
        if self.auth_state != "otp_required":
            return {"status": "REJECTED", "error": "no OTP pending"}
        if self._otp_sent_at and (
            (utc_now() - self._otp_sent_at).total_seconds() < OTP_RESEND_MIN_S
        ):
            return {"status": "REJECTED", "error": "OTP sent less than a minute ago"}
        # A fresh login gives a fresh OTP request; the old one may have expired.
        store.delete(self.settings.otp_file)
        self.kia.otp_request = None
        self.kia.token = None
        try:
            self.kia.ensure_session()
        except OtpRequired:
            self._send_otp()
            return {"status": "SUCCESS" if self.kia.otp_request else "FAILED"}
        except Exception as err:
            self._error("auth", err)
            return {"status": "FAILED", "error": redact(str(err))}
        self._set_auth_state("ok")
        return {"status": "SUCCESS", "note": "logged in without OTP"}

    # --- commands ------------------------------------------------------------

    def handle(self, command: Command) -> None:
        body: dict[str, Any] = {"command": command.kind}
        if command.kind == "otp":
            outcome = self._handle_otp_code(command.value)  # never echo the code
        elif command.kind == "otp_request":
            outcome = self._handle_otp_request()
        else:
            body["value"] = command.value
            outcome = self._run_vehicle_command(command)
        self.publish_event(self.topics.result, {**body, **outcome})

    def _run_vehicle_command(self, command: Command) -> dict[str, Any]:
        if time.monotonic() - command.received > COMMAND_MAX_AGE_S:
            return {"status": "REJECTED", "error": "command expired in queue"}
        if self.auth_state != "ok" or not self.kia.has_session:
            return {"status": "REJECTED", "error": f"not logged in ({self.auth_state})"}

        if command.kind == "climate":
            since = time.monotonic() - self._last_climate
            if since < self.settings.climate_debounce_s:
                return {
                    "status": "REJECTED",
                    "error": "climate debounce (CLIMATE_DEBOUNCE_S)",
                }
            self._last_climate = time.monotonic()

        if command.kind == "charge_limit":
            limits = dict(command.value)
            for key in ("ac", "dc"):
                if key not in limits:
                    current = self.status.get(f"target_soc_{key}")
                    if current is None:
                        return {
                            "status": "REJECTED",
                            "error": f"current {key} limit unknown; send both",
                        }
                    limits[key] = int(current)
            command.value = limits

        if command.kind == "refresh":
            ok = self.poll(fetch=self.kia.force_refresh)
            if ok:
                return {"status": "SUCCESS", "state": dict(self.status)}
            return {"status": "ERROR", "error": self.last_error}

        try:
            if command.kind == "lock":
                result = self.kia.lock(command.value == "LOCK")
            elif command.kind == "charge":
                result = self.kia.charge(command.value == "START")
            elif command.kind == "climate":
                result = self.kia.climate(command.value)
            else:
                result = self.kia.set_charge_limits(
                    command.value["ac"], command.value["dc"]
                )
        except Exception as err:
            # Sent once; never retried here.
            self._error("command", err)
            return {
                "status": "ERROR",
                "value": command.value,
                "error": redact(str(err)),
            }

        # Report the confirmed state from the next status read.
        self.poll()
        return {
            "status": result.status,
            "value": command.value,
            "state": {
                k: self.status[k]
                for k in _RESULT_FIELDS[command.kind]
                if k in self.status
            },
        }

    def health(self) -> dict[str, Any]:
        return {
            "mqtt_connected": self.mqtt_connected,
            "auth": self.auth_state,
            "last_poll_ok": iso(self.last_poll_ok) if self.last_poll_ok else None,
            "last_error": self.last_error,
        }


_RESULT_FIELDS = {
    "lock": ("locked", "updated"),
    "climate": ("climate", "updated"),
    "charge": ("charging", "plugged", "power_kw", "updated"),
    "charge_limit": ("target_soc_ac", "target_soc_dc", "updated"),
}


_EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(\.[\w-]+)+")


def redact(message: str) -> str:
    """Library errors can embed raw Kia responses (account email etc.)."""
    return _EMAIL.sub("<email>", message)[:300]


def _restore_otp_request(pending: dict[str, Any]) -> OTPRequest:
    return OTPRequest(
        request_id=pending.get("request_id"),
        otp_key=pending.get("otp_key"),
        has_email=None,
        has_sms=None,
        email=None,
        sms=None,
    )


def _parse_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
