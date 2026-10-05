from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.core.config import HareConfig
from hare.core.config.connection_config import ConnectionConfig
from hare.core.config.db_url_config import DBUrlConfig
from hare.core.hare import Hare
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.dialect_registry import DialectRegistry

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model


class BoundModels:
    """The models of a configuration, bound without a database, and the dialect of each connection -
    what the typing tools (the mypy plugin, ``hare stubs``) read the models through.

    Args:
        config: The configuration.
    """

    def __init__(self, config: HareConfig) -> None:
        apps = Hare.bind_models(config)
        #: The models, sorted by their full name.
        self.models: list[type[Model]] = sorted(apps.get_models_iterable(), key=BoundModels.get_fullname)
        #: The dialect of each connection of the configuration.
        self.dialects_by_connection: dict[str, Dialect] = {
            connection_alias: DialectRegistry.get_driver(BoundModels.get_engine(connection_config)).dialect
            for connection_alias, connection_config in config.connections.items()
        }

    def get_dialect(self, model: type[Model]) -> Dialect | None:
        """The dialect of the connection a model's queries run on.

        Args:
            model: The model.

        Returns:
            The dialect, None when the model has no connection of the configuration.
        """
        connection_alias = model._meta.default_connection
        return None if connection_alias is None else self.dialects_by_connection.get(connection_alias)

    @staticmethod
    def get_fullname(named_class: type[Any]) -> str:
        """The full name of a class the way the type checkers name it.

        Args:
            named_class: The class.

        Returns:
            ``module.QualifiedName``.
        """
        return f"{named_class.__module__}.{named_class.__qualname__}"

    @staticmethod
    def get_engine(connection_config: ConnectionConfig | DBUrlConfig) -> str:
        """The driver a connection of the configuration names.

        Args:
            connection_config: The connection.

        Returns:
            The driver's name.
        """
        if isinstance(connection_config, DBUrlConfig):
            return str(DbUrlConfigGenerator.expand(connection_config.url)["engine"])
        if connection_config.db_url is not None:
            return str(DbUrlConfigGenerator.expand(connection_config.db_url)["engine"])
        return str(connection_config.engine)
