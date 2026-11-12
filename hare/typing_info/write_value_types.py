from __future__ import annotations

from typing import TYPE_CHECKING, Any, Union

from hare.models.write.rules.enums import WriteType
from hare.models.write.rules.row_write_rules import RowWriteRules
from hare.query.constants import PRIMARY_KEY_ALIAS
from hare.typing_info.declarations import ValueType

if TYPE_CHECKING:
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model


class WriteValueTypes:
    """Describe writable names and their values independently of a type checker.

    Column inputs include the field's documented conversions; relations accept
    model instances, and generic relations also accept their tagged dictionaries.
    Reverse and to-many relations are excluded because they use relation managers.
    """

    @staticmethod
    def get_names(model: type[Model]) -> tuple[str, ...]:
        """Keep declaration order so all typing backends produce deterministic output."""
        meta = model._meta
        relations = meta.foreign_key_fields | meta.one_to_one_fields
        return tuple(
            dict.fromkeys(
                (
                    *meta.fields_db_projection,
                    *(name for name in meta.fields_map if name in relations),
                    *meta.generic_foreign_key_fields,
                    *((PRIMARY_KEY_ALIAS,) if meta.has_primary_key else ()),
                )
            )
        )

    @staticmethod
    def get(
        model: type[Model],
        name: str,
        *,
        accepts_expressions: bool = False,
        write_type: WriteType = WriteType.INSERT,
    ) -> ValueType | None:
        """Return a write contract, or None when the name cannot be assigned directly.

        Constructor/create descriptions retain their input names. UPDATE additionally
        applies the runtime assignment policy to every column a name expands into.
        """
        meta = model._meta
        if write_type is not WriteType.INSERT:
            if name not in WriteValueTypes.get_names(model):
                return None
            names = (
                (name,)
                if name == PRIMARY_KEY_ALIAS
                else RowWriteRules.get_stored_field_names(model, [name], ".update()")
            )
            if any(
                RowWriteRules.get_refusal_reason(model, column, write_type, ".update()", finds_rows_by_key=False)
                is not None
                for column in names
            ):
                return None
        if name == PRIMARY_KEY_ALIAS:
            if not meta.has_primary_key:
                return None
            if meta.has_composite_primary_key:
                values = tuple(
                    Union[(column.field_type, *column.get_accepted_value_types())] for column in meta.pk_fields
                )
                return ValueType(classes=(values,), accepts_expressions=accepts_expressions)
            name = meta.primary_key_attribute  # type: ignore[assignment]
        if name in meta.generic_foreign_key_fields:
            field = meta.generic_foreign_key_fields[name]
            return ValueType(
                classes=(dict,),
                models=tuple(field.branch_by_model),
                nullable=field.null,
                accepts_expressions=accepts_expressions,
            )
        if name in meta.foreign_key_fields or name in meta.one_to_one_fields:
            relation: RelationalField[Any] = meta.fields_map[name]  # type: ignore[assignment]
            return ValueType(
                models=(relation.related_model,),
                nullable=relation.null,
                accepts_expressions=accepts_expressions,
            )
        if name not in meta.fields_db_projection:
            return None
        column = meta.fields_map[name]
        return ValueType(
            classes=(column.field_type,),
            accepted_classes=column.get_accepted_value_types(),
            declared_in=(model, name),
            accepts_enum_values=True,
            nullable=column.null,
            accepts_expressions=accepts_expressions,
        )

    @classmethod
    def get_all(
        cls,
        model: type[Model],
        *,
        accepts_expressions: bool = False,
        write_type: WriteType = WriteType.INSERT,
    ) -> dict[str, ValueType]:
        return {
            name: description
            for name in cls.get_names(model)
            if (description := cls.get(model, name, accepts_expressions=accepts_expressions, write_type=write_type))
            is not None
        }
