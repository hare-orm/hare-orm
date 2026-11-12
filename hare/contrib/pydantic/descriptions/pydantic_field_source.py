from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hare.contrib.pydantic.messages import COLUMN_NOT_FETCHED, RELATED_ROWS_NOT_FETCHED, RELATION_NOT_FETCHED
from hare.exceptions import NoValuesFetched
from hare.fields.relations.relation_accessors import RelationAccessors
from hare.models import Model
from hare.query.queryset import QuerySet
from hare.query.queryset.single_rows.none_awaitable_type import NoneAwaitable


@dataclass(frozen=True, slots=True)
class PydanticFieldSource:
    """Ordered attribute paths a schema field can read from an ORM instance.

    Alias declarations are normalized through Pydantic's public alias objects.
    Loading checks follow the alternatives in order, allowing an available alias
    to supply a missing column. An existing but unfetched relation is an error,
    since Pydantic would select that value rather than try the next alternative.
    """

    paths: tuple[tuple[str | int, ...], ...]

    def check_loaded(self, obj: Model) -> None:
        """Refuse missing ORM data without implementing Pydantic value validation.

        Only paths starting from an ORM instance are inspected. Arbitrary mapping
        inputs continue directly to Pydantic. Missing non-ORM attributes, empty
        collections and null intermediate values retain Pydantic's normal behavior.
        """
        missing_column = None
        for path in self.paths:
            current: Any = obj
            for segment in path:
                model = current if isinstance(current, Model) else None
                field = model._meta.fields_map.get(segment) if model is not None and isinstance(segment, str) else None
                try:
                    value = (
                        getattr(current, segment)
                        if isinstance(segment, str) and not isinstance(current, Mapping)
                        else current[segment]
                    )
                except (AttributeError, KeyError, IndexError, TypeError):
                    if field is not None and field.has_db_field:
                        missing_column = segment
                    break
                if model is not None and isinstance(segment, str) and segment in model._meta.fetch_fields:
                    if field is not None and field.is_multi_valued:  # type: ignore[attr-defined]
                        if not RelationAccessors.get_relation(model, segment)._fetched:
                            raise NoValuesFetched(RELATED_ROWS_NOT_FETCHED.format(field=segment))
                    elif value is NoneAwaitable or isinstance(value, QuerySet):
                        raise NoValuesFetched(RELATION_NOT_FETCHED.format(field=segment))
                current = value
            else:
                return
        if missing_column is not None:
            raise NoValuesFetched(COLUMN_NOT_FETCHED.format(field=missing_column))

    @classmethod
    def get_fetch_targets(
        cls, model: type[Model], path: tuple[str | int, ...], prefix: tuple[str, ...] = ()
    ) -> tuple[tuple[tuple[str, ...], type[Model]], ...]:
        """Map attribute paths to relation prefixes, expanding generic relation branches.

        Integer collection indices do not change the relation prefetch path. A
        scalar or arbitrary attribute ends ORM traversal; its contents belong to
        Pydantic. Targets include the model reached by the last known relation so
        nested schemas can append their own fetch paths.
        """
        if not path:
            return ((prefix, model),) if prefix else ()
        name, *remaining = path
        if isinstance(name, int):
            return cls.get_fetch_targets(model, tuple(remaining), prefix)
        generic = model._meta.generic_foreign_key_fields.get(name)
        if generic is not None:
            return tuple(
                target
                for branch in generic.branch_names
                for target in cls.get_fetch_targets(model, (branch, *remaining), prefix)
            )
        if name in model._meta.fetch_fields:
            related = model._meta.fields_map[name].related_model  # type: ignore[attr-defined]
            return cls.get_fetch_targets(related, tuple(remaining), (*prefix, name))
        return ((prefix, model),) if prefix else ()
