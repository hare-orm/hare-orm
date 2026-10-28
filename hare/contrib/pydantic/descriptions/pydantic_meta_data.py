from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from pydantic import ConfigDict

if TYPE_CHECKING:
    from typing import Self


@dataclasses.dataclass
class PydanticMetaData:
    """The options of a model's Pydantic schemas - what its ``PydanticMeta`` declares, filled in by the defaults."""

    #: If not empty, only fields this property contains will be in the pydantic model
    include: tuple[str, ...] = ()

    #: Fields listed in this property will be excluded from pydantic model
    exclude: tuple[str, ...] = dataclasses.field(default_factory=lambda: ("Meta",))

    #: Computed fields can be listed here to use in pydantic model
    computed: tuple[str, ...] = dataclasses.field(default_factory=tuple)

    #: Values read from the row by name, with their types - a query's annotation, an attribute set on the row
    extra_fields: dict[str, Any] = dataclasses.field(default_factory=dict)

    #: Values of related rows, flattened: a name to the path of the related field
    related_values: dict[str, str] = dataclasses.field(default_factory=dict)

    #: Use backward relations without annotations - not recommended, it can be huge data
    #: without control
    backward_relations: bool = True

    #: Maximum recursion level allowed
    max_recursion: int = 3

    #: Whether relations may lead back to a model already in the schema - the data then repeats each cycle.
    #: Use it with ``exclude``/``include`` and a small ``max_recursion``.
    allow_cycles: bool = False

    #: If we should exclude raw fields (the ones have _id suffixes) of relations
    exclude_raw_fields: bool = True

    #: Sort fields alphabetically.
    #: If not set (or ``False``) then leave fields in declaration order
    sort_alphabetically: bool = False

    #: Allows user to specify custom config for generated model
    model_config: ConfigDict | None = None

    @staticmethod
    def _read_meta_fields(source: Any, fallback: PydanticMetaData) -> dict[str, Any]:
        """Read the 8 shared Meta fields from *source*, falling back to *fallback*'s own value
        for any attribute *source* doesn't define."""

        def get(attribute_name: str) -> Any:
            return getattr(source, attribute_name, getattr(fallback, attribute_name))

        return {
            "include": tuple(get("include")),
            "exclude": tuple(get("exclude")),
            "computed": tuple(get("computed")),
            "extra_fields": dict(get("extra_fields")),
            "related_values": dict(get("related_values")),
            "backward_relations": bool(get("backward_relations")),
            "max_recursion": int(get("max_recursion")),
            "allow_cycles": bool(get("allow_cycles")),
            "exclude_raw_fields": bool(get("exclude_raw_fields")),
            "sort_alphabetically": bool(get("sort_alphabetically")),
        }

    @classmethod
    def from_pydantic_meta(cls, old_pydantic_meta: Any) -> Self:
        default_meta = cls()
        model_config = getattr(old_pydantic_meta, "model_config", default_meta.model_config)
        return cls(**cls._read_meta_fields(old_pydantic_meta, default_meta), model_config=model_config)

    def construct_pydantic_meta(self, meta_override: type) -> PydanticMetaData:
        model_config = getattr(meta_override, "model_config", self.model_config)
        return PydanticMetaData(**self._read_meta_fields(meta_override, self), model_config=model_config)

    def selects(self, name: str) -> bool:
        """Whether ``include``/``exclude`` put the field ``name`` into the schema.

        A path into a relation in ``include`` (``"author.name"``) selects the relation itself.

        Args:
            name: The field's name on the model.

        Returns:
            ``True`` if the field goes into the schema.
        """
        if name in self.exclude:
            return False
        return not self.include or any(path == name or path.startswith(f"{name}.") for path in self.include)

    @property
    def model_computed(self) -> tuple[str, ...]:
        """The computed fields of the model itself - ``computed`` without paths into relations."""
        return tuple(name for name in self.computed if "." not in name)

    @property
    def model_extra_fields(self) -> dict[str, Any]:
        """The extra fields of the model itself - ``extra_fields`` without paths into relations."""
        return {name: annotation for name, annotation in self.extra_fields.items() if "." not in name}

    @property
    def model_related_values(self) -> dict[str, str]:
        """The related values of the model itself - ``related_values`` without paths into relations."""
        return {name: value_path for name, value_path in self.related_values.items() if "." not in name}

    def finalize_meta(
        self,
        exclude: tuple[str, ...] = (),
        include: tuple[str, ...] = (),
        computed: tuple[str, ...] = (),
        extra_fields: dict[str, Any] | None = None,
        related_values: dict[str, str] | None = None,
        allow_cycles: bool | None = None,
        sort_alphabetically: bool | None = None,
        max_recursion: int | None = None,
        model_config: ConfigDict | None = None,
    ) -> PydanticMetaData:
        """The options with the call's arguments applied - ``exclude``/``include``/``computed`` added to the
        meta's, the other arguments replacing its values when given.

        Args:
            exclude: The fields left out.
            include: The fields kept.
            computed: The computed fields.
            extra_fields: The extra fields - added to the meta's, replacing one of the same name.
            related_values: The related values - added to the meta's, replacing one of the same name.
            allow_cycles: Whether relations may lead back to a model in the schema.
            sort_alphabetically: Whether the fields are sorted by name.
            max_recursion: How deep relations are followed.
            model_config: The Pydantic config of the schema.

        Returns:
            The options.
        """
        _sort_fields: bool = self.sort_alphabetically if sort_alphabetically is None else sort_alphabetically
        _allow_cycles: bool = self.allow_cycles if allow_cycles is None else allow_cycles
        _max_recursion: int = self.max_recursion if max_recursion is None else max_recursion

        include = tuple(include) + self.include
        exclude = tuple(exclude) + self.exclude
        computed = tuple(computed) + self.computed

        _model_config = ConfigDict()
        if self.model_config:
            _model_config.update(self.model_config)
        if model_config:
            _model_config.update(model_config)

        return PydanticMetaData(
            include=include,
            exclude=exclude,
            computed=computed,
            extra_fields={**self.extra_fields, **(extra_fields or {})},
            related_values={**self.related_values, **(related_values or {})},
            backward_relations=self.backward_relations,
            max_recursion=_max_recursion,
            exclude_raw_fields=self.exclude_raw_fields,
            sort_alphabetically=_sort_fields,
            allow_cycles=_allow_cycles,
            model_config=_model_config,
        )
