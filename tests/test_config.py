import pytest

from zeroout import home
from zeroout.config import load_settings

KEYS = ("DB_PATH", "AUDIO_DIR", "ASR_MODEL_DIR", "OWNER_NUMBER", "TF_API_KEY", "TF_BASE_URL",
        "ZEROOUT_HOME", "ZEROOUT_ENV_FILE", "RATE_PER_MIN", "SESSION_SECRET")


@pytest.fixture
def box(tmp_path, monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("ZEROOUT_HOME", str(tmp_path / "zhome"))
    (tmp_path / "zhome").mkdir()
    return tmp_path


def user_file(box, text):
    (box / "zhome" / "zeroout.env").write_text(text)
    return box / "zhome" / "zeroout.env"


def test_defaults_live_in_the_data_folder(box):
    s = load_settings()
    assert s.db_path == str(home.data_dir() / "zeroout.db")
    assert s.audio_dir == str(home.data_dir() / "audio")
    assert s.asr_model_dir.startswith(str(home.models_dir()))
    assert s.owner_number == "" and s.config_path == "environment"


def test_user_config_file_is_read(box):
    f = user_file(box, "TF_API_KEY=fromuser\nOWNER_NUMBER=+14155550100\n")
    s = load_settings()
    assert s.tf_api_key == "fromuser" and s.owner_number == "+14155550100"
    assert s.config_path == str(f)


def test_stray_cwd_env_is_ignored(box):
    user_file(box, "TF_BASE_URL=https://good.example/v1/\n")
    (box / "cwd" / ".env").write_text(
        "TF_BASE_URL=https://attacker.example/v1/\nOWNER_NUMBER=+19995550000\n"
        "RATE_PER_MIN=0\nSESSION_SECRET=x\n")
    s = load_settings()
    assert s.tf_base_url == "https://good.example/v1/"
    assert s.owner_number == "" and s.rate_per_min == 0.014 and s.session_secret == ""


def test_zeroout_home_in_a_file_is_ignored(box):
    user_file(box, "TF_API_KEY=k\n")
    (box / "cwd" / ".env").write_text(f"ZEROOUT_HOME={box / 'evil'}\n")
    assert load_settings().tf_api_key == "k"
    assert home.config_dir() == box / "zhome"


def test_named_dev_file_is_honoured_and_beats_user_file(box, monkeypatch):
    user_file(box, "TF_API_KEY=user\nTF_BASE_URL=u\n")
    dev = box / "cwd" / ".env"
    dev.write_text("TF_API_KEY=dev\n")
    monkeypatch.setenv("ZEROOUT_ENV_FILE", ".env")
    s = load_settings()
    assert s.tf_api_key == "dev" and s.tf_base_url == "u"
    assert s.config_path == f".env, {box / 'zhome' / 'zeroout.env'}"


def test_real_environment_beats_files(box, monkeypatch):
    user_file(box, "DB_PATH=user.db\n")
    monkeypatch.setenv("DB_PATH", "real.db")
    assert load_settings().db_path == "real.db"


def test_blank_value_does_not_block_later_sources(box, monkeypatch):
    user_file(box, "TF_API_KEY=user\n")
    (box / "cwd" / ".env").write_text("TF_API_KEY=\n")
    monkeypatch.setenv("ZEROOUT_ENV_FILE", ".env")
    monkeypatch.setenv("TF_BASE_URL", "")
    assert load_settings().tf_api_key == "user"


def test_references_stay_literal(box):
    user_file(box, "TF_API_KEY=${HOME}/x\n")
    assert load_settings().tf_api_key == "${HOME}/x"


def test_files_are_not_written_into_the_environment(box):
    import os
    user_file(box, "TF_API_KEY=user\n")
    load_settings()
    assert "TF_API_KEY" not in os.environ


def test_relative_values_are_kept_as_given(box, monkeypatch):
    monkeypatch.setenv("AUDIO_DIR", "data/audio")
    assert load_settings().audio_dir == "data/audio"


def test_zeroout_home_in_a_named_env_file_does_not_move_paths(box, monkeypatch):
    elsewhere = box / "elsewhere"
    named = box / "named.env"
    named.write_text(f"ZEROOUT_HOME={elsewhere}\n")
    monkeypatch.setenv("ZEROOUT_ENV_FILE", str(named))
    s = load_settings()
    assert home.config_dir() == box / "zhome" and home.data_dir() == box / "zhome"
    assert s.db_path == str(box / "zhome" / "zeroout.db")
    assert str(elsewhere) not in (s.audio_dir, s.asr_model_dir)


def test_zeroout_home_unset_in_real_env_is_not_taken_from_files(box, monkeypatch):
    monkeypatch.delenv("ZEROOUT_HOME")
    monkeypatch.setattr("zeroout.home.sys.platform", "linux")
    xdg_config, xdg_data = box / "xdg-config", box / "xdg-data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    line = f"ZEROOUT_HOME={box / 'elsewhere'}\n"
    named = box / "named.env"
    named.write_text(line)
    (box / "cwd" / ".env").write_text(line)
    monkeypatch.setenv("ZEROOUT_ENV_FILE", str(named))
    s = load_settings()
    assert home.config_dir() == xdg_config / "zeroout"
    assert home.data_dir() == xdg_data / "zeroout"
    assert s.db_path == str(xdg_data / "zeroout" / "zeroout.db")
    assert "elsewhere" not in s.db_path + s.audio_dir + s.asr_model_dir + str(home.config_file())


def test_community_repo_defaults_and_overrides(box, monkeypatch):
    assert load_settings().community_repo == "MAXIMUSSCORP/zero-out-trees"
    monkeypatch.setenv("COMMUNITY_REPO", "someone/their.trees_2")
    assert load_settings().community_repo == "someone/their.trees_2"


@pytest.mark.parametrize("bad", [
    "nodash", "a/b/c", "../x", "a/..", "owner/na me", "owner/name;rm", "-/", "/name", "owner/", "ow ner/x",
    "owner/name\nx", "https://github.com/a/b", "owner/.", "owner/...", "ow/.."])
def test_bad_community_repo_is_refused(box, monkeypatch, bad):
    monkeypatch.setenv("COMMUNITY_REPO", bad)
    with pytest.raises(ValueError, match="COMMUNITY_REPO"):
        load_settings()
