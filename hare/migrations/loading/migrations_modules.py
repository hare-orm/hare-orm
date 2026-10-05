from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

from hare.exceptions import ConfigurationError


class MigrationsModules:
    """Where each app keeps its migrations: the ``migrations`` module its configuration names,
    else the ``migrations`` package next to its models module."""

    @staticmethod
    def get_first_models_module(models: Iterable[ModuleType | str] | str | None) -> str | None:
        """The name of an app's first models module."""
        if isinstance(models, str):
            return models
        if not models:
            return None
        for item in models:
            if isinstance(item, str):
                return item
            if isinstance(item, ModuleType):
                return item.__name__
        return None

    @staticmethod
    def infer(models: Iterable[ModuleType | str] | str | None) -> str | None:
        """The migrations module next to an app's models: ``app.migrations`` for ``app.models``,
        ``migrations`` for a single-file top-level models module.

        Args:
            models: The app's models modules.

        Returns:
            The module name, None without a models module.
        """
        module = MigrationsModules.get_first_models_module(models)
        if not module:
            return None
        if module.endswith(".models"):
            base = module[: -len(".models")]
        elif "." in module:
            base = module.rsplit(".", 1)[0]
        elif MigrationsModules.is_plain_module(module):
            return "migrations"
        else:
            base = module
        return f"{base}.migrations"

    @staticmethod
    def is_plain_module(module_name: str) -> bool:
        """Whether an importable module is a single file rather than a package."""
        try:
            specification = importlib.util.find_spec(module_name)
        except (ImportError, ValueError):
            return False
        return (
            specification is not None
            and specification.submodule_search_locations is None
            and specification.origin not in {None, "built-in"}
        )

    @staticmethod
    def check_unique(apps_config: dict[str, dict[str, Any]]) -> None:
        """Refuses two apps keeping their migrations in one module - their files (both starting at
        ``0001_initial``) would overwrite each other.

        Args:
            apps_config: Every configured app, label -> app config dict.

        Raises:
            ConfigurationError: Two apps share a migrations module.
        """
        app_label_by_migrations_module: dict[str, str] = {}
        for label, app_config in apps_config.items():
            migrations_module = app_config.get("migrations") or MigrationsModules.infer(app_config.get("models"))
            if not migrations_module:
                continue
            other_label = app_label_by_migrations_module.get(migrations_module)
            if other_label is not None:
                raise ConfigurationError(
                    f'Apps "{other_label}" and "{label}" would both keep their migrations in "{migrations_module}" '
                    "(single-file models modules in the same directory share one inferred package) - set "
                    f'"migrations" explicitly for each app, e.g. "migrations_{other_label}" and "migrations_{label}"'
                )
            app_label_by_migrations_module[migrations_module] = label

    @staticmethod
    def get_apps_with_existing_modules(apps_config: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """The apps, each naming the migrations module it has - an inferred one only when it
        exists.

        Args:
            apps_config: Every configured app, label -> app config dict.

        Returns:
            The app configs, copied.

        Raises:
            ConfigurationError: Two apps share a migrations module.
        """
        MigrationsModules.check_unique(apps_config)
        apps_with_modules: dict[str, dict[str, Any]] = {}
        for label, config in apps_config.items():
            updated = dict(config)
            if "migrations" not in updated:
                inferred = MigrationsModules.infer(updated.get("models"))
                if inferred:
                    try:
                        if importlib.util.find_spec(inferred) is not None:
                            updated["migrations"] = inferred
                    except (ModuleNotFoundError, AttributeError, ValueError):
                        pass
            apps_with_modules[label] = updated
        return apps_with_modules

    @staticmethod
    def get_apps_with_packages(apps_config: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """The apps, each naming its migrations package - created on disk when missing, as a
        migration about to be written needs one.

        Args:
            apps_config: Every configured app, label -> app config dict.

        Returns:
            The app configs, copied.

        Raises:
            ConfigurationError: Two apps share a migrations module, or one can't be created.
        """
        MigrationsModules.check_unique(apps_config)
        apps_with_packages: dict[str, dict[str, Any]] = {}
        for label, config in apps_config.items():
            updated = dict(config)
            updated["migrations"], __ = MigrationsModules.ensure_package(label, updated)
            apps_with_packages[label] = updated
        return apps_with_packages

    @staticmethod
    def ensure_package(app_label: str, app_config: dict[str, Any]) -> tuple[str, Path]:
        """Creates an app's migrations package on disk when it doesn't exist yet.

        Args:
            app_label: The app.
            app_config: Its config.

        Returns:
            The package's module name and directory.

        Raises:
            ConfigurationError: The module can't be inferred, exists as a plain module, or its
                parent module can't be imported or located.
        """
        migrations_module = app_config.get("migrations") or MigrationsModules.infer(app_config.get("models"))
        if not migrations_module:
            raise ConfigurationError(
                f"Cannot infer migrations module for app {app_label}; set apps.{app_label}.migrations"
            )
        if "." not in migrations_module:
            specification = importlib.util.find_spec(migrations_module)
            if specification and specification.submodule_search_locations:
                return migrations_module, Path(next(iter(specification.submodule_search_locations)))
            if specification and specification.origin and specification.origin != "built-in":
                raise ConfigurationError(f"Migrations module {migrations_module} exists but is not a package")
            return migrations_module, MigrationsModules.create_package(Path.cwd() / migrations_module)
        parent_module_name, package_name = migrations_module.rsplit(".", 1)
        try:
            parent_module = importlib.import_module(parent_module_name)
        except ModuleNotFoundError as error:
            raise ConfigurationError(
                f"Cannot import parent module {parent_module_name} for app {app_label}: {error}"
            ) from None
        except (NameError, AttributeError, SyntaxError) as error:
            # Usually the app's own models module - a typo or broken syntax there.
            raise ConfigurationError(
                f"Module {parent_module_name} failed to import for app {app_label}: {error}"
            ) from None
        if hasattr(parent_module, "__path__"):
            parent_path = Path(next(iter(parent_module.__path__)))
        else:
            module_file = getattr(parent_module, "__file__", None)
            if not module_file:
                raise ConfigurationError(f"Cannot resolve filesystem path for module {parent_module_name}")
            parent_path = Path(module_file).parent
        return migrations_module, MigrationsModules.create_package(parent_path / package_name)

    @staticmethod
    def create_package(package_path: Path) -> Path:
        """Creates a package directory with its ``__init__.py`` - nothing what exists already.

        Args:
            package_path: The directory.

        Returns:
            The directory.
        """
        package_path.mkdir(parents=True, exist_ok=True)
        init_path = package_path / "__init__.py"
        if not init_path.exists():
            init_path.write_text("", encoding="utf-8")
        importlib.invalidate_caches()
        return package_path
