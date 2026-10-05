from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.caching.cache import Cache
from hare.exceptions import QueryError
from hare.query.lookup_info.lookup_path import LookupPath

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model


class ConcreteFieldPaths:
    """The field paths a path reads: a trailing ``pk`` reads the primary key field(s), a trailing
    forward relation its key field(s) (``dept`` reads ``dept_id``), a trailing to-many or reverse
    one-to-one relation the related model's primary key field(s)."""

    #: Per model, path -> the field paths it reads. A path crosses relations into other models, so a
    #: change of any model forgets every entry.
    paths: ClassVar[Cache[Any]] = Cache(
        holds_sql=False, keyed_by_model=False, depends_on_other_models=True, model_attribute="concrete_field_paths"
    )

    @staticmethod
    def get_paths(model: type[Model], field_path: str) -> tuple[str, ...]:
        """The field paths a path reads.

        Args:
            model: The model the path starts from.
            field_path: A ``field``/``relation__field`` path.

        Returns:
            The paths read - the path itself when it names a field or isn't a model field.
        """
        paths: dict[str, tuple[str, ...]] | None = model._meta.concrete_field_paths
        if paths is None:
            paths = ConcreteFieldPaths.paths.get_model_bucket(model)
        concrete_field_paths = paths.get(field_path)
        if concrete_field_paths is None:
            concrete_field_paths = paths[field_path] = ConcreteFieldPaths.read_paths(model, field_path)
        return concrete_field_paths

    @staticmethod
    def read_paths(model: type[Model], field_path: str) -> tuple[str, ...]:
        """Reads the field paths a path reads - see ``get_paths()``.

        Args:
            model: The model the path starts from.
            field_path: A ``field``/``relation__field`` path.

        Returns:
            The paths read.
        """
        lookup_path = LookupPath.parse(model, field_path)
        if len(lookup_path.rest) != 1:
            return (field_path,)
        last_name = lookup_path.rest[0]
        meta = lookup_path.model._meta
        path_prefix = lookup_path.prefix
        if last_name == "pk" and last_name not in meta.fields_map:
            return tuple(
                f"{path_prefix}{primary_key_attribute_name}"
                for primary_key_attribute_name in meta.primary_key_attribute_names
            )
        if last_name not in meta.fetch_fields:
            return (field_path,)
        relation = cast("RelationalField[Model]", meta.fields_map[last_name])
        if last_name in meta.foreign_key_fields or last_name in meta.one_to_one_fields:
            return tuple(f"{path_prefix}{source_field_name}" for source_field_name in relation.source_fields)
        related_meta = relation.related_model._meta
        if not related_meta.has_primary_key:
            # A related model without a primary key is read by its key column to this row - never
            # NULL in a joined row, like a primary key.
            backward_relation = cast("BackwardForeignKeyRelation[Model]", relation)
            source_field_names = [
                related_meta.fields_db_projection_reverse.get(column, column)
                for column in backward_relation.relation_source_fields
            ]
            return tuple(f"{path_prefix}{last_name}__{source_field_name}" for source_field_name in source_field_names)
        return tuple(
            f"{path_prefix}{last_name}__{primary_key_attribute_name}"
            for primary_key_attribute_name in related_meta.primary_key_attribute_names
        )

    @staticmethod
    def get_path(model: type[Model], field_path: str) -> str:
        """The one field path a path reads - see ``get_paths()``.

        Args:
            model: The model the path starts from.
            field_path: A ``field``/``relation__field`` path.

        Returns:
            The path read.

        Raises:
            QueryError: The path reads a composite primary key or a composite foreign key - there's no
                single column.
        """
        concrete_field_paths = ConcreteFieldPaths.get_paths(model, field_path)
        if len(concrete_field_paths) != 1:
            component_names = ", ".join(repr(path) for path in concrete_field_paths)
            raise QueryError(
                f"'{field_path}' reads a composite primary key (or a foreign key to one) - there's no single "
                f"column to read. Name its components instead: {component_names}."
            )
        return concrete_field_paths[0]
