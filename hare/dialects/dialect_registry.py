from __future__ import annotations

import importlib
import urllib.parse as urlparse
from typing import TYPE_CHECKING, cast

from hare.core.registries import Registries
from hare.dialects.constants import BUILTIN_DIALECTS_BY_NAME, BUILTIN_DRIVER_MODULES_BY_NAME, DIALECT_ENTRY_POINT_GROUP
from hare.exceptions import ConfigurationError
from hare.sql.constants import IDENTIFIER_LENGTH_LIMIT

if TYPE_CHECKING:
    from hare.dialects.base.connection.driver import Driver
    from hare.dialects.base.dialect import Dialect


class DialectRegistry:
    """Every dialect and driver hare can connect with.

    Each of hare's own dialects is registered when first used - by its driver, or a lookup of its
    name - so a connection to PostgreSQL loads nothing of SQLite and back. A driver is loaded when
    it is first looked up: one of hare's own by importing just its module, any other by importing
    every module the ``hare.dialects`` entry point group of an installed package names - each
    registers its drivers on import - and none of hare's own drivers it doesn't use. Listing
    every dialect or driver loads them all first, and so does a lookup of a name nothing
    registered yet, before it fails. hare's own dialects are listed first, in their own order,
    whichever was registered first.
    """

    drivers_by_name: dict[str, Driver] = {}
    drivers_by_url_scheme: dict[str, Driver] = {}
    dialects_by_name: dict[str, Dialect] = {}
    #: Importing every entry point module has begun.
    loading_installed_drivers: bool = False
    #: Loading hare's own drivers and every entry point module has begun.
    loading: bool = False
    #: hare's own drivers and every entry point module are loaded - from then on the full list of
    #: drivers may have been handed out.
    loaded: bool = False

    @classmethod
    def register_dialect(cls, dialect: Dialect) -> None:
        """Adds a dialect.

        Args:
            dialect: The dialect.

        Raises:
            ConfigurationError: If another dialect already has the name - one of hare's own
                included - or the dialect keeps names shorter than the ``IDENTIFIER_LENGTH_LIMIT``
                bytes hare generates.
        """
        if dialect.name in BUILTIN_DIALECTS_BY_NAME and dialect.name not in cls.dialects_by_name:
            # hare's own dialect keeps its name - registered first, so another one is refused.
            builtin_dialect = cls.get_builtin_dialect(dialect.name)
            if builtin_dialect is not dialect:
                cls.register_dialect(builtin_dialect)
        registered_dialect = cls.dialects_by_name.get(dialect.name)
        if registered_dialect is dialect:
            return
        if registered_dialect is not None:
            raise ConfigurationError(f"Another dialect named {dialect.name!r} is already registered")
        max_identifier_length = dialect.features.max_identifier_length
        if max_identifier_length is not None and max_identifier_length < IDENTIFIER_LENGTH_LIMIT:
            raise ConfigurationError(
                f"Dialect {dialect.name!r} keeps names within {max_identifier_length} bytes - hare generates "
                f"table, column, index, constraint and alias names up to {IDENTIFIER_LENGTH_LIMIT} bytes"
            )
        cls.dialects_by_name[dialect.name] = dialect
        dialect.install()
        Registries.changed()

    @classmethod
    def register_driver(cls, driver: Driver) -> None:
        """Adds a driver, and its dialect when that is new.

        Args:
            driver: The driver.

        Raises:
            ConfigurationError: If another driver already has the driver's name or one of its
                DB_URL schemes, or another dialect its dialect's name.
        """
        for builtin_name in (driver.name, *driver.url_schemes):
            # hare's own driver keeps its name and schemes - registered first, so another one is
            # refused whichever was imported first.
            if builtin_name not in cls.drivers_by_name and builtin_name not in cls.drivers_by_url_scheme:
                cls.load_builtin_driver(builtin_name)
        if driver.name in cls.drivers_by_name:
            raise ConfigurationError(f"A driver named {driver.name!r} is already registered")
        taken_schemes = [scheme for scheme in driver.url_schemes if scheme in cls.drivers_by_url_scheme]
        if taken_schemes:
            raise ConfigurationError(f"DB_URL schemes {taken_schemes} of driver {driver.name!r} are already taken")
        cls.register_dialect(driver.dialect)
        cls.drivers_by_name[driver.name] = driver
        for scheme in driver.url_schemes:
            cls.drivers_by_url_scheme[scheme] = driver
            if scheme not in urlparse.uses_netloc:
                urlparse.uses_netloc.append(scheme)
        # A new dialect already dropped the caches above. Before the full list of drivers is
        # loaded nothing has read it - one of hare's own drivers loaded on first use changes no
        # cache; a driver registered after it (see get_drivers()) may.
        if cls.loaded:
            Registries.changed()

    @staticmethod
    def get_builtin_dialect(name: str) -> Dialect:
        """hare's own dialect of a name, its module imported.

        Args:
            name: A name of ``BUILTIN_DIALECTS_BY_NAME``.

        Returns:
            The dialect.
        """
        module_name, attribute_name = BUILTIN_DIALECTS_BY_NAME[name]
        return cast("Dialect", getattr(importlib.import_module(module_name), attribute_name))

    @classmethod
    def load_builtin_dialects(cls) -> None:
        """Registers every one of hare's own dialects not registered yet."""
        for name in BUILTIN_DIALECTS_BY_NAME:
            if name not in cls.dialects_by_name:
                cls.register_dialect(cls.get_builtin_dialect(name))

    @classmethod
    def load(cls) -> None:
        """Registers hare's own dialects and drivers and those of installed packages, once."""
        if cls.loading:
            return
        cls.loading = True
        cls.load_builtin_dialects()
        for module_name in BUILTIN_DRIVER_MODULES_BY_NAME.values():
            importlib.import_module(module_name)
        cls.load_installed_drivers()
        cls.loaded = True

    @classmethod
    def load_installed_drivers(cls) -> None:
        """Imports every module the entry point group of an installed package names - each
        registers its drivers - once."""
        if cls.loading_installed_drivers:
            return
        cls.loading_installed_drivers = True
        # Imported here, not with hare - importlib.metadata takes milliseconds to import, and only
        # a name none of hare's own dialects and drivers has asks for the installed packages.
        from importlib.metadata import entry_points

        for entry_point in entry_points(group=DIALECT_ENTRY_POINT_GROUP):
            entry_point.load()

    @classmethod
    def load_builtin_driver(cls, name: str) -> None:
        """Imports the module of hare's own driver named ``name`` - which registers it - when
        there is one and nothing registered a driver of that name yet.

        Args:
            name: The driver's name or one of its DB_URL schemes.
        """
        module_name = BUILTIN_DRIVER_MODULES_BY_NAME.get(name)
        if module_name is not None:
            importlib.import_module(module_name)

    @classmethod
    def get_drivers(cls) -> tuple[Driver, ...]:
        """Every registered driver.

        Returns:
            The drivers.
        """
        cls.load()
        return tuple(cls.drivers_by_name.values())

    @classmethod
    def get_driver(cls, name: str) -> Driver:
        """The driver a connection config's ``engine`` names - by the driver's name or one of its
        DB_URL schemes, the same word a DB_URL of the connection would start with.

        Args:
            name: The driver's name or one of its DB_URL schemes.

        Returns:
            The driver.

        Raises:
            ConfigurationError: If no driver has the name.
        """
        driver = cls.drivers_by_name.get(name) or cls.drivers_by_url_scheme.get(name)
        if driver is None:
            cls.load_builtin_driver(name)
            driver = cls.drivers_by_name.get(name) or cls.drivers_by_url_scheme.get(name)
        if driver is None:
            cls.load_installed_drivers()
            driver = cls.drivers_by_name.get(name) or cls.drivers_by_url_scheme.get(name)
        if driver is None:
            cls.load()
            driver = cls.drivers_by_name.get(name) or cls.drivers_by_url_scheme.get(name)
        if driver is None:
            engines = sorted({*cls.drivers_by_name, *cls.drivers_by_url_scheme})
            raise ConfigurationError(f'Unknown database engine "{name}": expected one of {engines}')
        return driver

    @classmethod
    def find_driver_for_url_scheme(cls, scheme: str) -> Driver | None:
        """The driver a DB_URL scheme picks, None when no driver has the scheme.

        Args:
            scheme: The scheme.

        Returns:
            The driver, or None.
        """
        driver = cls.drivers_by_url_scheme.get(scheme)
        if driver is None:
            cls.load_builtin_driver(scheme)
            driver = cls.drivers_by_url_scheme.get(scheme)
        if driver is None:
            cls.load_installed_drivers()
            driver = cls.drivers_by_url_scheme.get(scheme)
        if driver is None:
            cls.load()
            driver = cls.drivers_by_url_scheme.get(scheme)
        return driver

    @classmethod
    def get_driver_for_url_scheme(cls, scheme: str) -> Driver:
        """The driver a DB_URL scheme picks.

        Args:
            scheme: The scheme.

        Returns:
            The driver.

        Raises:
            ConfigurationError: If no driver has the scheme.
        """
        driver = cls.find_driver_for_url_scheme(scheme)
        if driver is None:
            raise ConfigurationError(f"Unknown DB scheme: {scheme}")
        return driver

    @classmethod
    def get_dialects(cls) -> tuple[Dialect, ...]:
        """Every registered dialect.

        Returns:
            The dialects.
        """
        cls.load()
        builtin_dialects = [cls.dialects_by_name[name] for name in BUILTIN_DIALECTS_BY_NAME]
        return (
            *builtin_dialects,
            *(dialect for name, dialect in cls.dialects_by_name.items() if name not in BUILTIN_DIALECTS_BY_NAME),
        )

    @classmethod
    def get_declared_dialects(cls) -> tuple[Dialect, ...]:
        """Every dialect hare and the installed packages declare - hare's own drivers aren't imported
        for it, so asking costs no driver library's import.

        Returns:
            The dialects, hare's own first.
        """
        cls.load_builtin_dialects()
        cls.load_installed_drivers()
        builtin_dialects = [cls.dialects_by_name[name] for name in BUILTIN_DIALECTS_BY_NAME]
        return (
            *builtin_dialects,
            *(dialect for name, dialect in cls.dialects_by_name.items() if name not in BUILTIN_DIALECTS_BY_NAME),
        )

    @classmethod
    def get_dialect(cls, name: str | Dialect) -> Dialect:
        """The dialect of a name - a dialect itself is returned as it is (a connection's own, which
        may be a variant of the registered one).

        Args:
            name: The dialect's name, or the dialect.

        Returns:
            The dialect.

        Raises:
            ConfigurationError: If no dialect has the name.
        """
        if not isinstance(name, str):
            return name
        dialect = cls.dialects_by_name.get(name)
        if dialect is None and name in BUILTIN_DIALECTS_BY_NAME:
            cls.register_dialect(cls.get_builtin_dialect(name))
            dialect = cls.dialects_by_name.get(name)
        if dialect is None:
            cls.load_installed_drivers()
            dialect = cls.dialects_by_name.get(name)
        if dialect is None:
            cls.load()
            dialect = cls.dialects_by_name.get(name)
        if dialect is None:
            raise ConfigurationError(f"Unknown dialect {name!r}: expected one of {sorted(cls.dialects_by_name)}")
        return dialect
