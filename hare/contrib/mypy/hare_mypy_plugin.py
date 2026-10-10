from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from mypy.nodes import TypeInfo
from mypy.plugin import Plugin

from hare.contrib.mypy.annotation_types import AnnotationTypes
from hare.contrib.mypy.constants import (
    ANNOTATION_METHOD_NAMES,
    FILTER_METHOD_NAMES,
    MODEL_FULLNAME,
    NAME_METHOD_NAMES,
    QUERY_SPECIFICATION_FULLNAME,
    QUERYSET_FULLNAME,
    VALUES_METHOD_NAMES,
    WRITE_METHOD_NAMES,
)
from hare.contrib.mypy.field_names_check import FieldNamesCheck
from hare.contrib.mypy.filter_arguments_check import FilterArgumentsCheck
from hare.contrib.mypy.model_registry import ModelRegistry
from hare.contrib.mypy.mypy_types import MypyTypes
from hare.contrib.mypy.queryset_extension_methods import QuerySetExtensionMethods
from hare.contrib.mypy.values_row_types import ValuesRowTypes
from hare.contrib.mypy.write_arguments_check import WriteArgumentsCheck

if TYPE_CHECKING:  # pragma: nocoverage
    from mypy.options import Options
    from mypy.plugin import (
        ClassDefContext,
        FunctionSigContext,
        MethodContext,
        MethodSigContext,
        ReportConfigContext,
    )
    from mypy.types import FunctionLike, Type


class HareMypyPlugin(Plugin):
    """The mypy plugin of hare: checks the filter keys and values, the selected rows, the field names
    and the written values of queries against the project's models, bound without a database, and
    declares the QuerySet methods the dialects of the project's connections registered.

    Enabled with ``plugins = ["hare.contrib.mypy"]`` in mypy's configuration.

    Args:
        options: mypy's options.
    """

    def __init__(self, options: Options) -> None:
        super().__init__(options)
        self.model_registry = ModelRegistry(options)
        self.filter_arguments_check = FilterArgumentsCheck(self, self.model_registry)
        self.annotation_types = AnnotationTypes(self, self.model_registry)
        self.values_row_types = ValuesRowTypes(self, self.model_registry)
        self.field_names_check = FieldNamesCheck(self, self.model_registry)
        self.write_arguments_check = WriteArgumentsCheck(self, self.model_registry)
        self.queryset_extension_methods = QuerySetExtensionMethods(self.model_registry)

    @classmethod
    def for_version(cls, version: str) -> type[Plugin]:
        """The plugin class for a mypy version - what mypy calls the ``plugin`` entry point with.

        Args:
            version: mypy's version.

        Returns:
            The plugin class.
        """
        return cls

    def report_config_data(self, report_config_context: ReportConfigContext) -> Any:
        """What the checks read from the models - a change makes mypy check the modules anew.

        Args:
            report_config_context: The module mypy asks about.

        Returns:
            The digest.
        """
        self.model_registry.load()
        return self.model_registry.fingerprint

    def get_base_class_hook(self, fullname: str) -> Callable[[ClassDefContext], None] | None:
        if fullname == QUERY_SPECIFICATION_FULLNAME:
            return self.queryset_extension_methods.add_methods
        return None

    def get_method_signature_hook(self, fullname: str) -> Callable[[MethodSigContext], FunctionLike] | None:
        class_name, _, method_name = fullname.rpartition(".")
        if method_name in FILTER_METHOD_NAMES and self.is_subclass(class_name, QUERYSET_FULLNAME):
            return self.reporting_load_error(self.filter_arguments_check.get_signature, "default_signature")
        if method_name in WRITE_METHOD_NAMES and self.is_subclass(class_name, QUERYSET_FULLNAME):
            return self.reporting_load_error(
                partial(self.write_arguments_check.get_method_signature, method_name == "update"), "default_signature"
            )
        return None

    def get_method_hook(self, fullname: str) -> Callable[[MethodContext], Type] | None:
        class_name, _, method_name = fullname.rpartition(".")
        if not self.is_subclass(class_name, QUERYSET_FULLNAME):
            return None
        if method_name in ANNOTATION_METHOD_NAMES:
            return self.reporting_load_error(self.annotation_types.get_annotated_type, "default_return_type")
        if method_name in VALUES_METHOD_NAMES:
            return self.reporting_load_error(self.values_row_types.get_values_type, "default_return_type")
        if method_name in NAME_METHOD_NAMES:
            return self.reporting_load_error(self.field_names_check.check_names, "default_return_type")
        return None

    def get_function_signature_hook(self, fullname: str) -> Callable[[FunctionSigContext], FunctionLike] | None:
        if fullname != MODEL_FULLNAME and self.is_subclass(fullname, MODEL_FULLNAME):
            return self.reporting_load_error(
                partial(self.write_arguments_check.get_constructor_signature, fullname), "default_signature"
            )
        return None

    def is_subclass(self, class_name: str, base_fullname: str) -> bool:
        """Whether a class mypy knows is a subclass of another.

        Args:
            class_name: The full name of the class.
            base_fullname: The full name of the base class.

        Returns:
            Whether it is - False for a name that isn't a class.
        """
        symbol = self.lookup_fully_qualified(class_name)
        return symbol is not None and isinstance(symbol.node, TypeInfo) and symbol.node.has_base(base_fullname)

    def reporting_load_error(self, hook: Callable[[Any], Any], default_attribute: str) -> Callable[[Any], Any]:
        """A hook that reports why the models couldn't be loaded - once in each module - and leaves
        every call as mypy types it.

        Args:
            hook: The hook.
            default_attribute: The attribute of the hook's context holding mypy's own result.

        Returns:
            The hook.
        """

        def run_hook(hook_context: Any) -> Any:
            self.model_registry.load()
            if self.model_registry.load_error is None:
                return hook(hook_context)
            if hook_context.api.path not in self.model_registry.load_error_paths:
                self.model_registry.load_error_paths.add(hook_context.api.path)
                MypyTypes(self, hook_context.api, self.model_registry).fail(
                    self.model_registry.load_error, hook_context.context
                )
            return getattr(hook_context, default_attribute)

        return run_hook
