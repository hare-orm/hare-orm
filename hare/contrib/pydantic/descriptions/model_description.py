from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.fields import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from typing import Self

    from hare.models import Model


@dataclasses.dataclass
class ModelDescription:
    """A model's fields grouped by type, each group in declaration order - what the pydantic
    model creator builds a schema from. ``ModelDescription.from_model(Book)`` describes a model.
    """

    #: The model's primary key, as one Field per component - a single-column PK is a 1-item
    #: list, a composite PK (CompositePrimaryKey) is one entry per component in declared order.
    pk_fields: list[Field[Any]]
    #: Every field holding a plain value, except the primary key - the key columns of forward
    #: relations (``author_id``) included.
    data_fields: list[Field[Any]] = dataclasses.field(default_factory=list)
    #: Forward ``ForeignKeyField``s.
    fk_fields: list[Field[Any]] = dataclasses.field(default_factory=list)
    #: Backward sides of other models' ``ForeignKeyField``s pointing at this model.
    backward_fk_fields: list[Field[Any]] = dataclasses.field(default_factory=list)
    #: Forward ``OneToOneField``s.
    o2o_fields: list[Field[Any]] = dataclasses.field(default_factory=list)
    #: Backward sides of other models' ``OneToOneField``s pointing at this model.
    backward_o2o_fields: list[Field[Any]] = dataclasses.field(default_factory=list)
    #: ``ManyToManyField``s, both the declared and the generated reverse ones.
    m2m_fields: list[Field[Any]] = dataclasses.field(default_factory=list)

    @staticmethod
    def _fields_in(model: type[Model], names: Iterable[str]) -> list[Field[Any]]:
        return [field for name, field in model._meta.fields_map.items() if name in names]

    @classmethod
    def from_model(cls, model: type[Model]) -> Self:
        """Describes a model's fields.

        Args:
            model: The model; its relations must be initialised (``Hare.init()`` or
                ``Hare.bind_models()``).

        Returns:
            The description.
        """
        meta = model._meta
        # The primary key's fields - one or several - are listed here, not among the data fields.
        pk_attr_names = meta.pk_attr_names
        return cls(
            pk_fields=[meta.fields_map[name] for name in pk_attr_names],
            data_fields=cls._fields_in(model, meta.fields - meta.fetch_fields - set(pk_attr_names)),
            fk_fields=cls._fields_in(model, meta.fk_fields),
            backward_fk_fields=cls._fields_in(model, meta.backward_fk_fields),
            o2o_fields=cls._fields_in(model, meta.o2o_fields),
            backward_o2o_fields=cls._fields_in(model, meta.backward_o2o_fields),
            m2m_fields=cls._fields_in(model, meta.m2m_fields),
        )
