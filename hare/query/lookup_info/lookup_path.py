from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.caching.cache import Cache

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class LookupPath:
    """A ``relation__relation__field__...`` name read against a model: the relations it crosses
    from the start, and the segments left at the model they lead to - a field's name first, then
    whatever reads inside its value (a JSON key, an array item, a date part, a lookup).

    Attributes:
        start_model: The model the name starts at.
        relations: The relation fields crossed, in order.
        relation_names: Their names, in order.
        model: The model the relations lead to - ``start_model`` when there are none.
        rest: The segments after the relations.
    """

    #: A model's parsed names, in a bucket the model keeps: (name, crosses_last) -> the path. A
    #: path crosses relations into other models, so a change to any model drops them all.
    paths: ClassVar[Cache[Any]] = Cache(
        holds_sql=False, keyed_by_model=False, depends_on_other_models=True, model_attribute="lookup_paths"
    )

    start_model: type[Model]
    relations: tuple[RelationalField[Model], ...]
    relation_names: tuple[str, ...]
    model: type[Model]
    rest: tuple[str, ...]

    @classmethod
    def parse(cls, model: type[Model], name: str, *, crosses_last: bool = False) -> LookupPath:
        """Reads a name against a model, crossing relations as long as its segments name them.

        Kept per model and name (``paths``) until models change.

        Args:
            model: The model the name starts at.
            name: The name.
            crosses_last: Whether the last segment is crossed too when it names a relation - a
                ``select_related()`` path - instead of being kept as the name of what is read.

        Returns:
            The path.
        """
        paths: dict[tuple[str, bool], LookupPath] | None = model._meta.lookup_paths
        if paths is None:
            paths = LookupPath.paths.get_model_bucket(model)
        cache_key = (name, crosses_last)
        cached_path = paths.get(cache_key)
        if cached_path is not None:
            return cached_path
        segments = name.split("__")
        crossable_count = len(segments) if crosses_last else len(segments) - 1
        relations: list[RelationalField[Model]] = []
        current_model = model
        position = 0
        while position < crossable_count and segments[position] in current_model._meta.fetch_fields:
            relation = cast("RelationalField[Model]", current_model._meta.fields_map[segments[position]])
            relations.append(relation)
            current_model = relation.related_model
            position += 1
        path = cls(model, tuple(relations), tuple(segments[:position]), current_model, tuple(segments[position:]))
        paths[cache_key] = path
        return path

    @property
    def prefix(self) -> str:
        """The relation names as a name prefix - ``"event__tournament__"``, empty for none."""
        return "".join(f"{relation_name}__" for relation_name in self.relation_names)

    @property
    def field_name(self) -> str | None:
        """The name the relations end at - None when the name only crossed relations."""
        return self.rest[0] if self.rest else None

    def get_target_field(self) -> Field[Any] | None:
        """The field of the model the relations lead to that the name ends at, None when the
        name ends anywhere else (more segments follow, or it names no field)."""
        if len(self.rest) != 1:
            return None
        return self.model._meta.fields_map.get(self.rest[0])

    @property
    def crosses_to_many(self) -> bool:
        """Whether a relation crossed can hold many rows."""
        return any(relation.is_multi_valued for relation in self.relations)
