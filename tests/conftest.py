"""Shared fixtures for the notebooklm test suite.

Three responsibilities:

1. Per-service `main.py` loaders. All three services have `main.py`, so a
   plain `import main` only resolves to whichever one wins `sys.path`.
   We use `importlib.util.spec_from_file_location` to load each under a
   distinct name (`slack_main`, `gong_main`, `config_main`, `gong_api`)
   and register in `sys.modules`.

2. Autouse no-real-IO fixture. Patches `shared.secrets.get_secret` and
   the `storage.Client` factory in `shared.gcs_mapping` so an accidental
   real call surfaces as a test failure rather than silent network I/O.

3. Autouse cache-reset fixture. Clears `shared.gcs_mapping._cache` and
   `shared.secrets._cache` before each test so the TTL tests don't leak
   state into anything that runs after them.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent

# Make `shared` importable as a real top-level package. Services ship
# `shared/` rsynced next to their own `main.py` at deploy time, but in
# tests we import from the repo root.
sys.path.insert(0, str(REPO))


def _load_module(alias, path, extra_syspath=None):
    """Load `path` under module name `alias`, returning the module object."""
    if extra_syspath:
        sys.path.insert(0, str(extra_syspath))
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not build spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def slack_main():
    """Load slack-sync/main.py under the alias `slack_main`."""
    return _load_module(
        "slack_main",
        REPO / "slack-sync" / "main.py",
        extra_syspath=REPO / "slack-sync",
    )


@pytest.fixture(scope="session")
def gong_api():
    """Load gong-sync/gong_api.py under the alias `gong_api`.

    Loaded before `gong_main` so that `gong_main`'s `from gong_api import ...`
    resolves to our alias rather than trying to re-exec the file.
    """
    return _load_module(
        "gong_api",
        REPO / "gong-sync" / "gong_api.py",
        extra_syspath=REPO / "gong-sync",
    )


@pytest.fixture(scope="session")
def gong_main(gong_api):
    """Load gong-sync/main.py under the alias `gong_main`."""
    return _load_module(
        "gong_main",
        REPO / "gong-sync" / "main.py",
        extra_syspath=REPO / "gong-sync",
    )


@pytest.fixture(scope="session")
def config_main():
    """Load config-sync/main.py under the alias `config_main`."""
    return _load_module(
        "config_main",
        REPO / "config-sync" / "main.py",
        extra_syspath=REPO / "config-sync",
    )


@pytest.fixture(autouse=True)
def _reset_shared_caches():
    """Ensure module-level caches in `shared.*` don't leak state across tests."""
    import shared.gcs_mapping as gcs_mapping
    import shared.secrets as secrets
    import shared.sheets as sheets

    gcs_mapping._cache.clear()
    gcs_mapping._client = None
    secrets._cache.clear()
    secrets._client = None
    sheets._sheets_client = None
    yield
    gcs_mapping._cache.clear()
    gcs_mapping._client = None
    secrets._cache.clear()
    secrets._client = None
    sheets._sheets_client = None


@pytest.fixture(autouse=True)
def _no_real_io(monkeypatch):
    """Poison real-IO boundaries so a forgotten mock surfaces loudly.

    Service modules do `from shared.secrets import get_secret`, which
    rebinds the name locally at import time. Patching just
    `shared.secrets.get_secret` would leave the service-local binding
    pointing at the original. We patch every alias we know about; tests
    that legitimately need these paths must re-patch the specific
    binding they use.
    """
    def _fail(*args, **kwargs):
        raise AssertionError(
            "Real I/O attempted in a test. Patch `<alias>.get_secret` "
            "(e.g. `slack_main.get_secret`) explicitly in your test."
        )

    monkeypatch.setattr("shared.secrets.get_secret", _fail)
    monkeypatch.setattr("shared.gcs_mapping._get_client", _fail)
    monkeypatch.setattr("shared.sheets.get_sheets_client", _fail)

    # Propagate to any service alias modules already loaded.
    for alias in ("slack_main", "gong_main", "gong_api", "config_main"):
        mod = sys.modules.get(alias)
        if mod is not None and hasattr(mod, "get_secret"):
            monkeypatch.setattr(f"{alias}.get_secret", _fail)


@pytest.fixture
def fake_request():
    """Build a MagicMock shaped like a Flask request for webhook-style tests."""
    def _build(method="POST", headers=None, body=b"", args=None):
        req = MagicMock()
        req.method = method
        req.headers = headers or {}
        req.get_data.return_value = body.decode() if isinstance(body, bytes) else body
        req.args = args or {}
        req.get_json.return_value = None
        return req

    return _build
