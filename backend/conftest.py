import sys
import os

import pytest

# Ensure the backend/ directory is on sys.path so `from app.xxx import ...`
# works regardless of how pytest is invoked (locally or in CI).
sys.path.insert(0, os.path.dirname(__file__))


# Pin anyio-marked async tests to the asyncio backend ONLY.
#
# anyio 3.x (what the CI runner resolves) runs every @pytest.mark.anyio test
# on BOTH the asyncio and trio backends by default. We don't depend on trio,
# it isn't installed, and our httpx.AsyncClient/ASGITransport tests don't run
# under it — so the [trio] variants ERROR/FAIL and, worse, leave an
# event-loop in a state that makes the next [asyncio] test hang forever in
# selector.select(). That wedged the CI pytest job for 30+ minutes while the
# identical suite passed locally (anyio 4.x runs asyncio-only by default).
#
# Forcing the backend here makes CI match local: only [asyncio] variants run.
@pytest.fixture
def anyio_backend():
    return "asyncio"
