"""Hare models in Litestar's DTOs: ``HareDTO`` reads and writes a model's rows, and
``HarePlugin`` gives each handler returning or taking hare models one - Litestar's own way to shape
a response, with its OpenAPI schema."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Generator
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from litestar.dto import DTOField, Mark
from litestar.dto.base_dto import AbstractDTO
from litestar.dto.data_structures import DTOFieldDefinition
from litestar.types.empty import Empty
from litestar.typing import FieldDefinition

from hare.contrib.frameworks.litestar.constants import DTO_CHOICE_PLACE, DTO_PATH_SEPARATOR, TO_MANY_RELATION_TYPES
from hare.exceptions import NoValuesFetched
from hare.models import Model
from hare.query.queryset import QuerySet, RelatedQuerySet

if TYPE_CHECKING:  # pragma: nocoverage
    from litestar.dto._backend import DTOBackend

    from hare.fields.base.field import Field


ModelType = TypeVar("ModelType", bound=Model)


class HareDTO(AbstractDTO[ModelType], Generic[ModelType]):
    """A Litestar DTO of a hare model::

        class BookDTO(HareDTO[Book]):
            config = DTOConfig(exclude={"published_at"}, rename_strategy="camel")

        @get("/books", return_dto=BookDTO)
        async def list_books(books: NamedDependency[BookQuery]) -> Page[Book]:
            return await books.page()

    Its fields are the model's fields: each value, a relation's key column (``author_id``), and
    each relation as the related rows - ``null`` unless the query loaded them
    (``select_related()``, ``prefetch_related()``, a request query's ``include``), so a response
    never runs a query of its own. Relations and the values the database generates are read-only:
    a request's data sets the key column (``author_id``), not the relation. ``DTOConfig`` shapes
    it as for any Litestar DTO - ``exclude``, ``include``, ``rename_fields``,
    ``rename_strategy``, ``max_nested_depth`` (1 by default: the related rows without their own
    relations). A field of related rows is named by its path through the relations:
    ``exclude={"author.books", "tags.id"}``.
    """

    @classmethod
    def create_for_field_definition(
        cls, field_definition: FieldDefinition, handler_id: str, backend_cls: type[DTOBackend] | None = None
    ) -> None:
        cls.config = dataclasses.replace(
            cls.config,
            exclude={cls.get_litestar_path(cls.model_type, path) for path in cls.config.exclude},
            include={cls.get_litestar_path(cls.model_type, path) for path in cls.config.include},
            rename_fields={
                cls.get_litestar_path(cls.model_type, path): name for path, name in cls.config.rename_fields.items()
            },
        )
        super().create_for_field_definition(field_definition, handler_id, backend_cls)

    def data_to_encodable_type(self, data: Any) -> Any:
        """Encodes a handler's response - ``null`` for no row, as from a handler returning
        ``Book | None``.

        Args:
            data: A row, rows, a page of rows or None.

        Returns:
            What Litestar encodes.
        """
        if data is None:
            return None
        return super().data_to_encodable_type(data)

    @staticmethod
    def get_litestar_path(model_type: type[Model], path: str) -> str:
        """A field's path through the relations as Litestar names it: a relation holds its row or
        ``null``, so Litestar goes through the row's place in that choice - ``author.name`` is
        ``author.0.name`` - and a list of rows adds the row's place in the list - ``tags.id`` is
        ``tags.0.0.id``. The models are bound by the time a handler's DTO is built.

        Args:
            model_type: The model the path starts at.
            path: The path, fields joined with dots.

        Returns:
            Litestar's path - the same for a path already written so.
        """
        parts = path.split(DTO_PATH_SEPARATOR)
        litestar_parts: list[str] = []
        current_model: type[Model] | None = model_type
        for index, part in enumerate(parts):
            litestar_parts.append(part)
            field = None if current_model is None else current_model._meta.fields_map.get(part)
            next_part = parts[index + 1] if index + 1 < len(parts) else None
            if field is None or field.relation_type is None or next_part in (None, DTO_CHOICE_PLACE):
                current_model = None
                continue
            litestar_parts.append(DTO_CHOICE_PLACE)
            if field.relation_type in TO_MANY_RELATION_TYPES:
                litestar_parts.append(DTO_CHOICE_PLACE)
            current_model = field.related_model  # type: ignore[attr-defined]
        return DTO_PATH_SEPARATOR.join(litestar_parts)

    @staticmethod
    def attribute_accessor(row: object, name: str) -> Any:
        """Reads a row's attribute for Litestar - ``None`` for a relation the query didn't load.

        Args:
            row: A model instance.
            name: The attribute.

        Returns:
            The attribute's value; the related row, the list of related rows, or None for a
            relation.
        """
        value = getattr(row, name)
        if isinstance(value, RelatedQuerySet):
            try:
                return list(value)
            except NoValuesFetched:
                return None
        if isinstance(value, QuerySet):
            return None
        return value

    @classmethod
    def generate_field_definitions(cls, model_type: type[Model]) -> Generator[DTOFieldDefinition]:
        for name, field in model_type._meta.fields_map.items():
            yield cls.get_field_definition(model_type, name, field)

    @classmethod
    def get_field_definition(cls, model_type: type[Model], name: str, field: Field[Any]) -> DTOFieldDefinition:
        """The DTO field of one model field.

        Args:
            model_type: The model.
            name: The field's name.
            field: The field.

        Returns:
            The DTO field: the related model, a list of it or None for a relation; the field's
            enum or value type, None when nullable, for a value.
        """
        relation_type = field.relation_type
        default: Any = Empty
        default_factory: Callable[[], Any] | None = None
        read_only = relation_type is not None or field.generated
        annotation = field.get_annotation()
        if relation_type is not None:
            # A relation that wasn't loaded is left out.
            annotation = annotation | None
            default = None
        else:
            if field.null:
                default = None
            if callable(field.default):
                default, default_factory = Empty, field.default
            elif field.default is not None:
                default = field.default
        field_definition = FieldDefinition.from_kwarg(annotation=annotation, name=name, default=default)
        return DTOFieldDefinition.from_field_definition(
            field_definition=field_definition,
            model_name=model_type.__name__,
            default_factory=default_factory,
            dto_field=DTOField(mark=Mark.READ_ONLY) if read_only else DTOField(),
        )

    @classmethod
    def detect_nested_field(cls, field_definition: FieldDefinition) -> bool:
        return field_definition.is_subclass_of(Model)
