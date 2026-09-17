"""Keep every synthetic test away from the user's private DAM home state."""

import pytest
from pathlib import Path
from uuid import uuid4


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch):
    # The path deliberately does not exist: tests must inject temporary paths
    # for writes, while default-path reads cannot reach the user's real files.
    isolated = Path(__file__).resolve().parents[2] / f".dam-test-home-{uuid4().hex}"
    monkeypatch.setenv("HOME", str(isolated))
