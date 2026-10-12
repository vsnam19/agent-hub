import pytest

from agenthub.config import ConfigError
from agenthub.workers import available_skills, worker_plugins


def _plugin(root, name):
    d = root / name
    (d / "skills").mkdir(parents=True)
    return d


def _user(config, text):
    config.mkdir(parents=True, exist_ok=True)
    (config / "workers.toml").write_text(text)


def _repo(repo, text):
    (repo / ".agent-hub").mkdir(parents=True, exist_ok=True)
    (repo / ".agent-hub" / "config.toml").write_text(text)


@pytest.fixture
def layout(tmp_path):
    plugins = tmp_path / "plugins"
    telaf = _plugin(plugins, "telaf")
    aspice = _plugin(plugins, "aspice")
    config = tmp_path / "config"
    repo = tmp_path / "repo"
    repo.mkdir()
    return config, repo, telaf, aspice


def test_no_user_file_gives_nothing(layout):
    config, repo, _, _ = layout
    _repo(repo, 'worker_plugins = ["telaf"]\n')
    assert worker_plugins(config, repo) == ()


def test_default_list_in_order(layout):
    config, repo, telaf, aspice = layout
    _user(config, f'default = ["telaf", "aspice"]\n[plugins]\ntelaf = "{telaf}"\naspice = "{aspice}"\n')
    assert worker_plugins(config, repo) == (telaf, aspice)


def test_repo_list_replaces_default(layout):
    config, repo, telaf, aspice = layout
    _user(config, f'default = ["telaf", "aspice"]\n[plugins]\ntelaf = "{telaf}"\naspice = "{aspice}"\n')
    _repo(repo, 'worker_plugins = ["aspice"]\n')
    assert worker_plugins(config, repo) == (aspice,)


def test_repo_cannot_name_unknown_plugin(layout):
    config, repo, telaf, _ = layout
    _user(config, f'default = ["telaf"]\n[plugins]\ntelaf = "{telaf}"\n')
    _repo(repo, 'worker_plugins = ["evil"]\n')
    with pytest.raises(ConfigError, match="'evil'"):
        worker_plugins(config, repo)


def test_missing_skills_dir_is_error(layout, tmp_path):
    config, repo, _, _ = layout
    bare = tmp_path / "bare"
    bare.mkdir()
    missing = tmp_path / "missing"
    _user(config, f'default = ["bare"]\n[plugins]\nbare = "{bare}"\n')
    with pytest.raises(ConfigError, match="skills/"):
        worker_plugins(config, repo)

    _user(config, f'default = ["gone"]\n[plugins]\ngone = "{missing}"\n')
    with pytest.raises(ConfigError, match="skills/"):
        worker_plugins(config, repo)


def test_tilde_is_expanded(layout, tmp_path, monkeypatch):
    config, repo, _, _ = layout
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _plugin(home / "Workspaces", "telaf")
    _user(config, 'default = ["telaf"]\n[plugins]\ntelaf = "~/Workspaces/telaf"\n')
    assert worker_plugins(config, repo) == ((home / "Workspaces" / "telaf").resolve(),)


def test_invalid_toml_names_file(layout):
    config, repo, _, _ = layout
    _user(config, "default = [\n")
    with pytest.raises(ConfigError) as err:
        worker_plugins(config, repo)
    assert str(config / "workers.toml") in str(err.value)


def test_repo_config_text_overrides_file(layout):
    config, repo, telaf, aspice = layout
    _user(config, f'default = ["telaf", "aspice"]\n[plugins]\ntelaf = "{telaf}"\naspice = "{aspice}"\n')
    _repo(repo, 'worker_plugins = ["telaf"]\n')
    assert worker_plugins(config, repo, 'worker_plugins = ["aspice"]\n') == (aspice,)
    assert worker_plugins(config, repo, "") == (telaf, aspice)


def test_default_must_be_list_of_names(layout):
    config, repo, telaf, _ = layout
    _user(config, f'default = "telaf"\n[plugins]\ntelaf = "{telaf}"\n')
    with pytest.raises(ConfigError, match="default must be a list"):
        worker_plugins(config, repo)


def test_plugins_values_must_be_strings(layout):
    config, repo, _, _ = layout
    _user(config, "default = []\n[plugins]\ntelaf = 1\n")
    with pytest.raises(ConfigError, match="plugins must be a table"):
        worker_plugins(config, repo)


def test_available_skills_maps_skill_to_plugin(layout, tmp_path):
    config, repo, telaf, aspice = layout
    (telaf / "skills" / "telaf").mkdir()
    (telaf / "skills" / "telaf" / "SKILL.md").write_text("# telaf\n")
    (aspice / "skills" / "spice").mkdir()
    (aspice / "skills" / "spice" / "SKILL.md").write_text("# spice\n")
    (aspice / "skills" / "no-file").mkdir()
    _user(config, f'default = ["telaf"]\n[plugins]\ntelaf = "{telaf}"\naspice = "{aspice}"\n')
    assert available_skills(config) == {"telaf": "telaf", "spice": "aspice"}
    assert available_skills(tmp_path / "absent") == {}
