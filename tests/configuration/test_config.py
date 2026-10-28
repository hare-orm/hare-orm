import sys

import pytest

import tests.shared_models.testmodels as testmodels_module
from hare.core.config import AppConfig, ConnectionConfig, DBUrlConfig, HareConfig
from hare.exceptions import ConfigurationError

pytestmark = pytest.mark.database_independent

MINIMAL_CONFIG = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {"models": {"models": ["tests.shared_models.testmodels"]}},
}


def test_connection_config_db_url_round_trips_through_to_dict_and_from_dict():
    """ConnectionConfig(db_url=...).to_config() used to return a bare string - indistinguishable
    from DBUrlConfig's own to_config() output, since HareConfig.from_dict() picks the
    reconstructed connection class purely by shape (str -> DBUrlConfig, mapping ->
    ConnectionConfig.from_dict()). HareConfig.from_dict(cfg.to_dict()) silently reconstructed
    this ConnectionConfig as a DBUrlConfig instead, breaking round-trip class identity."""
    original = HareConfig(
        connections={"default": ConnectionConfig(db_url="sqlite+aiosqlite://:memory:")},
        apps={"models": AppConfig(models=["tests.shared_models.testmodels"])},
    )

    round_tripped = HareConfig.from_dict(original.to_dict())

    connection = round_tripped.connections["default"]
    assert isinstance(connection, ConnectionConfig)
    assert not isinstance(connection, DBUrlConfig)
    assert connection == original.connections["default"]


def test_from_dict_routers_list_passes_through():
    config = HareConfig.from_dict({**MINIMAL_CONFIG, "routers": ["hare.router.ConnectionRouter"]})
    assert config.routers == ["hare.router.ConnectionRouter"]


def test_from_dict_routers_other_iterable_is_coerced_to_list():
    config = HareConfig.from_dict({**MINIMAL_CONFIG, "routers": ("hare.router.ConnectionRouter",)})
    assert config.routers == ["hare.router.ConnectionRouter"]


def test_from_config_file_malformed_json_raises_configuration_error(tmp_path):
    """json.load() used to raise a bare JSONDecodeError, contradicting this method's own
    docstring, which promises ConfigurationError for invalid file contents."""
    config_file = tmp_path / "hare.json"
    config_file.write_text('{"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Invalid JSON"):
        HareConfig.from_config_file(str(config_file))


@pytest.mark.requires("yaml")
def test_from_config_file_malformed_yaml_raises_configuration_error(tmp_path):
    """yaml.safe_load() used to raise a bare yaml.YAMLError subclass instead of ConfigurationError."""
    config_file = tmp_path / "hare.yaml"
    yaml_content = "connections:\n  default: sqlite+aiosqlite://:memory:\napps:\n  app:\n  models: bad\n"
    config_file.write_text(yaml_content, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Invalid YAML"):
        HareConfig.from_config_file(str(config_file))


def test_a_yaml_config_file_without_pyyaml_names_its_extra(tmp_path, monkeypatch):
    """A YAML file read without PyYAML raised a bare ModuleNotFoundError: yaml."""
    monkeypatch.setitem(sys.modules, "yaml", None)
    config_file = tmp_path / "hare.yaml"
    config_file.write_text("connections: {}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match=r"hare-orm\[yaml\]"):
        HareConfig.from_config_file(str(config_file))


def test_from_config_file_missing_file_raises_configuration_error(tmp_path):
    """open() used to raise a bare FileNotFoundError, contradicting this method's own docstring,
    which promises ConfigurationError when the file is missing."""
    with pytest.raises(ConfigurationError, match="not found"):
        HareConfig.from_config_file(str(tmp_path / "does_not_exist.json"))


@pytest.mark.parametrize(
    "models",
    [
        pytest.param(("tests.shared_models.testmodels",), id="tuple_of_str"),
        pytest.param((testmodels_module,), id="tuple_of_module_type"),
        pytest.param([testmodels_module], id="list_of_module_type"),
    ],
)
def test_app_config_accepts_iterable_of_str_or_module_type(models):
    """AppConfig.models is documented (via Hare.init()/HareContext.init()'s own `modules=` type
    hint, dict[str, Iterable[str | ModuleType]]) to accept any iterable of dotted-path strings
    or already-imported modules, not just a list of strings."""
    app_config = AppConfig(models=models)
    assert app_config.models == ["tests.shared_models.testmodels"]


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            {**MINIMAL_CONFIG, "router": ["some.Router"]},
            "Unknown key 'router' in the config - known keys: .* did you mean \"routers\"",
        ),
        (
            {
                "connections": {"default": "sqlite+aiosqlite://:memory:", "other": "sqlite+aiosqlite://:memory:"},
                "apps": {"models": {"models": ["tests.shared_models.testmodels"], "default_conection": "other"}},
            },
            "Unknown key 'default_conection' in app 'models' .* did you mean \"default_connection\"",
        ),
        (
            {
                "connections": {"default": {"engine": "sqlite+aiosqlite", "credential": {"file_path": ":memory:"}}},
                "apps": {"models": {"models": ["tests.shared_models.testmodels"]}},
            },
            "Unknown key 'credential' in connection 'default' .* did you mean \"credentials\"",
        ),
        (
            {**MINIMAL_CONFIG, "cli": {"command": ["package.module:Command"]}},
            'Unknown key \'command\' in the "cli" section .* did you mean "commands"',
        ),
    ],
    ids=["top-level", "app", "connection", "cli"],
)
def test_from_dict_refuses_unknown_keys(config, message):
    """A misspelt key is refused, naming the closest known one, instead of being ignored - an
    ignored ``default_conection`` left the app's models on the default connection."""
    with pytest.raises(ConfigurationError, match=message):
        HareConfig.from_dict(config)


def test_config_mapping_keys_are_the_dataclass_fields():
    """Every field of the config dataclasses round-trips through the mapping form."""
    from hare.core.config import CliConfig

    config = HareConfig(
        connections={"default": ConnectionConfig(engine="sqlite+aiosqlite", credentials={"file_path": ":memory:"})},
        apps={
            "models": AppConfig(
                models=["tests.shared_models.testmodels"], default_connection="default", migrations="m"
            )
        },
        routers=[],
        use_timezone=False,
        timezone="UTC",
        cli=CliConfig(commands=["package.module:Command"]),
        swappable={"USER_MODEL": "models.Author"},
    )

    assert HareConfig.from_dict(config.to_dict()) == config
