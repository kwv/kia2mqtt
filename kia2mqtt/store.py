"""Files on the data volume, written atomically with mode 600.

- token.json: the Kia session (sid, rmtoken, device_id). Credentials are not
  stored; they are re-added from the environment on load.
- otp.json: a pending OTP request, so a restart can still verify the code
  that was already sent instead of sending another.
- state.json: bridge bookkeeping (last poll time).
"""

import json
import os
from pathlib import Path
from typing import Any

from hyundai_kia_connect_api.Token import Token

_SECRET_FIELDS = ("username", "password", "pin")


def write_private_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def delete(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def save_token(path: Path, token: Token) -> None:
    data = token.to_dict()
    for field in _SECRET_FIELDS:
        data.pop(field, None)
    write_private_json(path, data)


def load_token(path: Path, username: str, password: str, pin: str) -> Token | None:
    data = read_json(path)
    if not data or not data.get("refresh_token"):
        return None
    data.update(username=username, password=password, pin=pin)
    try:
        return Token.from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None
