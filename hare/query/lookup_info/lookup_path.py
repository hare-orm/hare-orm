from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.cache import Cache
from hare.exceptions import FieldError

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model
    from hare.models.model_binding import ModelBinding


@dataclass(frozen=True, slots=True)
class LookupPath:
    """A ``relation__relation__field__...`` name read against a model: the relations it crosses
    from the start, and the segments left at the model they lead to - a field's name first, then
    whatever reads inside its value (a JSON key, an array item, a date part, a lookup).

    Attributes:
        relations: The relation fields crossed, in order.
        relation_names: Their names, in order.
        model: The model the relations lead to - the one the name starts at when there are none.
        rest: The segments after the relations.
    """

    #: A model's parsed names, in a bucket the model keeps: (name, crosses_last) -> the path. A
    #: path crosses relations into other models, so a change to any model drops them all.
    paths: ClassVar[Cache[Any]] = Cache(depends_on_other_models=True)

    relations: tuple[RelationalField[Model], ...]
    relation_names: tuple[str, ...]
    binding: ModelBinding
    rest: tuple[str, ...]

    @classmethod
    def parse(cls, binding: ModelBinding, name: str, *, crosses_last: bool = False) -> LookupPath:
        """Reads a name against a model, crossing relations as long as its segments name them.

        Kept per model and name (``paths``) until models change.

        Args:
            binding: The binding of the model the name starts at.
            name: The name.
            crosses_last: Whether the last segment is crossed too when it names a relation - a
                ``select_related()`` path - instead of being kept as the name of what is read.

        Returns:
            The path.
        """
        paths = LookupPath.paths.get_bucket(binding)
        cache_key = (name, crosses_last)
        cached_path = paths.get(cache_key)
        if cached_path is not None:
            return cached_path
        segments = name.split("__")
        crossable_count = len(segments) if crosses_last else len(segments) - 1
        relations: list[RelationalField[Model]] = []
        current_binding = binding
        position = 0
        while position < crossable_count and segments[position] in current_binding.fetch_fields:
            relation: RelationalField[Model] = current_binding.fields_map[segments[position]]  # type: ignore[assignment]
            relations.append(relation)
            current_binding = relation.get_related_binding()
            position += 1
        path = cls(tuple(relations), tuple(segments[:position]), current_binding, tuple(segments[position:]))
        return paths.store(cache_key, path)

    @staticmethod
    def check_select_related(model_binding: ModelBinding, relation_path: str) -> None:
        """Checks every hop of a ``select_related()`` path is a single-valued relation.

        Raises:
            FieldError: A hop is not found, is not a relation, or is a reverse ForeignKey /
                ManyToManyField (joining one would repeat the row once per related row).
        """
        lookup_path = LookupPath.parse(model_binding, relation_path, crosses_last=True)
        for position, (part, related_field) in enumerate(
            zip(lookup_path.relation_names, lookup_path.relations, strict=True)
        ):
            if related_field.is_multi_valued:
                source_binding = (
                    lookup_path.relations[position - 1].get_related_binding() if position else model_binding
                )
                raise FieldError(
                    f"select_related() can't follow {part!r} on {source_binding.full_name} - it can hold many "
                    "related rows (a reverse ForeignKey or a ManyToManyField); use prefetch_related() instead"
                )
        if lookup_path.rest:
            target_binding = lookup_path.binding
            part = lookup_path.rest[0]
            if part in target_binding.fields_map:
                raise FieldError(f"select_related() field {part!r} on {target_binding.full_name} is not a relation")
            raise FieldError(f"select_related() relation {part!r} for {target_binding.full_name} not found")

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
        return self.binding.fields_map.get(self.rest[0])

    @property
    def model(self) -> type[Model]:
        """The Python class represented by this structural binding."""
        return self.binding._model
