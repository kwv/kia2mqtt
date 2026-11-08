"""Thin layer over hyundai_kia_connect_api's Kia USA client.

KiaClient owns the session (token file), picks the vehicle, and exposes the
few calls the bridge makes. It is not thread-safe; the bridge calls it from a
single worker thread.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, cast

from hyundai_kia_connect_api.ApiImpl import OTPRequest
from hyundai_kia_connect_api.const import (
    ORDER_STATUS,
    OTP_NOTIFY_TYPE,
    VEHICLE_LOCK_ACTION,
)
from hyundai_kia_connect_api.KiaUvoApiUSA import KiaUvoApiUSA
from hyundai_kia_connect_api.Token import Token
from hyundai_kia_connect_api.Vehicle import Vehicle

from . import store
from .climate import to_options
from .config import Settings

_LOGGER = logging.getLogger(__name__)

REGION_USA = 3
BRAND_KIA = 1

# Seconds to wait for cmm/gts to report a command finished.
ACTION_TIMEOUT_S = 60


class OtpRequired(Exception):
    """Kia wants a one-time passcode before it will issue a session."""


class KiaApi(KiaUvoApiUSA):
    """KiaUvoApiUSA whose status request asks for location only when enabled.

    The library's cached-status request always sets vehicleInfoReq.location
    to "1"; this override sends "1" only if request_location is True.
    """

    request_location = False

    def _get_cached_vehicle_state(
        self, token: Token, vehicle: Vehicle
    ) -> dict[str, Any]:
        body = {
            "vehicleConfigReq": {
                "airTempRange": "0",
                "maintenance": "1",
                "seatHeatCoolOption": "0",
                "vehicle": "1",
                "vehicleFeature": "0",
            },
            "vehicleInfoReq": {
                "drivingActivty": "0",
                "dtc": "0",
                "enrollment": "0",
                "functionalCards": "0",
                "location": "1" if self.request_location else "0",
                "vehicleStatus": "1",
                "weather": "0",
            },
            "vinKey": [vehicle.key],
        }
        response = self.post_request_with_logging_and_active_session(
            token=token, url=self.API_URL + "cmm/gvi", json_body=body, vehicle=vehicle
        )
        return cast(dict[str, Any], response.json()["payload"]["vehicleInfoList"][0])


@dataclass
class ActionResult:
    status: str  # SUCCESS | TIMEOUT | PENDING | FAILED
    xid: str | None


class KiaClient:
    def __init__(self, settings: Settings, api: KiaApi | None = None) -> None:
        self.settings = settings
        self.api = api or KiaApi(REGION_USA, BRAND_KIA, "en")
        self.api.request_location = settings.location
        self.token: Token | None = store.load_token(
            settings.token_file,
            settings.kia_username,
            settings.kia_password,
            settings.kia_pin,
        )
        if self.token and self.token.device_id:
            # Keep the device id Kia trusted when the rmtoken was issued.
            self.api.device_id = self.token.device_id
        self.vehicle: Vehicle | None = None
        self.otp_request: OTPRequest | None = None

    # --- session -----------------------------------------------------------

    @property
    def has_session(self) -> bool:
        return self.token is not None and self.vehicle is not None

    def drop_session(self) -> None:
        """Forget the sid and vehicle key; the rmtoken is kept for re-login."""
        self.vehicle = None
        if self.token:
            self.token.valid_until = dt.datetime.min

    def _token_valid(self) -> bool:
        if not self.token or not self.token.access_token:
            return False
        until = self.token.valid_until
        if until.tzinfo is None:
            until = until.replace(tzinfo=dt.UTC)
        return until > dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)

    def ensure_session(self) -> None:
        """Log in if needed. Raises OtpRequired when Kia asks for a code."""
        if not self._token_valid():
            result = self.api.login(
                self.settings.kia_username,
                self.settings.kia_password,
                pin=self.settings.kia_pin,
                token=self.token,
            )
            if isinstance(result, OTPRequest):
                self.otp_request = result
                self.vehicle = None
                raise OtpRequired()
            self._set_token(result)
            self.vehicle = None
        if self.vehicle is None:
            # The vehicle key is tied to the session, so re-read it per login.
            self.vehicle = self._pick_vehicle(self.api.get_vehicles(self.token))

    def send_otp(self) -> None:
        assert self.otp_request is not None
        self.api.send_otp(self.otp_request, OTP_NOTIFY_TYPE[self.settings.otp_method])

    def verify_otp(self, code: str) -> None:
        assert self.otp_request is not None
        token = self.api.verify_otp_and_complete_login(
            self.settings.kia_username,
            self.settings.kia_password,
            code,
            self.otp_request,
            pin=self.settings.kia_pin,
        )
        self._set_token(token)
        self.otp_request = None
        self.vehicle = None

    def _set_token(self, token: Token) -> None:
        self.token = token
        self.save_token()

    def save_token(self) -> None:
        if self.token:
            store.save_token(self.settings.token_file, self.token)

    def _pick_vehicle(self, vehicles: list[Vehicle]) -> Vehicle:
        wanted = self.settings.kia_vehicle
        if wanted:
            matches = [v for v in vehicles if (v.name or "").lower() == wanted.lower()]
        else:
            matches = vehicles
        if len(matches) != 1:
            raise LookupError(
                f"{len(vehicles)} vehicles on the account, {len(matches)} match"
                " KIA_VEHICLE; set KIA_VEHICLE to the car's nickname"
            )
        return matches[0]

    # --- calls -------------------------------------------------------------

    def _ctx(self) -> tuple[Token, Vehicle]:
        assert self.token is not None and self.vehicle is not None
        return self.token, self.vehicle

    def fetch_status(self) -> tuple[dict[str, Any], int | None, int | None]:
        """Cached cmm/gvi (never wakes the car), plus evc/gts charge targets.

        Returns (vehicleInfoList[0], ac_target, dc_target).
        """
        token, vehicle = self._ctx()
        self.api.update_vehicle_with_cached_state(token, vehicle)
        return (
            vehicle.data or {},
            vehicle.ev_charge_limits_ac,
            vehicle.ev_charge_limits_dc,
        )

    def force_refresh(self) -> tuple[dict[str, Any], int | None, int | None]:
        """rems/rvs: asks the car itself to report (wakes it, uses its 12 V
        battery), then reads the cached status like fetch_status()."""
        token, vehicle = self._ctx()
        self.api.force_refresh_vehicle_state(token, vehicle)
        return (
            vehicle.data or {},
            vehicle.ev_charge_limits_ac,
            vehicle.ev_charge_limits_dc,
        )

    def charge(self, start: bool) -> ActionResult:
        token, vehicle = self._ctx()
        if start:
            return self._confirm(self.api.start_charge(token, vehicle))
        return self._confirm(self.api.stop_charge(token, vehicle))

    def _confirm(self, xid: str | None) -> ActionResult:
        if not xid:
            return ActionResult(status=ORDER_STATUS.FAILED.value, xid=None)
        token, vehicle = self._ctx()
        status = self.api.check_action_status(
            token, vehicle, xid, synchronous=True, timeout=ACTION_TIMEOUT_S
        )
        return ActionResult(status=status.value, xid=xid)

    def lock(self, lock: bool) -> ActionResult:
        token, vehicle = self._ctx()
        action = VEHICLE_LOCK_ACTION.LOCK if lock else VEHICLE_LOCK_ACTION.UNLOCK
        return self._confirm(self.api.lock_action(token, vehicle, action))

    def climate(self, request: dict[str, Any] | str) -> ActionResult:
        """request: "off", or a body that passed climate.validate()."""
        token, vehicle = self._ctx()
        if request == "off":
            xid = self.api.stop_climate(token, vehicle)
        else:
            assert isinstance(request, dict)
            xid = self.api.start_climate(token, vehicle, to_options(request))
        return self._confirm(xid)

    def set_charge_limits(self, ac: int, dc: int) -> ActionResult:
        token, vehicle = self._ctx()
        return self._confirm(self.api.set_charge_limits(token, vehicle, ac, dc))
