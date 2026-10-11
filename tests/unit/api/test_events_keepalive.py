"""The keepalive interval of ``GET /events`` must be a positive, finite number of seconds (M2.1).

An interval of 0 or less would make an idle stream spin (a keepalive on every turn of the event
loop), and NaN or infinity would make it send none at all. ``register_events`` refuses them when
the route is added, so the mistake shows at start-up and not as a busy server. All values are
fictional.
"""

import math

import pytest
from fastapi import FastAPI

from app.api.events import EVENTS_PATH, ShutdownSignal, register_events


def event_routes(app: FastAPI) -> list[object]:
    return [route for route in app.routes if getattr(route, "path", None) == EVENTS_PATH]


@pytest.mark.parametrize(
    "seconds",
    [
        pytest.param(0, id="zero-int"),
        pytest.param(0.0, id="zero"),
        pytest.param(-1.0, id="negative"),
        pytest.param(-0.001, id="slightly-negative"),
        pytest.param(math.nan, id="nan"),
        pytest.param(math.inf, id="inf"),
        pytest.param(-math.inf, id="minus-inf"),
    ],
)
def test_register_events_refuses_a_keepalive_that_is_not_positive_and_finite(
    seconds: float,
) -> None:
    app = FastAPI()

    with pytest.raises(ValueError, match="keepalive_seconds"):
        register_events(app, shutdown=ShutdownSignal(), keepalive_seconds=seconds)

    assert event_routes(app) == [], "a refused interval registers no route"


@pytest.mark.parametrize("seconds", [0.001, 0.01, 15.0, 3600])
def test_register_events_accepts_a_positive_finite_keepalive(seconds: float) -> None:
    app = FastAPI()

    register_events(app, shutdown=ShutdownSignal(), keepalive_seconds=seconds)

    assert len(event_routes(app)) == 1
