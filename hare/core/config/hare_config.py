from __future__ import annotations

import importlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING, Any, cast

from hare.core.config.app_config import AppConfig
from hare.core.config.cli_config import CliConfig
from hare.core.config.config_section import ConfigSection
from hare.core.config.connection_config import ConnectionConfig
from hare.core.config.db_url_config import DBUrlConfig
from hare.core.constants import (
    JSON_CONFIG_FILE_EXTENSIONS,
    SUPPORTED_CONFIG_FILE_EXTENSIONS,
    SWAPPABLE_MODEL_LABEL_PATTERN,
    SWAPPABLE_SETTING_NAME_PATTERN,
    YAML_CONFIG_FILE_EXTENSIONS,
)
from hare.dialects.base.db_url import DbUrlConfigGenerator
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Self


@dataclass(frozen=True)
class HareConfig(ConfigSection):
    connections: dict[str, ConnectionConfig | DBUrlConfig]
    apps: dict[str, AppConfig]
    routers: list[str | type] | None = None
    use_tz: bool | None = None
    timezone: str | None = None
    #: The ``cli`` section - extra ``hare`` subcommands. Only the CLI reads it.
    cli: CliConfig | None = None
    #: Swappable model settings - setting name (``"USER_MODEL"``) to the ``"app_label.ModelName"``
    #: label of the model a relation declared with ``swappable(name)`` points at.
    swappable: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.connections, dict) or not self.connections:
            raise ConfigurationError("HareConfig.connections must be a non-empty dict")
        for name, conn in self.connections.items():
            if not isinstance(name, str) or not name:
                raise ConfigurationError("Connection names must be non-empty strings")
            if not isinstance(conn, (ConnectionConfig, DBUrlConfig)):
                raise ConfigurationError("Connection values must be ConnectionConfig or DBUrlConfig")

        if not isinstance(self.apps, dict) or not self.apps:
            raise ConfigurationError("HareConfig.apps must be a non-empty dict")
        for name, app in self.apps.items():
            if not isinstance(name, str) or not name:
                raise ConfigurationError("App names must be non-empty strings")
            if not isinstance(app, AppConfig):
                raise ConfigurationError("App values must be AppConfig")
            if app.default_connection and app.default_connection not in self.connections:
                raise ConfigurationError(f'App "{name}" refers to unknown connection "{app.default_connection}"')

        if self.routers is not None:
            if not isinstance(self.routers, list):
                raise ConfigurationError("HareConfig.routers must be a list or None")
            for router in self.routers:
                if not isinstance(router, (str, type)):
                    raise ConfigurationError("Routers must be str or type")

        if self.use_tz is not None and not isinstance(self.use_tz, bool):
            raise ConfigurationError("HareConfig.use_tz must be a bool or None")

        if self.timezone is not None and not isinstance(self.timezone, str):
            raise ConfigurationError("HareConfig.timezone must be a string or None")

        if self.cli is not None and not isinstance(self.cli, CliConfig):
            raise ConfigurationError("HareConfig.cli must be a CliConfig or None")

        if self.swappable is not None:
            self._check_swappable()

    def _check_swappable(self) -> None:
        """Checks every ``swappable`` setting name and the model label it points at.

        Raises:
            ConfigurationError: ``swappable`` isn't a dict, a setting name isn't an upper-case
                identifier, a value isn't an ``"app_label.ModelName"`` label, or its app isn't
                configured.
        """
        if not isinstance(self.swappable, dict):
            raise ConfigurationError('Config "swappable" must be a mapping of setting name to "app_label.ModelName"')
        for setting, label in self.swappable.items():
            if not isinstance(setting, str) or not SWAPPABLE_SETTING_NAME_PATTERN.fullmatch(setting):
                raise ConfigurationError(
                    f'Swappable setting name {setting!r} must be an upper-case identifier such as "USER_MODEL"'
                )
            if not isinstance(label, str) or not SWAPPABLE_MODEL_LABEL_PATTERN.fullmatch(label):
                raise ConfigurationError(
                    f'Swappable setting "{setting}" must name a model as "app_label.ModelName", got {label!r}'
                )
            app_label = label.split(".", 1)[0]
            if app_label not in self.apps:
                raise ConfigurationError(
                    f'Swappable setting "{setting}" points at "{label}", but no app "{app_label}" is configured'
                )

    def to_dict(self) -> dict[str, Any]:
        connections = {name: conn.to_config() for name, conn in self.connections.items()}
        apps = {name: app.to_dict() for name, app in self.apps.items()}
        config: dict[str, Any] = {
            "connections": connections,
            "apps": apps,
        }
        if self.routers is not None:
            config["routers"] = self.routers
        if self.use_tz is not None:
            config["use_tz"] = self.use_tz
        if self.timezone is not None:
            config["timezone"] = self.timezone
        if self.cli is not None:
            config["cli"] = self.cli.to_dict()
        if self.swappable is not None:
            config["swappable"] = dict(self.swappable)
        return config

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        if not isinstance(data, Mapping):
            raise ConfigurationError("HareConfig must be created from a mapping")
        cls.check_known_keys(data, "the config")

        if "connections" not in data:
            raise ConfigurationError('Config must define "connections" section')
        if "apps" not in data:
            raise ConfigurationError('Config must define "apps" section')

        raw_connections = data["connections"]
        if not isinstance(raw_connections, Mapping):
            raise ConfigurationError('Config "connections" must be a mapping')
        connections: dict[str, ConnectionConfig | DBUrlConfig] = {}
        for name, conn in raw_connections.items():
            if isinstance(conn, str):
                connections[name] = DBUrlConfig(conn)
            elif isinstance(conn, Mapping):
                connections[name] = ConnectionConfig.from_dict(conn, f"connection {name!r}")
            else:
                raise ConfigurationError("Connection values must be mapping or string")

        raw_apps = data["apps"]
        if not isinstance(raw_apps, Mapping):
            raise ConfigurationError('Config "apps" must be a mapping')
        apps: dict[str, AppConfig] = {}
        for name, app in raw_apps.items():
            if not isinstance(app, Mapping):
                raise ConfigurationError("App values must be mappings")
            apps[name] = AppConfig.from_dict(app, f"app {name!r}")

        routers = data.get("routers")
        if routers is not None and not isinstance(routers, list):
            if isinstance(routers, str):
                raise ConfigurationError("HareConfig.routers must be a list or None")
            try:
                routers = list(routers)
            except TypeError as e:
                raise ConfigurationError("HareConfig.routers must be a list, an iterable, or None") from e

        cli_section = data.get("cli")
        cli = CliConfig.from_dict(cli_section) if cli_section is not None else None

        swappable = data.get("swappable")
        if swappable is not None and not isinstance(swappable, Mapping):
            raise ConfigurationError('Config "swappable" must be a mapping of setting name to "app_label.ModelName"')

        return cls(
            connections=connections,
            apps=apps,
            routers=routers,
            use_tz=data.get("use_tz"),
            timezone=data.get("timezone"),
            cli=cli,
            swappable=dict(swappable) if swappable is not None else None,
        )

    @classmethod
    def from_config_file(cls, config_file: str) -> Self:
        """
        Load configuration from a YAML or JSON file.

        Args:
            config_file (str): Path to the configuration file. Supported extensions: .yml, .yaml,
                .json.

        Returns:
            Self: The constructed HareConfig.

        Raises:
            ConfigurationError: If the file is missing, unsupported, or contents are invalid.
        """
        _, extension = os.path.splitext(config_file)
        extension = extension.lower()
        if extension in YAML_CONFIG_FILE_EXTENSIONS:
            import yaml  # pylint: disable=C0415

            try:
                with open(config_file, encoding="utf-8") as f:
                    config = yaml.safe_load(f)
            except FileNotFoundError as exc:
                raise ConfigurationError(f"Config file not found: {config_file}") from exc
            except OSError as exc:
                raise ConfigurationError(f"Cannot read config file {config_file}: {exc}") from exc
            except UnicodeDecodeError as exc:
                raise ConfigurationError(f"Config file {config_file} is not valid UTF-8: {exc}") from exc
            except yaml.YAMLError as exc:
                raise ConfigurationError(f"Invalid YAML in config file {config_file}: {exc}") from exc
        elif extension in JSON_CONFIG_FILE_EXTENSIONS:
            try:
                with open(config_file, encoding="utf-8") as f:
                    config = json.load(f)
            except FileNotFoundError as exc:
                raise ConfigurationError(f"Config file not found: {config_file}") from exc
            except OSError as exc:
                raise ConfigurationError(f"Cannot read config file {config_file}: {exc}") from exc
            except UnicodeDecodeError as exc:
                raise ConfigurationError(f"Config file {config_file} is not valid UTF-8: {exc}") from exc
            except json.JSONDecodeError as exc:
                raise ConfigurationError(f"Invalid JSON in config file {config_file}: {exc}") from exc
        else:
            supported = ", ".join(SUPPORTED_CONFIG_FILE_EXTENSIONS)
            raise ConfigurationError(f"Unknown config extension {extension}, only {supported} are supported")
        return cls.from_dict(config)

    @classmethod
    def from_db_url(cls, db_url: str, modules: Mapping[str, Iterable[str | ModuleType]]) -> Self:
        """
        Create a HareConfig instance using a database URL and app modules mapping.

        This factory method builds a configuration dictionary using the provided database URL and modules,
        and returns a HareConfig instance based on that configuration.

        Args:
            db_url: Database connection URL as a string.
            modules:
                A mapping where keys are app names, and values are iterables of Python module names
                (as strings or Python module types) containing ORM models.

        Returns:
            Self: The constructed HareConfig instance.

        Raises:
            ConfigurationError: If the generated config is invalid.
        """
        if not isinstance(modules, Mapping):
            raise ConfigurationError(
                f"modules must be a mapping of app label to module paths, got {type(modules).__name__} - "
                "write {'models': [...]} instead"
            )
        config_dict = DbUrlConfigGenerator.build(db_url, app_modules=modules)
        return cls.from_dict(config_dict)

    @classmethod
    def load(cls, source: Mapping[str, Any] | Self | str) -> Self:
        """The configuration from wherever it is given.

        Args:
            source: A ``HareConfig``; a dict of the same shape; the path of a .json/.yml/.yaml
                file holding it; or ``module.VARIABLE`` naming a module-level dict or
                ``HareConfig``.

        Returns:
            The configuration.

        Raises:
            ConfigurationError: The source can't be read or the configuration is invalid.
        """
        if isinstance(source, HareConfig):
            return source
        if isinstance(source, Mapping):
            return cls.from_dict(source)
        if not isinstance(source, str):
            raise ConfigurationError(
                f"config must be a HareConfig, a dict, a file path or 'module.VARIABLE', got {type(source).__name__}"
            )
        if os.path.splitext(source)[1].lower() in SUPPORTED_CONFIG_FILE_EXTENSIONS or cls.is_file_path(source):
            return cls.from_config_file(source)
        return cls.from_module_variable(source)

    @staticmethod
    def is_file_path(source: str) -> bool:
        """Whether a configuration source names a file rather than ``module.VARIABLE`` - it holds a
        path separator, or a file of that name exists."""
        return "/" in source or os.path.sep in source or os.path.isfile(source)

    @classmethod
    def from_module_variable(cls, path: str) -> Self:
        """The configuration a module-level variable holds.

        Args:
            path: ``module.VARIABLE``, e.g. ``"settings.HARE_ORM"``.

        Returns:
            The configuration.

        Raises:
            ConfigurationError: The path names no importable module or no such variable, or the
                variable holds neither a dict nor a ``HareConfig``.
        """
        module_path, _, variable_name = path.strip().rpartition(".")
        if not module_path or not variable_name:
            raise ConfigurationError(
                f"Invalid config path {path!r}: expected 'module.VARIABLE' (e.g. 'config.HARE_ORM') or the path "
                "of a .json/.yml file"
            )
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError:
            raise ConfigurationError(
                f"Cannot import configuration module {module_path!r} - make sure it is on the Python path "
                f"(current working directory: {os.getcwd()}); a configuration file is named with one of the "
                f"extensions {', '.join(SUPPORTED_CONFIG_FILE_EXTENSIONS)}"
            ) from None
        value = getattr(module, variable_name, None)
        if value is None:
            available_names = [name for name in dir(module) if not name.startswith("_") and name.isupper()]
            hint = f" - available: {', '.join(available_names[:5])}" if available_names else ""
            raise ConfigurationError(f"Variable {variable_name!r} not found in module {module_path!r}{hint}")
        if isinstance(value, HareConfig):
            return cast("Self", value)
        if not isinstance(value, Mapping):
            raise ConfigurationError(
                f"Config variable {path!r} must be a dict or a HareConfig, got {type(value).__name__}"
            )
        return cls.from_dict(value)
