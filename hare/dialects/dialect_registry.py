from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, cast

from hare.core.extensions.declarations import ExtensionContribution
from hare.core.extensions.extension_loader import ExtensionLoader
from hare.core.extensions.messages import EXTENSION_CYCLE_MESSAGE
from hare.core.log import logger
from hare.core.registries import Registries
from hare.dialects.constants import BUILTIN_DIALECTS_BY_NAME, BUILTIN_DRIVER_MODULES_BY_NAME, DIALECT_ENTRY_POINT_GROUP
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.dialects.base.connection.driver import Driver
    from hare.dialects.base.dialect import Dialect


class DialectRegistry:
    """Lazy access to atomically published dialect and driver declarations.

    Imports only define extension factories. A requested built-in driver loads its own module;
    installed packages are discovered when a name is unknown or enumeration is explicit.
    Failed factories are remembered until refresh_installed_drivers() is requested.
    """

    loading_installed_drivers: bool = False
    installed_drivers_loaded: bool = False
    installed_driver_errors: list[str] = []
    loading: bool = False
    loaded: bool = False
    extensions = ExtensionLoader()

    @classmethod
    def register_dialect(cls, dialect: Dialect) -> None:
        """Publish a dialect and all its field and query declarations atomically."""
        Registries.register(ExtensionContribution(dialects=(dialect,)))

    @classmethod
    def register_driver(cls, driver: Driver) -> None:
        """Publish a driver together with its dialect's declarations."""
        Registries.register(ExtensionContribution(drivers=(driver,)))

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
            if name not in Registries.snapshot.dialects:
                cls.register_dialect(cls.get_builtin_dialect(name))

    @classmethod
    def load(cls) -> None:
        """Explicitly enumerate built-in components and installed extension factories."""
        if cls.loaded:
            return
        if cls.loading:
            raise ConfigurationError(EXTENSION_CYCLE_MESSAGE.format(name="all dialects"))
        cls.loading = True
        try:
            cls.load_builtin_dialects()
            for name in BUILTIN_DRIVER_MODULES_BY_NAME:
                cls.load_builtin_driver(name)
            cls.load_installed_drivers()
            cls.loaded = True
        finally:
            cls.loading = False

    @classmethod
    def load_installed_drivers(cls) -> None:
        """Discover factories once; a failed package does not hide successful packages."""
        if cls.installed_drivers_loaded:
            return
        if cls.loading_installed_drivers:
            raise ConfigurationError(EXTENSION_CYCLE_MESSAGE.format(name=DIALECT_ENTRY_POINT_GROUP))
        from importlib.metadata import EntryPoint, entry_points

        cls.loading_installed_drivers = True
        try:
            for entry_point in entry_points(group=DIALECT_ENTRY_POINT_GROUP):
                name = f"{entry_point.name} = {entry_point.value}"
                try:

                    def load_contribution(entry: EntryPoint = entry_point) -> ExtensionContribution:
                        return cast("ExtensionContribution", entry.load()())

                    cls.extensions.load(name, load_contribution)
                except Exception as error:
                    failure = f"{name} ({error!r})"
                    cls.installed_driver_errors.append(failure)
                    logger.warning("Skipping the %r entry point %s", DIALECT_ENTRY_POINT_GROUP, failure)
            cls.installed_drivers_loaded = True
        finally:
            cls.loading_installed_drivers = False

    @classmethod
    def refresh_installed_drivers(cls) -> None:
        """Explicitly retry failed factories and discover newly installed extensions."""
        cls.extensions.clear_failures()
        cls.installed_driver_errors.clear()
        cls.installed_drivers_loaded = False
        cls.load_installed_drivers()

    @classmethod
    def get_installed_driver_errors_note(cls) -> str:
        """What an unknown scheme or engine's message adds about the entry points that failed to
        import - nothing when none did.

        Returns:
            The note.
        """
        if not cls.installed_driver_errors:
            return ""
        return f" - entry points that failed to import: {'; '.join(cls.installed_driver_errors)}"

    @classmethod
    def load_builtin_driver(cls, name: str) -> None:
        """Load only the built-in module associated with a requested name or URL scheme."""
        module_name = BUILTIN_DRIVER_MODULES_BY_NAME.get(name)
        if module_name is not None:
            cls.extensions.load(module_name, lambda: importlib.import_module(module_name).extension())

    @classmethod
    def get_drivers(cls) -> tuple[Driver, ...]:
        """Every registered driver.

        Returns:
            The drivers.
        """
        cls.load()
        return tuple(Registries.snapshot.drivers.values())

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
        driver = Registries.snapshot.drivers.get(name) or Registries.snapshot.schemes.get(name)
        if driver is None:
            cls.load_builtin_driver(name)
            driver = Registries.snapshot.drivers.get(name) or Registries.snapshot.schemes.get(name)
        if driver is None:
            cls.load_installed_drivers()
            driver = Registries.snapshot.drivers.get(name) or Registries.snapshot.schemes.get(name)
        if driver is None:
            cls.load()
            driver = Registries.snapshot.drivers.get(name) or Registries.snapshot.schemes.get(name)
        if driver is None:
            engines = sorted({*Registries.snapshot.drivers, *Registries.snapshot.schemes})
            raise ConfigurationError(
                f"Unknown database engine {name!r}: expected one of {engines}{cls.get_installed_driver_errors_note()}"
            )
        return driver

    @classmethod
    def find_driver_for_url_scheme(cls, scheme: str) -> Driver | None:
        """The driver a DB_URL scheme picks, None when no driver has the scheme - a scheme is matched
        whatever its case, as a URL's is.

        Args:
            scheme: The scheme.

        Returns:
            The driver, or None.
        """
        scheme = scheme.lower()
        driver = Registries.snapshot.schemes.get(scheme)
        if driver is None:
            cls.load_builtin_driver(scheme)
            driver = Registries.snapshot.schemes.get(scheme)
        if driver is None:
            cls.load_installed_drivers()
            driver = Registries.snapshot.schemes.get(scheme)
        if driver is None:
            cls.load()
            driver = Registries.snapshot.schemes.get(scheme)
        return driver

    @classmethod
    def get_url_schemes(cls) -> list[str]:
        """The DB_URL schemes of the registered drivers.

        Returns:
            The schemes, sorted.
        """
        cls.load()
        return sorted(Registries.snapshot.schemes)

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
            raise ConfigurationError(
                f"Unknown database URL scheme {scheme!r}: expected one of {cls.get_url_schemes()}"
                f"{cls.get_installed_driver_errors_note()}"
            )
        return driver

    @classmethod
    def get_dialects(cls) -> tuple[Dialect, ...]:
        """Every registered dialect.

        Returns:
            The dialects.
        """
        cls.load()
        builtin_dialects = [Registries.snapshot.dialects[name] for name in BUILTIN_DIALECTS_BY_NAME]
        return (
            *builtin_dialects,
            *(
                dialect
                for name, dialect in Registries.snapshot.dialects.items()
                if name not in BUILTIN_DIALECTS_BY_NAME
            ),
        )

    @classmethod
    def get_connected_dialects(cls) -> tuple[Dialect, ...]:
        """The registered dialects a driver connects to.

        Returns:
            The dialects, hare's own first, whatever order the drivers were registered in.
        """
        connected_dialect_names = {driver.dialect.name for driver in cls.get_drivers()}
        return tuple(dialect for dialect in cls.get_dialects() if dialect.name in connected_dialect_names)

    @classmethod
    def get_declared_dialects(cls) -> tuple[Dialect, ...]:
        """Every dialect hare and the installed packages declare - hare's own drivers aren't imported
        for it, so asking costs no driver library's import.

        Returns:
            The dialects, hare's own first.
        """
        cls.load_builtin_dialects()
        cls.load_installed_drivers()
        builtin_dialects = [Registries.snapshot.dialects[name] for name in BUILTIN_DIALECTS_BY_NAME]
        return (
            *builtin_dialects,
            *(
                dialect
                for name, dialect in Registries.snapshot.dialects.items()
                if name not in BUILTIN_DIALECTS_BY_NAME
            ),
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
        dialect = Registries.snapshot.dialects.get(name)
        if dialect is None and name in BUILTIN_DIALECTS_BY_NAME:
            cls.register_dialect(cls.get_builtin_dialect(name))
            dialect = Registries.snapshot.dialects.get(name)
        if dialect is None:
            cls.load_installed_drivers()
            dialect = Registries.snapshot.dialects.get(name)
        if dialect is None:
            cls.load()
            dialect = Registries.snapshot.dialects.get(name)
        if dialect is None:
            dialect_names = sorted(str(dialect_name) for dialect_name in Registries.snapshot.dialects)
            raise ConfigurationError(f"Unknown dialect {name!r}: expected one of {dialect_names}")
        return dialect
