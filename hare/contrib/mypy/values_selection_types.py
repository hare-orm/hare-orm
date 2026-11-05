from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

from mypy.types import Instance, LiteralType, NoneType, TupleType, Type, TypedDictType, get_proper_type

from hare.contrib.mypy.constants import HIDDEN_SELECTION_ATTRIBUTE, STR_FULLNAME, TUPLE_FULLNAME
from hare.query.enums import RowShape
from hare.query.queryset.selection.values_selection import ValuesSelection

if TYPE_CHECKING:
    from hare.contrib.mypy.mypy_types import MypyTypes


class ValuesSelectionTypes:
    """Keep selected names and source paths alongside mypy rows, using serializable types.

    Row types alone cannot distinguish a flat composite key from a tuple projection or
    identify repeated positions and renamed annotations. Selection updates use the same
    ValuesSelection contract as runtime; extra attributes are copied, never mutated.
    """

    @staticmethod
    def get(receiver: Type) -> tuple[ValuesSelection, bool] | None:
        receiver = get_proper_type(receiver)
        if not isinstance(receiver, Instance) or receiver.extra_attrs is None:
            return None
        metadata = get_proper_type(receiver.extra_attrs.attrs.get(HIDDEN_SELECTION_ATTRIBUTE))
        if not isinstance(metadata, TupleType):
            return None
        shape, selection_mode, *names = [cast(str, cast(LiteralType, item).value) for item in metadata.items]
        if shape == RowShape.DICT:
            selection = ValuesSelection(RowShape.DICT, renamed_fields=tuple(zip(names[::2], names[1::2], strict=True)))
        else:
            selection = ValuesSelection(RowShape(shape), field_names=tuple(names))
        return selection, selection_mode == "all"

    @staticmethod
    def store(
        queryset: Instance, selection: ValuesSelection | None, types: MypyTypes, *, implicit_all: bool = False
    ) -> Instance:
        if selection is None:
            return queryset.copy_with_extra_attr(HIDDEN_SELECTION_ATTRIBUTE, NoneType())
        names = (
            [
                value
                for pair in (*((name, name) for name in selection.field_names), *selection.renamed_fields)
                for value in pair
            ]
            if selection.shape is RowShape.DICT
            else list(selection.field_names)
        )
        string_type = types.api.named_generic_type(STR_FULLNAME, [])
        metadata = TupleType(
            [
                LiteralType(value, string_type)
                for value in [selection.shape.value, "all" if implicit_all else "selected", *names]
            ],
            types.api.named_generic_type(TUPLE_FULLNAME, [types.any()]),
        )
        return queryset.copy_with_extra_attr(HIDDEN_SELECTION_ATTRIBUTE, metadata)

    @staticmethod
    def annotate(
        selection: ValuesSelection, row_type: Type, new_types: dict[str, Type], *, is_alias: bool, implicit_all: bool
    ) -> tuple[ValuesSelection, Type]:
        """Replace selected sources and append only newly selected annotation names."""
        row = get_proper_type(row_type)
        if implicit_all and is_alias:
            if selection.shape is RowShape.TUPLE and isinstance(row, TupleType):
                retained = [
                    (name, item)
                    for name, item in zip(selection.field_names, row.items, strict=True)
                    if name not in new_types
                ]
                selection = replace(selection, field_names=tuple(name for name, _ in retained))
                row = row.copy_modified(items=[item for _, item in retained])
            elif selection.shape is RowShape.DICT:
                selection = replace(
                    selection,
                    renamed_fields=tuple(
                        (name, path) for name, path in selection.renamed_fields if path not in new_types
                    ),
                )
        updated = selection.with_added_names(() if is_alias else new_types)
        if selection.shape is RowShape.FLAT:
            return updated, new_types.get(selection.field_names[0], row_type)
        if selection.shape is RowShape.TUPLE and isinstance(row, TupleType):
            items = [new_types.get(name, item) for name, item in zip(selection.field_names, row.items, strict=True)]
            items.extend(new_types[name] for name in updated.field_names[len(selection.field_names) :])
            return updated, row.copy_modified(items=items)
        if selection.shape is RowShape.DICT and isinstance(row, TypedDictType):
            named_items = {output: new_types.get(path, row.items[output]) for output, path in selection.renamed_fields}
            named_items.update(
                (output, new_types[path]) for output, path in updated.renamed_fields[len(selection.renamed_fields) :]
            )
            return updated, TypedDictType(named_items, set(named_items), set(), row.fallback)
        return updated, row_type
