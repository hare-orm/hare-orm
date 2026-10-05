from __future__ import annotations

import sys
import typing
from typing import Any

from hare.query.enums import LookupValueShape
from hare.stubs.stub_imports import StubImports
from hare.typing_info.constants import ALWAYS_ACCEPTED_VALUE_FULLNAMES, UNION_ORIGINS
from hare.typing_info.declarations import ValueType


class ValueTypeText:
    """The annotation a stub writes for a ``ValueType``.

    Args:
        imports: The stub's imports - the names the annotation uses are added.
    """

    def __init__(self, imports: StubImports) -> None:
        self.imports = imports

    def get_text(self, value_type: ValueType) -> str:
        """The annotation of a value - its shape and the expressions it takes included.

        Args:
            value_type: The value's type.

        Returns:
            The annotation.
        """
        item_text = self.get_item_text(value_type)
        if value_type.shape is LookupValueShape.LIST:
            text = f"{self.imports.add('collections.abc', 'Iterable')}[{item_text}]"
        elif value_type.shape is LookupValueShape.RANGE:
            bound_text = self.get_union([item_text, "None"])
            text = f"tuple[{bound_text}, {bound_text}] | list[{bound_text}]"
        else:
            text = item_text
        if value_type.accepts_expressions:
            accepted = [self.get_fullname_text(fullname) for fullname in ALWAYS_ACCEPTED_VALUE_FULLNAMES]
            text = self.get_union([text, *accepted])
        return text

    def get_item_text(self, value_type: ValueType) -> str:
        """The annotation of one value, without its shape."""
        if value_type.is_any:
            return self.imports.add("typing", "Any")
        if value_type.literals:
            literal = self.imports.add("typing", "Literal")
            return f"{literal}[{', '.join(repr(name) for name in value_type.literals)}]"
        items = [self.get_class_text(cls) for cls in value_type.classes]
        declared_in = value_type.declared_in
        field = None if declared_in is None else declared_in[0]._meta.fields_map[declared_in[1]]
        enum_type = getattr(field, "enum_type", None) if value_type.accepts_enum_values else None
        if enum_type is not None:
            items.insert(0, self.imports.add_class(enum_type))
        items.extend(self.imports.add_class(model) for model in value_type.models)
        if value_type.nullable:
            items.append("None")
        return self.get_union(items) if items else self.imports.add("typing", "Any")

    def get_class_text(self, cls: Any) -> str:
        """The annotation of a runtime class - a tuple of classes for a composite key's values, a
        parametrized type (``list[str]``, ``int | None``) for a container field's value; ``Any`` for
        a class no module holds (made at run time)."""
        if isinstance(cls, tuple):
            return f"tuple[{', '.join(self.get_class_text(item) for item in cls)}]"
        origin = typing.get_origin(cls)
        if origin is not None:
            argument_texts = [
                "..." if argument is Ellipsis else "None" if argument is type(None) else self.get_class_text(argument)
                for argument in typing.get_args(cls)
            ]
            if origin in UNION_ORIGINS:
                return self.get_union(argument_texts)
            return f"{self.get_class_text(origin)}[{', '.join(argument_texts)}]"
        if not isinstance(cls, type) or cls is object or not self.is_importable(cls):
            return self.imports.add("typing", "Any")
        return self.imports.add_class(cls)

    @staticmethod
    def is_importable(cls: type) -> bool:
        """Whether a class is the one its module holds under its qualified name."""
        holder: Any = sys.modules.get(cls.__module__)
        for name in cls.__qualname__.split("."):
            holder = getattr(holder, name, None)
        return holder is cls

    def get_fullname_text(self, fullname: str) -> str:
        """The annotation of a class named by its full name."""
        module_name, _, name = fullname.rpartition(".")
        return self.imports.add(module_name, name)

    @staticmethod
    def get_union(items: list[str]) -> str:
        """The union of annotations, each once, in order."""
        return " | ".join(dict.fromkeys(items))
