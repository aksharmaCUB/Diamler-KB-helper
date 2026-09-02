import pytest

from kb_helper.config import ConfigError, expand_env, load_settings


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
