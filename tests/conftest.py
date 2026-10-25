import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never read the developer's own zeroout.env or ZEROOUT_ENV_FILE."""
    monkeypatch.setenv("ZEROOUT_HOME", str(tmp_path / "zhome"))
    monkeypatch.delenv("ZEROOUT_ENV_FILE", raising=False)
