# CLAUDE.md

Kia Connect (US) → MQTT bridge. The README holds the MQTT contract; keep it in sync with `kia2mqtt/bridge.py` (topics) and `kia2mqtt/status.py` (fields).

A general-purpose bridge for anyone's car. It holds no policy and publishes only under `{TOPIC_PREFIX}/{CAR_NAME}/…`. House-specific choices (presets, schedules, topic mapping) belong to consumers, never in this repo.

## Layout
- `kia2mqtt/config.py`: env settings
- `kia2mqtt/status.py`: gvi payload → status fields (pure, tested against the fixture)
- `kia2mqtt/kia.py`: library wrapper; `KiaApi` overrides the status request so it never asks for location
- `kia2mqtt/climate.py`: generic climate/set JSON → library options (no named presets; consumers build requests)
- `kia2mqtt/store.py`: token/otp/state files (mode 600)
- `kia2mqtt/bridge.py`: worker loop, commands, OTP flow
- `kia2mqtt/__main__.py`: paho MQTT, health server

## Commands
```sh
./venv/bin/pytest
./venv/bin/flake8 .
./venv/bin/black kia2mqtt tests && ./venv/bin/isort kia2mqtt tests
```

## Rules
- Public repo: no real email, VIN, vehicleKey, account IDs, tokens or GPS anywhere: code, fixtures, logs, commits. Use `you@example.com`.
- Never run anything that writes to the car (lock, climate, charge limit, even a no-op) without the user's explicit OK.
- Stay a generic bridge: no presets, schedules or house-specific defaults. Expose API capabilities as commands; never send them automatically (force refresh, charge start/stop are user-triggered only).
- After a library bump, `tests/test_kia.py::test_library_status_path_uses_override_end_to_end` must still pass. It guards the location-off-by-default override.
