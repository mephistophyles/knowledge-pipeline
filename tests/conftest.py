import pytest

from pipeline.config import Settings
from pipeline.db import bootstrap
from pipeline.vault import VaultWriter


@pytest.fixture
def settings(tmp_path):
    """A Settings pointed entirely at a tmp dir (local blob store)."""
    raw = {
        "pipeline_version": "0.1.0-test",
        "paths": {"db": "pipeline.db", "intermediate": "intermediate", "vault": "vault"},
        "storage": {"blobstore": "local", "local": {"root": "raw"}},
        "worker": {"poll_interval_seconds": 0.01, "max_attempts": 3},
    }
    s = Settings(raw=raw, config_path=tmp_path / "pipeline.yaml", root=tmp_path)
    VaultWriter(s.vault_dir).ensure_layout()
    return s


@pytest.fixture
def conn(settings):
    return bootstrap(settings.db_path)
