import pytest

from kb_helper.config import ConfigError, ConfigStore, expand_env, load_settings


def test_expand_env(monkeypatch):
    monkeypatch.setenv("SECRET", "s3")
    assert expand_env({"a": "${SECRET}", "b": ["${MISSING:-dflt}"], "c": 3}) == {"a": "s3", "b": ["dflt"], "c": 3}
    with pytest.raises(ConfigError):
        expand_env("${MISSING_NO_DEFAULT}")


def test_load_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TENANT", "t1")
    config = tmp_path / "config.yaml"
    config.write_text(
        "assistant:\n  model: claude-sonnet-5\n  effort: medium\nserver:\n  port: 9000\n"
        "connectors:\n  - name: sp\n    type: sharepoint\n    options:\n      tenant_id: ${TENANT}\n",
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert settings.model == "claude-sonnet-5"
    assert settings.effort == "medium"
    assert settings.port == 9000
    assert settings.connectors[0]["options"]["tenant_id"] == "t1"


def test_missing_file_gives_defaults(tmp_path):
    settings = load_settings(tmp_path / "none.yaml")
    assert settings.connectors == [] and settings.model == "claude-opus-5"


def test_bad_effort(tmp_path):
    config = tmp_path / "c.yaml"
    config.write_text("assistant:\n  effort: turbo\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(config)


def test_config_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = tmp_path / "config.yaml"
    store = ConfigStore(path)
    assert not store.exists and store.settings().api_key is None

    store.update_assistant(api_key="sk-ant-test", model="claude-sonnet-5", effort="low", extra_instructions=None)
    store.upsert_connector({"name": "docs", "type": "local_folder", "options": {"path": "."}})
    store.upsert_connector({"name": "sp", "type": "sharepoint", "options": {"auth_mode": "user"}})
    store.save()
    assert oct(path.stat().st_mode & 0o777) == "0o600"

    again = ConfigStore(path)
    settings = again.settings()
    assert settings.api_key == "sk-ant-test" and settings.model == "claude-sonnet-5" and settings.effort == "low"
    assert [c["name"] for c in again.connectors()] == ["docs", "sp"]

    again.upsert_connector({"name": "docs2", "type": "local_folder", "options": {"path": "/x"}}, previous_name="docs")
    assert [c["name"] for c in again.connectors()] == ["docs2", "sp"]
    assert again.remove_connector("sp") is True and again.remove_connector("sp") is False
    assert again.get_connector("docs2")["options"] == {"path": "/x"}


def test_env_api_key_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    store = ConfigStore(tmp_path / "c.yaml")
    store.update_assistant(api_key="sk-file")
    settings = store.settings()
    assert settings.api_key == "sk-env" and settings.api_key_from_env is True
