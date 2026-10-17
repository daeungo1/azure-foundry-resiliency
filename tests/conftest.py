"""Run async tests on the supported asyncio backend."""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
