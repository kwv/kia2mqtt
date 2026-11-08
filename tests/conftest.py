import copy
import json
from pathlib import Path

import pytest

from kia2mqtt import config

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def gvi():
    with open(FIXTURES / "gvi_ev9.json") as f:
        return copy.deepcopy(json.load(f))


@pytest.fixture
def settings(tmp_path):
    return config.load(
        {
            "CAR_NAME": "testcar",
            "KIA_USERNAME": "you@example.com",
            "KIA_PASSWORD": "not-a-real-password",
            "MQTT_URL": "mqtt://broker.example:1883",
            "DATA_DIR": str(tmp_path),
        }
    )
