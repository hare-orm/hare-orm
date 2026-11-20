from __future__ import annotations

import hashlib
import importlib
import inspect
import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hare.cli.config_locator import ConfigLocator
from hare.contrib.mypy.constants import (
    CONFIG_NOT_SET_MESSAGE,
    MODELS_NOT_LOADED_MESSAGE,
    MYPY_IMPORTS_SETTING,
    PYPROJECT_FILE_NAME,
)
from hare.core.config import HareConfig
from hare.core.hare import Hare
from hare.exceptions import FieldError
from hare.fields.data.containers.container_field import ContainerField
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.models.write.rules.enums import WriteType
from hare.query.constants import PRIMARY_KEY_ALIAS
from hare.query.queryset.extensions.query_set_extensions import QuerySetExtensions
from hare.typing_info.bound_models import BoundModels
from hare.typing_info.filter_value_types import FilterValueTypes
from hare.typing_info.write_value_types import WriteValueTypes

if TYPE_CHECKING:
    from mypy.options import Options

    from hare.dialects.base.dialect import Dialect
    from hare.models.model_binding import ModelBinding
    from hare.typing_info.declarations import ValueType


class ModelRegistry:
    """The models of the project mypy checks, bound without a database: they are found the way the
    ``hare`` command finds them (``[tool.hare] hare_orm`` in ``pyproject.toml`` or the ``HARE_ORM``
    environment variable) and loaded once per mypy run.

    Args:
        options: mypy's options - the configuration file locates the project.
    """

    def __init__(self, options: Options) -> None:
        self.options = options
        #: The captured bindings by the full name of their class - None until loaded.
        self.bindings_by_fullname: dict[str, ModelBinding] | None = None
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
        if self.bindings_by_fullname is not None:
            return
        self.bindings_by_fullname = {}
        try:
            self._load_models()
        except Exception as error:  # any failure of the project's code is reported to mypy
            self.load_error = MODELS_NOT_LOADED_MESSAGE.format(error=f"{type(error).__name__}: {error}")
            self.fingerprint = self.load_error

    def get_binding(self, fullname: str) -> ModelBinding | None:
        """The offline registry's prepared node for this Python class name."""
        self.load()
        bindings = self.bindings_by_fullname
        return None if bindings is None else bindings.get(fullname)

    def get_dialect(self, binding: ModelBinding) -> Dialect | None:
        """The dialect of the connection a model's queries run on.

        Args:
            binding: The model.

        Returns:
            The dialect, None when the model has no connection of the configuration.
        """
        return None if self.bound_models is None else self.bound_models.get_dialect(binding)

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
        self.bound_models = BoundModels(Hare.bind_models(HareConfig.load(config_source)))
        self.bindings_by_fullname = {
            BoundModels.get_fullname(binding._model): binding for binding in self.bound_models.bindings
        }
        self.fingerprint = self._get_fingerprint(config_source, self.bound_models)

    @staticmethod
    def _get_mypy_imports(pyproject_path: Path) -> list[str]:
        if not pyproject_path.exists():
            return []
        hare_section = tomllib.loads(pyproject_path.read_text("utf-8")).get("tool", {}).get("hare", {})
        return hare_section.get(MYPY_IMPORTS_SETTING, [])

    @staticmethod
    def _get_fingerprint(config_source: str, bound_models: BoundModels) -> str:
        """Hash the typing contracts, without serializing defaults or other runtime values.

        ValueType descriptions already contain the input/output types read by the plugin.
        Relations are recorded as graph edges, not expanded to an arbitrary path depth;
        container fields additionally describe their children for indexed/key paths.
        """
        descriptions: list[str] = [config_source]
        for connection_alias, connection_dialect in sorted(bound_models.dialects_by_connection.items()):
            descriptions.append(f"{connection_alias}:{connection_dialect.name}")
        for name, implementations in sorted(QuerySetExtensions.get_registered().items()):
            for dialect_class, implementation in sorted(
                implementations.items(), key=lambda item: BoundModels.get_fullname(item[0])
            ):
                parameters = tuple(
                    (parameter.name, parameter.kind.name, parameter.default is inspect.Parameter.empty)
                    for parameter in implementation.get_call_signature().parameters.values()
                )
                descriptions.append(repr((name, BoundModels.get_fullname(dialect_class), parameters)))
        for meta in bound_models.bindings:
            model = meta._model
            dialect = bound_models.get_dialect(meta)
            descriptions.append(f"{BoundModels.get_fullname(model)}@{meta.default_connection}")
            descriptions.append(repr(meta.primary_key_attribute_names))
            for accepts_expressions, write_type in ((False, WriteType.INSERT), (True, WriteType.UPDATE)):
                descriptions.append(
                    repr(
                        tuple(
                            (name, ModelRegistry._get_value_type_description(value_type))
                            for name, value_type in WriteValueTypes.get_all(
                                meta, accepts_expressions=accepts_expressions, write_type=write_type
                            ).items()
                        )
                    )
                )
            for name, generic_field in sorted(meta.generic_foreign_key_fields.items()):
                branches = sorted(
                    (branch, BoundModels.get_fullname(target))
                    for target, branch in generic_field.branch_by_model.items()
                )
                descriptions.append(repr((name, generic_field.null, branches)))
            for field_name, field in sorted(meta.fields_map.items()):
                related_model = getattr(field, "related_model", None)
                descriptions.append(
                    repr(
                        (
                            field_name,
                            ModelRegistry._get_field_description(field),
                            None if related_model is None else BoundModels.get_fullname(related_model),
                        )
                    )
                )
            if dialect is None:
                continue
            for path in (*sorted(meta.fields_map), *((PRIMARY_KEY_ALIAS,) if meta.has_primary_key else ())):
                try:
                    lookups = meta.get_lookups(path, dialect)
                except FieldError:
                    # Some relation attributes cannot be compared as a whole.
                    continue
                for suffix, lookup in sorted(lookups.items()):
                    descriptions.append(
                        repr(
                            (
                                path,
                                suffix,
                                lookup.target,
                                lookup.transforms,
                                lookup.crosses_to_many,
                                lookup.requires_extension,
                                ModelRegistry._get_value_type_description(
                                    FilterValueTypes.get_filter_value_type(lookup)
                                ),
                                ModelRegistry._get_value_type_description(
                                    FilterValueTypes.get_selected_value_type(lookup)
                                ),
                            )
                        )
                    )
        return hashlib.sha256("\n".join(descriptions).encode("utf-8")).hexdigest()

    @staticmethod
    def _get_value_type_description(value_type: ValueType) -> tuple[ValueType, tuple[str, str | None, str] | None]:
        """Hash a declaration reference by its symbol, excluding the binding object's identity."""
        declared = value_type.declared_in
        symbol = (
            None if declared is None else (BoundModels.get_fullname(declared[0]._model), declared[0].app, declared[1])
        )
        return replace(value_type, declared_in=None), symbol

    @staticmethod
    def _get_field_description(field: Field[Any]) -> tuple[Any, ...]:
        """Describe field and extension types even inside containers with unbounded paths.

        Callback identities and SQL bodies are irrelevant to typing. Registered lookups
        contribute their declared inputs; transforms contribute their output field types.
        """
        effective_field = GeneratedField.get_effective_field(field)
        if effective_field is not field:
            return (
                BoundModels.get_fullname(type(field)),
                field.null,
                ModelRegistry._get_field_description(effective_field),
            )
        lookups = {}
        transforms = {}
        for field_class in reversed(type(field).__mro__):
            lookups.update(Field.get_class_lookups(field_class))
            transforms.update(Field.get_class_transforms(field_class))
        outputs = []
        for name, transform in sorted(transforms.items()):
            output = transform.get_transform(field)[1]
            outputs.append(
                (
                    name,
                    transform.required_extension,
                    BoundModels.get_fullname(type(output)),
                    output.field_type,
                    output.get_python_type(),
                    output.null,
                    output.get_accepted_value_types(),
                    None if output.supported_lookups is None else tuple(sorted(output.supported_lookups)),
                )
            )
        return (
            BoundModels.get_fullname(type(field)),
            field.field_type,
            field.get_python_type(),
            field.null,
            field.get_accepted_value_types(),
            None if field.supported_lookups is None else tuple(sorted(field.supported_lookups)),
            tuple(
                (name, lookup.value_shape, lookup.value_type, lookup.required_extension)
                for name, lookup in sorted(lookups.items())
            ),
            tuple(outputs),
            tuple((step, ModelRegistry._get_field_description(child)) for step, child in field.get_held_steps())
            if isinstance(field, ContainerField)
            else (),
        )
