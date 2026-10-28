from __future__ import annotations

import importlib
import json
import keyword
import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING, Any, cast

from hare.core.config.app_config import AppConfig
from hare.core.config.cli_config import CliConfig
from hare.core.config.config_section import ConfigSection
from hare.core.config.connection_config import ConnectionConfig
from hare.core.config.constants import SWAPPABLE_MODEL_LABEL_PATTERN
from hare.core.config.db_url_config import DBUrlConfig
from hare.core.config.migrations_config import MigrationsConfig
from hare.core.constants import (
    JSON_CONFIG_FILE_EXTENSIONS,
    SUPPORTED_CONFIG_FILE_EXTENSIONS,
    SWAPPABLE_SETTING_NAME_PATTERN,
    YAML_CONFIG_FILE_EXTENSIONS,
    YAML_CONFIG_FILE_PACKAGE_MESSAGE,
)
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Self


@dataclass(frozen=True)
class HareConfig(ConfigSection):
    """The configuration ``Hare.init()`` takes: the connections, the apps and the settings of the context."""

    connections: dict[str, ConnectionConfig | DBUrlConfig]
    apps: dict[str, AppConfig]
    routers: list[str | type] | None = None
    #: How long after a write the reads of the written connection's models stay on it, in
    #: seconds - None for the rest of the asyncio task (``hare.core.routing.Routing``).
    read_your_writes_seconds: float | None = None
    use_timezone: bool | None = None
    timezone: str | None = None
    #: The ``cli`` section - extra ``hare`` subcommands. Only the CLI reads it.
    cli: CliConfig | None = None
    #: Swappable model settings - setting name (``"USER_MODEL"``) to the ``"app_label.ModelName"``
    #: label of the model a relation declared with ``swappable(name)`` points at, or to a dict of
    #: branch name to such a label - the targets of a ``GenericForeignKeyField(swappable(name))``.
    swappable: dict[str, str | dict[str, str]] | None = None
    #: The ``migrations`` section - read by ``migrate``, ``makemigrations`` and ``checkmigrations``.
    migrations: MigrationsConfig | None = None

    def __post_init__(self) -> None:
        self._check_apps()
        if self.swappable is not None:
            self._check_swappable(self.swappable)

    def _check_apps(self) -> None:
        """Checks the apps name configured connections.

        Raises:
            ConfigurationError: An app names an unknown connection.
        """
        for name, app in self.apps.items():
            if app.default_connection and app.default_connection not in self.connections:
                raise ConfigurationError(f"App {name!r} refers to unknown connection {app.default_connection!r}")

    def _check_swappable(self, swappable: dict[str, str | dict[str, str]]) -> None:
        """Checks every ``swappable`` setting name and the model label it points at.

        Args:
            swappable: The ``swappable`` settings.

        Raises:
            ConfigurationError: A setting name isn't an upper-case identifier, a value is neither an
                ``"app_label.ModelName"`` label nor a non-empty dict of branch name to one, or a
                label's app isn't configured.
        """
        for setting, value in swappable.items():
            if not isinstance(setting, str) or not SWAPPABLE_SETTING_NAME_PATTERN.fullmatch(setting):
                raise ConfigurationError(
                    f'Swappable setting name {setting!r} must be an upper-case identifier such as "USER_MODEL"'
                )
            if isinstance(value, dict):
                if not value:
                    raise ConfigurationError(f"Swappable setting {setting!r} names no model")
                for branch_name, label in value.items():
                    if (
                        not isinstance(branch_name, str)
                        or not branch_name.isidentifier()
                        or keyword.iskeyword(branch_name)
                    ):
                        raise ConfigurationError(
                            f"Swappable setting {setting!r}: the branch name {branch_name!r} isn't a Python identifier"
                        )
                    self._check_swappable_label(setting, label)
            else:
                self._check_swappable_label(setting, value)

    def _check_swappable_label(self, setting: str, label: Any) -> None:
        """Checks a model label a ``swappable`` setting names.

        Args:
            setting: The setting name.
            label: The label.

        Raises:
            ConfigurationError: The label isn't ``"app_label.ModelName"``, or its app isn't configured.
        """
        if not isinstance(label, str) or not SWAPPABLE_MODEL_LABEL_PATTERN.fullmatch(label):
            raise ConfigurationError(
                f'Swappable setting {setting!r} must name a model as "app_label.ModelName", got {label!r}'
            )
        app_label = label.split(".", 1)[0]
        if app_label not in self.apps:
            raise ConfigurationError(
                f"Swappable setting {setting!r} points at {label!r}, but no app {app_label!r} is configured"
            )

    def to_dict(self) -> dict[str, Any]:
        """The configuration as the mapping ``Hare.init()`` takes - the settings left at None left out.

        Returns:
            The mapping.
        """
        connections = {name: connection_config.to_config() for name, connection_config in self.connections.items()}
        apps = {name: app.to_dict() for name, app in self.apps.items()}
        config: dict[str, Any] = {
            "connections": connections,
            "apps": apps,
        }
        if self.routers is not None:
            config["routers"] = self.routers
        if self.read_your_writes_seconds is not None:
            config["read_your_writes_seconds"] = self.read_your_writes_seconds
        if self.use_timezone is not None:
            config["use_timezone"] = self.use_timezone
        if self.timezone is not None:
            config["timezone"] = self.timezone
        if self.cli is not None:
            config["cli"] = self.cli.to_dict()
        if self.swappable is not None:
            config["swappable"] = {
                setting: dict(value) if isinstance(value, dict) else value for setting, value in self.swappable.items()
            }
        if self.migrations is not None:
            config["migrations"] = self.migrations.to_dict()
        return config

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Builds the configuration from its mapping form.

        Args:
            data: The mapping.

        Returns:
            The configuration.

        Raises:
            ConfigurationError: It, or a section, isn't a mapping, or a section is missing or unknown.
        """
        cls.check_known_keys(data, "the config")

        if "connections" not in data:
            raise ConfigurationError('Config must define "connections" section')
        if "apps" not in data:
            raise ConfigurationError('Config must define "apps" section')
        cls.raise_if_not_mapping(data["connections"], 'the "connections" section')
        cls.raise_if_not_mapping(data["apps"], 'the "apps" section')

        connections: dict[str, ConnectionConfig | DBUrlConfig] = {
            name: DBUrlConfig(connection_config)
            if isinstance(connection_config, str)
            else ConnectionConfig.from_dict(connection_config, f"connection {name!r}")
            for name, connection_config in data["connections"].items()
        }
        apps = {name: AppConfig.from_dict(app, f"app {name!r}") for name, app in data["apps"].items()}

        routers = data.get("routers")
        if routers is not None:
            routers = list(routers)

        cli_section = data.get("cli")
        cli = CliConfig.from_dict(cli_section) if cli_section is not None else None

        swappable = data.get("swappable")

        migrations_section = data.get("migrations")
        migrations = MigrationsConfig.from_dict(migrations_section) if migrations_section is not None else None

        return cls(
            connections=connections,
            apps=apps,
            routers=routers,
            read_your_writes_seconds=data.get("read_your_writes_seconds"),
            use_timezone=data.get("use_timezone"),
            timezone=data.get("timezone"),
            cli=cli,
            swappable=dict(swappable) if swappable is not None else None,
            migrations=migrations,
        )

    @classmethod
    def from_config_file(cls, config_file: str) -> Self:
        """Reads the configuration from a YAML or JSON file.

        Args:
            config_file: The path of the file - ``.yml``, ``.yaml`` or ``.json``.

        Returns:
            The configuration.

        Raises:
            ConfigurationError: The file is missing, unreadable or of another extension, its contents aren't a
                valid configuration, or a YAML file is read without PyYAML installed.
        """
        _, extension = os.path.splitext(config_file)
        extension = extension.lower()
        if extension in YAML_CONFIG_FILE_EXTENSIONS:
            try:
                import yaml
            except ImportError as error:
                raise ConfigurationError(YAML_CONFIG_FILE_PACKAGE_MESSAGE) from error

            try:
                with open(config_file, encoding="utf-8") as config_stream:
                    config = yaml.safe_load(config_stream)
            except FileNotFoundError as error:
                raise ConfigurationError(f"Config file not found: {config_file}") from error
            except OSError as error:
                raise ConfigurationError(f"Cannot read config file {config_file}: {error}") from error
            except UnicodeDecodeError as error:
                raise ConfigurationError(f"Config file {config_file} is not valid UTF-8: {error}") from error
            except yaml.YAMLError as error:
                raise ConfigurationError(f"Invalid YAML in config file {config_file}: {error}") from error
        elif extension in JSON_CONFIG_FILE_EXTENSIONS:
            try:
                with open(config_file, encoding="utf-8") as config_stream:
                    config = json.load(config_stream)
            except FileNotFoundError as error:
                raise ConfigurationError(f"Config file not found: {config_file}") from error
            except OSError as error:
                raise ConfigurationError(f"Cannot read config file {config_file}: {error}") from error
            except UnicodeDecodeError as error:
                raise ConfigurationError(f"Config file {config_file} is not valid UTF-8: {error}") from error
            except json.JSONDecodeError as error:
                raise ConfigurationError(f"Invalid JSON in config file {config_file}: {error}") from error
        else:
            supported = ", ".join(SUPPORTED_CONFIG_FILE_EXTENSIONS)
            raise ConfigurationError(f"Unknown config extension {extension}, only {supported} are supported")
        return cls.from_dict(config)

    @classmethod
    def from_db_url(cls, db_url: str, modules: Mapping[str, Iterable[str | ModuleType]]) -> Self:
        """Builds a configuration of one connection, ``default``, from its database URL.

        Args:
            db_url: The database URL.
            modules: The app labels, each to the modules (paths or module objects) holding its models.

        Returns:
            The configuration.

        Raises:
            ConfigurationError: The URL or the modules aren't valid.
        """
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
                module fails as it is imported.
        """
        module_path, _, variable_name = path.strip().rpartition(".")
        if not module_path or not variable_name:
            raise ConfigurationError(
                f"Invalid config path {path!r}: expected 'module.VARIABLE' (e.g. 'config.HARE_ORM') or the path "
                "of a .json/.yml file"
            )
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError as error:
            # The configuration module itself (or a package on its path) is missing - not a module it imports.
            if error.name is None or not (module_path == error.name or module_path.startswith(f"{error.name}.")):
                raise ConfigurationError(f"Configuration module {module_path!r} failed to import: {error}") from error
            raise ConfigurationError(
                f"Cannot import configuration module {module_path!r} - make sure it is on the Python path "
                f"(current working directory: {os.getcwd()}); a configuration file is named with one of the "
                f"extensions {', '.join(SUPPORTED_CONFIG_FILE_EXTENSIONS)}"
            ) from None
        except ConfigurationError:
            raise
        except Exception as error:
            # The module's own code failed - its error named, not a traceback through the loader.
            raise ConfigurationError(
                f"Configuration module {module_path!r} failed to import: {type(error).__name__}: {error}"
            ) from error
        value = getattr(module, variable_name, None)
        if value is None:
            available_names = [name for name in dir(module) if not name.startswith("_") and name.isupper()]
            hint = f" - available: {', '.join(available_names[:5])}" if available_names else ""
            raise ConfigurationError(f"Variable {variable_name!r} not found in module {module_path!r}{hint}")
        if isinstance(value, HareConfig):
            return cast("Self", value)
        return cls.from_dict(value)
