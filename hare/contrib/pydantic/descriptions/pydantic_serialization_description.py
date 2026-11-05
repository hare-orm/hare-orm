from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, get_args

from hare.contrib.pydantic.descriptions.pydantic_field_source import PydanticFieldSource
from hare.contrib.pydantic_field_aliases import PydanticFieldAliases
from hare.core.caching.cache import Cache

if TYPE_CHECKING:
    from hare.contrib.pydantic.models.pydantic_model import PydanticModel


@dataclass(frozen=True, slots=True)
class PydanticSerializationDescription:
    """One completed schema's loaded-field checks and automatic relation fetch paths.

    Pydantic's resolved model fields supply annotations, including inherited fields.
    A recursive schema can validate an already loaded finite object tree, but has no
    finite automatic fetch plan: its ``fetch_fields`` is None. Descriptions follow
    the schema class's lifetime and are replaced after a Pydantic schema rebuild.
    """

    schema_generation: Any = field(repr=False, compare=False)
    field_sources: tuple[PydanticFieldSource, ...]
    fetch_fields: tuple[str, ...] | None

    descriptions: ClassVar[Cache[PydanticSerializationDescription]] = Cache(holds_sql=False)

    @classmethod
    def get(cls, schema: type[PydanticModel]) -> PydanticSerializationDescription:
        description = cls.descriptions.get_value(schema)
        if description is None or description.schema_generation is not schema.__pydantic_core_schema__:
            description = cls.build(schema, ())
            cls.descriptions.set_value(schema, description)
        return description

    @classmethod
    def build(
        cls, schema: type[PydanticModel], ancestors: tuple[type[PydanticModel], ...]
    ) -> PydanticSerializationDescription:
        model = schema.source_model
        sources: list[PydanticFieldSource] = []
        paths: list[str] = []
        finite = True
        if model is not None:
            for name, parameter in schema.model_fields.items():
                source = PydanticFieldSource(PydanticFieldAliases.get_paths(name, parameter, schema.model_config))
                sources.append(source)
                for input_path in source.paths:
                    targets = source.get_fetch_targets(model, input_path)
                    for relation_path, related_model in targets:
                        relation_name = "__".join(relation_path)
                        nested_schemas = tuple(
                            nested
                            for nested in cls.get_nested_schemas(parameter.annotation)
                            if len(targets) == 1 or nested.source_model is related_model
                        )
                        if not nested_schemas:
                            paths.append(relation_name)
                        for nested in nested_schemas:
                            if nested in (*ancestors, schema):
                                finite = False
                                continue
                            nested_description = cls.build(nested, (*ancestors, schema))
                            if nested_description.fetch_fields is None:
                                finite = False
                            elif nested_description.fetch_fields:
                                paths.extend(f"{relation_name}__{path}" for path in nested_description.fetch_fields)
                            else:
                                paths.append(relation_name)
            paths.extend("__".join(path.split(".")[:-1]) for path in schema.related_value_paths)
        return cls(
            schema.__pydantic_core_schema__,
            tuple(dict.fromkeys(sources)),
            tuple(dict.fromkeys(path for path in paths if path)) if finite else None,
        )

    @staticmethod
    def get_nested_schemas(annotation: Any) -> tuple[type[PydanticModel], ...]:
        # Local import: the model reads this description when validating an ORM instance.
        from hare.contrib.pydantic.models.pydantic_model import PydanticModel

        result = []
        pending = [annotation]
        while pending:
            candidate = pending.pop()
            if isinstance(candidate, type) and issubclass(candidate, PydanticModel):
                result.append(candidate)
            else:
                pending.extend(reversed(get_args(candidate)))
        return tuple(dict.fromkeys(result))
