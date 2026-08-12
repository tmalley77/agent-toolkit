import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    monkeypatch.setenv("AGENT_DB_PATH", path)
    import importlib
    from agent_toolkit import database
    importlib.reload(database)
    yield path
    if os.path.exists(path):
        os.remove(path)
