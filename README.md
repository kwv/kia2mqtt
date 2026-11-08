# kia2mqtt

A bridge between the **Kia Connect (US)** API and MQTT, built on the community
[`hyundai_kia_connect_api`](https://github.com/Hyundai-Kia-Connect/hyundai_kia_connect_api)
library (MIT, pinned to an exact version).

It publishes the car's status and accepts commands over MQTT, and nothing more:
- no automations
- no presets
- no schedules

Whatever consumes the topics (Node-RED, Home Assistant, your own scripts)
decides what to do with them.

**Supported:** Kia vehicles on US Kia Connect accounts. Other regions and the
Hyundai/Genesis brands use different login flows and payloads in the library.
They aren't supported yet, and contributions are welcome.

## Behaviour
- Polls the **cached** status (`cmm/gvi`) every `POLL_MINUTES` (default 30). The car is only woken when you send `refresh/set`.
- Location is **off** unless `KIA_LOCATION=true`. When it's off, the bridge doesn't even request it from Kia.
- Each command is sent **once**, confirmed via `cmm/gts`, and reported. It is never retried.
- Unknown is never `false` or `0`. A value Kia doesn't report is not published, and a value that stops being reported is cleared (empty retained payload).

## MQTT contract

`{base}` is `{TOPIC_PREFIX}/{CAR_NAME}`, e.g. `kia/my-ev9`. All timestamps are
UTC ISO 8601 (`2026-11-08T13:41:47Z`).

### Status (retained)

`{base}/status/{field}`, one plain-text value per topic. Values are
re-published on every MQTT reconnect, so a broker without persistence still
gets them back.

| Field | Payload | Source (`gvi`) |
|---|---|---|
| `soc` | `66` (%) | `evStatus.batteryStatus` |
| `charging` | `true` / `false` | `evStatus.batteryCharge` |
| `plugged` | `true` / `false` | `evStatus.batteryPlugin` ≠ 0 |
| `power_kw` | `1.4` | `evStatus.realTimePower` |
| `remain_min` | `650` | `evStatus.remainChargeTime[0].timeInterval` (minutes to target) |
| `range` | `220.59` | `evStatus.drvDistance[0]…totalAvailableRange`, else `distanceToEmpty` |
| `odometer` | `13259` | `vehicleConfig.vehicleDetail.vehicle.mileage` |
| `distance_unit` | `mi` | unit of `range` and `odometer` (always `mi` on US accounts) |
| `locked` | `true` / `false` | `doorLock` |
| `climate` | `true` / `false` | `climate.airCtrl` |
| `target_soc_ac` | `80` | `evStatus.targetSOC` plugType 1, else `evc/gts` |
| `target_soc_dc` | `100` | `evStatus.targetSOC` plugType 0, else `evc/gts` |
| `updated` | ISO time | `syncDate.utc`: when the **car** last reported to Kia |
| `latitude`, `longitude` | `12.345` | `location.coord`. Only with `KIA_LOCATION=true` |
| `location_updated` | ISO time | `location.syncDate.utc`. Only with `KIA_LOCATION=true` |

`updated` is the age of the data. A poll can succeed and still return an old
report, so judge staleness from `updated`, not from when the message arrived.

### Bridge health and auth (retained)

| Topic | Payload |
|---|---|
| `{base}/bridge/online` | `true`; `false` via MQTT last will or on shutdown |
| `{base}/bridge/last_poll` | ISO time of the last **successful** poll |
| `{base}/auth/state` | `ok` / `otp_required` / `error` |

HTTP `GET :8080/health` returns 200 while MQTT is connected (503 otherwise),
with a JSON body: `mqtt_connected`, `auth`, `last_poll_ok`, `last_error`.
For a "bridge went quiet" alert, watch `bridge/online`, or `bridge/last_poll`
going older than a couple of poll intervals.

### Events (not retained)

All are JSON with an `at` timestamp.

| Topic | When | Body |
|---|---|---|
| `{base}/result` | after every command | `{"command", "value", "status", "state", "error"?}` |
| `{base}/auth/otp_required` | Kia asks for a one-time passcode | `{"sent": true, "method": "EMAIL"}`, or `{"sent": false, "reason": …}` |
| `{base}/error` | a poll, login or command fails | `{"type": "poll" \| "auth" \| "command", "error": "…"}` |

`status` in a result:
- `SUCCESS`: Kia reports the car finished it.
- `TIMEOUT`: no answer within 60 s. The outcome is unknown, so check the next status.
- `FAILED`: Kia returned no transaction id.
- `ERROR`: the request raised an error.
- `REJECTED`: the bridge refused it and sent nothing (bad payload, debounce, not logged in, or expired in the queue).

`state` holds the related status fields from the read that follows the command,
i.e. what the car reports, not an optimistic guess. OTP results never echo the code.

### Commands

Retained messages on these topics are **ignored**, so a command can never replay
on reconnect. The bridge uses a clean MQTT session, so commands sent while it
is offline are dropped, not queued. Commands that wait more than 2 minutes in
the bridge's own queue are dropped too.

| Topic | Payload | Kia call |
|---|---|---|
| `{base}/lock/set` | `LOCK` / `UNLOCK` | `rems/door/lock` / `unlock` |
| `{base}/climate/set` | climate JSON (below) / `off` | `rems/start` / `rems/stop` |
| `{base}/charge/set` | `START` / `STOP` | `evc/charge` / `evc/cancel` |
| `{base}/charge_limit/set` | `{"ac": 90}`, `{"dc": 80}` or both | `evc/sts` |
| `{base}/refresh/set` | anything | `rems/rvs`: **wakes the car** to report fresh status (uses its 12 V battery) |
| `{base}/auth/otp/set` | the code, e.g. `123456` | `cmm/verifyOTP` |
| `{base}/auth/otp/request` | anything | log in again and send a fresh code |

- **Charge limit:** integers from 50 to 100. Some cars accept only 10 % steps and reject other values (reported in `result`). A side that is left out keeps the car's current value. If that value is unknown, the request is rejected.
- **Climate:** commands closer together than `CLIMATE_DEBOUNCE_S` (default 60, `0` = off) are rejected.

### Climate request

```json
{"temp": 72, "temp_unit": "F", "duration_min": 10, "defrost": true,
 "rear_window_heat": true, "side_mirror_heat": true,
 "steering_wheel": "high",
 "seats": {"driver": "heat_medium"}}
```

| Key | Values | Default |
|---|---|---|
| `temp` | 62–82 °F, or 17–28 °C with `temp_unit: "C"` | required |
| `temp_unit` | `F` / `C` (°C is converted to whole °F for the US API) | `F` |
| `duration_min` | 1–30 | required |
| `defrost` | `true` / `false` | `false` |
| `rear_window_heat` | `true` / `false` | `false` |
| `side_mirror_heat` | `true` / `false`; needs `rear_window_heat` (the API has no "mirrors only" code) | `false` |
| `steering_wheel` | `off` / `low` / `high` | `off` |
| `seats` | `{"driver" \| "passenger" \| "rear_left" \| "rear_right": level}` | not sent |

Seat levels: `off`, `heat_low`, `heat_medium`, `heat_high`, `cool_low`, `cool_medium`, `cool_high`.
- Once `seats` is given, all four seats are set, and any seat not named is turned off.
- Without `seats`, the request doesn't touch the seats.
- `driver` is the front-left seat (US cars).

Unknown keys or out-of-range values are rejected and nothing is sent.

Which options a car actually applies depends on the model and its equipment.
The `low` steering-wheel step is unverified.

Example presets a consumer might keep:
- Winter: the example above.
- Summer: `{"temp": 72, "duration_min": 10, "seats": {"driver": "cool_medium"}}`

## Login and one-time passcodes

The session lives in `$DATA_DIR/token.json` (mode 600). It holds the Kia
session id, the long-lived refresh token and the device id. It does **not**
hold the username or password. Keep `/data` on a persistent volume so
restarts and upgrades reuse it without a new passcode.

When Kia asks for a passcode (first run, or the refresh token expired):
1. The bridge sends **one** code (`KIA_OTP_METHOD`), publishes `auth/otp_required`, sets `auth/state` to `otp_required`, and **pauses polling**.
2. Publish the code to `{base}/auth/otp/set`. A wrong code is reported in `result` and doesn't trigger a new code.
3. The pending request is saved in `otp.json`, so a restart keeps waiting for the same code instead of sending another.
4. Need a new code? Publish to `{base}/auth/otp/request` (at most once a minute). The bridge never sends codes on its own in a loop.

## Configuration

Environment variables:

| Variable | Default | |
|---|---|---|
| `CAR_NAME` | required | used in topics, `[a-z0-9_-]` |
| `KIA_USERNAME` | required | Kia Connect login (e.g. `you@example.com`) |
| `KIA_PASSWORD` | required | |
| `KIA_PIN` | empty | Kia Connect PIN |
| `KIA_VEHICLE` | | car nickname; needed only if the account has more than one car |
| `KIA_OTP_METHOD` | `EMAIL` | `EMAIL` or `SMS` |
| `KIA_LOCATION` | `false` | `true` requests and publishes GPS location |
| `MQTT_URL` | required | `mqtt://user:pass@host:1883` or `mqtts://…` (URL-encode special characters) |
| `TOPIC_PREFIX` | `kia` | |
| `POLL_MINUTES` | `30` | minimum 5. Kia rate-limits accounts that poll too often. |
| `CLIMATE_DEBOUNCE_S` | `60` | `0` disables |
| `DATA_DIR` | `/data` | token, pending passcode, last poll time |
| `HEALTH_PORT` | `8080` | |
| `LOG_LEVEL` | `INFO` | The library's own logger is held at WARNING, because it logs full Kia responses at DEBUG. |

After a restart, the first poll waits until 10 minutes have passed since the
previous one, so a crash loop can't hammer Kia.

### Docker

```sh
docker run -d --name kia2mqtt \
  -e CAR_NAME=my-ev9 -e KIA_USERNAME=you@example.com -e KIA_PASSWORD=… \
  -e MQTT_URL=mqtt://broker:1883 \
  -v kia2mqtt-data:/data -p 8080:8080 \
  kwv4/kia2mqtt:latest
```

The image runs as uid 1000. See also `docker-compose.example.yml`.

## Development

```sh
uv venv -p 3.12 venv && VIRTUAL_ENV=venv uv pip install -r requirements.txt -r requirements-dev.txt
./venv/bin/pytest
./venv/bin/flake8 .
```

Tests replay a hand-written `cmm/gvi` payload (`tests/fixtures/gvi_ev9.json`).
Any new fixture must be stripped of VIN, vehicle key, account and owner fields,
nickname, plate, dealer codes and location before it is committed.

## Releasing

CI builds `kwv4/kia2mqtt:{version}` and `:latest` (amd64 + arm64) on a `v*` tag.
`make bump` / `bump-minor` opens a release PR; after it merges, push the tag on
its own (`git push origin vX.Y.Z`). Merged Dependabot pip/docker PRs tag the next
patch automatically. `hyundai_kia_connect_api` is pinned exactly and bumped only
through those PRs.

## License

MIT
