from __future__ import annotations

import importlib
import importlib.util
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

from hare.exceptions import ConfigurationError


class MigrationsModules:
    """Where each app keeps its migrations: the ``migrations`` module its configuration names,
    else the ``migrations`` package next to its models module."""

    @staticmethod
    def get_first_models_module(models: Iterable[ModuleType | str] | str | None) -> ModuleType | str | None:
        """Select the module used for both package naming and placement."""
        return models if isinstance(models, str) else next(iter(models or ()), None)

    @staticmethod
    def get_model_module_origin(model_module: ModuleType | str | None) -> Path | None:
        """The selected module's source, including module objects without a spec."""
        if isinstance(model_module, str):
            origin = getattr(sys.modules.get(model_module), "__file__", None)
            if origin is None:
                specification = importlib.util.find_spec(model_module)
                origin = None if specification is None else specification.origin
        else:
            origin = getattr(model_module, "__file__", None)
        return None if origin is None or origin in {"built-in", "frozen"} else Path(origin)

    @staticmethod
    def infer(models: Iterable[ModuleType | str] | str | None) -> str | None:
        """The migrations module next to an app's models: ``app.migrations`` for ``app.models``,
        ``migrations`` for a single-file top-level models module.

        Args:
            models: The app's models modules.

        Returns:
            The module name, None without a models module.
        """
        return MigrationsModules.get_inferred_name(MigrationsModules.get_first_models_module(models))

    @staticmethod
    def get_inferred_name(models_module: ModuleType | str | None) -> str | None:
        """The migrations package name for the selected models module."""
        module = models_module.__name__ if isinstance(models_module, ModuleType) else models_module
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
                    f"Apps {other_label!r} and {label!r} would both keep their migrations in {migrations_module!r} "
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
                        specification = importlib.util.find_spec(inferred)
                    except ModuleNotFoundError as error:
                        if error.name is None or not (inferred == error.name or inferred.startswith(error.name + ".")):
                            raise ConfigurationError(
                                f"Cannot locate migrations module {inferred} for app {label}: {error}"
                            ) from error
                    except Exception as error:
                        raise ConfigurationError(
                            f"Cannot locate migrations module {inferred} for app {label}: {error}"
                        ) from error
                    else:
                        if specification is not None:
                            updated["migrations"] = inferred
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
                parent module can't be imported or located, or its directory can't be created.
        """
        declared_module = app_config.get("migrations")
        models_module = MigrationsModules.get_first_models_module(app_config.get("models"))
        migrations_module = declared_module or MigrationsModules.get_inferred_name(models_module)
        if not migrations_module:
            raise ConfigurationError(
                f"Cannot infer migrations module for app {app_label}; set apps.{app_label}.migrations"
            )
        try:
            specification = importlib.util.find_spec(migrations_module)
        except Exception as error:
            raise ConfigurationError(
                f"Cannot locate migrations module {migrations_module} for app {app_label}: {error}"
            ) from None
        if specification is not None:
            if specification.submodule_search_locations:
                return migrations_module, Path(next(iter(specification.submodule_search_locations)))
            raise ConfigurationError(f"Migrations module {migrations_module} exists but is not a package")
        model_origin = MigrationsModules.get_model_module_origin(models_module)
        if "." not in migrations_module:
            base_path = Path.cwd()
            if not declared_module and model_origin is not None:
                base_path = model_origin.parent
            return migrations_module, MigrationsModules.create_package(base_path / migrations_module)
        parent_module_name, package_name = migrations_module.rsplit(".", 1)
        try:
            parent_module = importlib.import_module(parent_module_name)
        except ModuleNotFoundError as error:
            raise ConfigurationError(
                f"Cannot import parent module {parent_module_name} for app {app_label}: {error}"
            ) from None
        except Exception as error:
            # Usually the app's own models module - a typo or broken syntax there.
            raise ConfigurationError(
                f"Module {parent_module_name} failed to import for app {app_label}: {error}"
            ) from None
        if hasattr(parent_module, "__path__"):
            parent_paths = [Path(path) for path in parent_module.__path__]
            parent_path = next(
                (path for path in parent_paths if model_origin is not None and model_origin.is_relative_to(path)),
                parent_paths[0],
            )
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

        Raises:
            ConfigurationError: The filesystem cannot create the directory or initializer.
        """
        try:
            package_path.mkdir(parents=True, exist_ok=True)
            init_path = package_path / "__init__.py"
            if not init_path.exists():
                init_path.write_text("", encoding="utf-8")
        except OSError as error:
            raise ConfigurationError(f"Cannot create migrations package at {package_path}: {error}") from error
        importlib.invalidate_caches()
        return package_path
