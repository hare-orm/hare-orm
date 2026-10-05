from __future__ import annotations

import hashlib
import importlib
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from hare.cli.config_locator import ConfigLocator
from hare.contrib.mypy.constants import (
    CONFIG_NOT_SET_MESSAGE,
    MODELS_NOT_LOADED_MESSAGE,
    MYPY_IMPORTS_SETTING,
    MYPY_IMPORTS_TYPE_MESSAGE,
    PYPROJECT_FILE_NAME,
)
from hare.core.config import HareConfig
from hare.query.queryset.extensions.query_set_extensions import QuerySetExtensions
from hare.typing_info.bound_models import BoundModels

if TYPE_CHECKING:  # pragma: nocoverage
    from mypy.options import Options

    from hare.dialects.base.dialect import Dialect
    from hare.models import Model


class ModelRegistry:
    """The models of the project mypy checks, bound without a database: they are found the way the
    ``hare`` command finds them (``[tool.hare] hare_orm`` in ``pyproject.toml`` or the ``HARE_ORM``
    environment variable) and loaded once per mypy run.

    Args:
        options: mypy's options - the configuration file locates the project.
    """

    def __init__(self, options: Options) -> None:
        self.options = options
        #: The models by the full name of their class - None until loaded.
        self.models_by_fullname: dict[str, type[Model]] | None = None
        #: The models bound without a database - None until loaded.
        self.bound_models: BoundModels | None = None
        #: Why the models couldn't be loaded, None when they could.
        self.load_error: str | None = None
        #: The modules the load error was reported in - once in each.
        self.load_error_paths: set[str] = set()
        #: A digest of everything the checks read from the models - a change makes mypy check anew.
        self.fingerprint = ""

    def load(self) -> None:
        """Loads the models, once; a failure is kept in ``load_error``."""
        if self.models_by_fullname is not None:
            return
        self.models_by_fullname = {}
        try:
            self._load_models()
        except Exception as error:  # any failure of the project's code is reported to mypy
            self.load_error = MODELS_NOT_LOADED_MESSAGE.format(error=f"{type(error).__name__}: {error}")
            self.fingerprint = self.load_error

    def get_model(self, fullname: str) -> type[Model] | None:
        """The model of a class.

        Args:
            fullname: The full name of the class (``module.ClassName``).

        Returns:
            The model, None for a class that isn't a bound model.
        """
        self.load()
        return (self.models_by_fullname or {}).get(fullname)

    def get_dialect(self, model: type[Model]) -> Dialect | None:
        """The dialect of the connection a model's queries run on.

        Args:
            model: The model.

        Returns:
            The dialect, None when the model has no connection of the configuration.
        """
        return None if self.bound_models is None else self.bound_models.get_dialect(model)

    def _load_models(self) -> None:
        project_path = Path(self.options.config_file).resolve().parent if self.options.config_file else Path.cwd()
        if str(project_path) not in sys.path:
            sys.path.insert(0, str(project_path))
        pyproject_path = project_path / PYPROJECT_FILE_NAME
        config_source = ConfigLocator.locate(str(pyproject_path))
        if not config_source:
            raise ValueError(CONFIG_NOT_SET_MESSAGE)
        for module_name in self._get_mypy_imports(pyproject_path):
            importlib.import_module(module_name)
        self.bound_models = BoundModels(HareConfig.load(config_source))
        models = self.bound_models.models
        self.models_by_fullname = {BoundModels.get_fullname(model): model for model in models}
        self.fingerprint = self._get_fingerprint(config_source, self.bound_models)

    @staticmethod
    def _get_mypy_imports(pyproject_path: Path) -> list[str]:
        if not pyproject_path.exists():
            return []
        hare_section = tomllib.loads(pyproject_path.read_text("utf-8")).get("tool", {}).get("hare", {})
        module_names = hare_section.get(MYPY_IMPORTS_SETTING, [])
        if not isinstance(module_names, list) or not all(isinstance(name, str) for name in module_names):
            raise ValueError(MYPY_IMPORTS_TYPE_MESSAGE.format(file=pyproject_path, value=module_names))
        return module_names

    @staticmethod
    def _get_fingerprint(config_source: str, bound_models: BoundModels) -> str:
        descriptions: list[str] = [config_source]
        for connection_alias, dialect in sorted(bound_models.dialects_by_connection.items()):
            descriptions.append(f"{connection_alias}:{dialect.name}")
        for name, implementations in sorted(QuerySetExtensions.registered.items()):
            for dialect_name, implementation in sorted(implementations.items()):
                descriptions.append(f"{name}@{dialect_name}{implementation.get_call_signature()}")
        for model in bound_models.models:
            descriptions.append(f"{BoundModels.get_fullname(model)}@{model._meta.default_connection}")
            for field_name, field in model._meta.fields_map.items():
                related_model = getattr(field, "related_model", None)
                descriptions.append(
                    f"{field_name}:{BoundModels.get_fullname(type(field))}:{field.null}:{field.field_type!r}:"
                    f"{'' if related_model is None else BoundModels.get_fullname(related_model)}:"
                    f"{sorted(type(field).registered_lookups)}:{sorted(type(field).registered_transforms)}"
                )
        return hashlib.sha256("\n".join(descriptions).encode("utf-8")).hexdigest()
