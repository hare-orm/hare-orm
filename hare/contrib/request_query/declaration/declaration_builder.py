from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.contrib.request_query.bounds.lookup_pair_bounds import LookupPairBounds
from hare.contrib.request_query.bounds.parameter_bounds import ParameterBounds
from hare.contrib.request_query.bounds.range_bounds import RangeBounds
from hare.contrib.request_query.constants import (
    DESCENDING_PREFIX,
    FILTER_METHOD_PREFIX,
    LOOKUP_SEPARATOR,
    LOWER_BOUND_LOOKUPS,
    REQUEST_ARGUMENT_NAME,
    STRICT_BOUND_LOOKUPS,
    UPPER_BOUND_LOOKUPS,
)
from hare.contrib.request_query.options.deleted_config import DeletedConfig
from hare.contrib.request_query.options.fields_config import FieldsConfig
from hare.contrib.request_query.options.filter import Filter
from hare.contrib.request_query.options.include_config import IncludeConfig
from hare.contrib.request_query.options.latest_versions import LatestVersions
from hare.contrib.request_query.options.no_filter import NoFilter
from hare.contrib.request_query.options.ordering_config import OrderingConfig
from hare.contrib.request_query.options.search_config import SearchConfig
from hare.contrib.request_query.pagination import Pagination
from hare.contrib.request_query.value_annotations import ValueAnnotation
from hare.core.context import HareContext
from hare.dialects.registry import DialectRegistry
from hare.exceptions import ConfigurationError, FieldError, QueryError
from hare.query.enums import Connector, Lookup, LookupValueShape
from hare.query.queryset import QuerySet

if TYPE_CHECKING:  # pragma: nocoverage
    from pydantic.fields import FieldInfo

    from hare.contrib.request_query.base import RequestQuery
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.lookup_info.lookup_info import LookupInfo
    from hare.query.lookup_info.ordering_info import OrderingInfo
from hare.contrib.request_query.declaration.filter_declaration import FilterDeclaration
from hare.contrib.request_query.declaration.parameter_filter import ParameterFilter
from hare.contrib.request_query.declaration.request_query_declaration import RequestQueryDeclaration


class DeclarationBuilder:
    """Builds and checks the declaration of one request query class.

    Attributes:
        described_models: Every model the descriptions it read so far read - the class's
            declaration or parameters are stale once one of them changes.
    """

    def __init__(self, request_query_class: type[RequestQuery[Any]]) -> None:
        self.request_query_class = request_query_class
        self.class_name = request_query_class.__qualname__
        self.described_models: set[type[Model]] = set()

    def build(self) -> RequestQueryDeclaration:
        """Reads the class's ``Meta`` and parameters through the ORM and checks them.

        Returns:
            The declaration.

        Raises:
            ConfigurationError: The class has no queryset, a parameter names no filter the model
                has, a filter doesn't run on a dialect the class serves, a parameter's annotation
                doesn't take its filter's value, or an option names a field the model lacks.
        """
        queryset = self.get_queryset()
        dialects = self.get_dialects(queryset)
        search = self.get_option("search", SearchConfig)
        ordering = self.get_option("ordering", OrderingConfig)
        pagination = self.get_option("pagination", Pagination)
        fields = self.get_option("fields", FieldsConfig)
        include = self.get_option("include", IncludeConfig)
        deleted = self.get_option("deleted", DeletedConfig)
        versions = self.get_option("versions", LatestVersions)
        for model_option in (deleted, versions, pagination):
            if model_option is not None:
                model_option.check_model(self.class_name, queryset.model)
        join_type = self.request_query_class.get_meta_option("join_type")
        if join_type not in {Connector.AND, Connector.OR}:
            raise ConfigurationError(
                f"{self.class_name}.Meta.join_type must be Connector.AND or Connector.OR, got {join_type!r}"
            )
        option_parameters = self.get_option_parameters()
        if fields is not None:
            self.check_fields(queryset, fields)
        include_to_many = self.build_include(queryset, include) if include is not None else {}
        filters = tuple(
            declaration
            for parameter, field_info in self.request_query_class.model_fields.items()
            if parameter not in option_parameters
            and (declaration := self.build_filter(queryset, dialects, parameter, field_info)) is not None
        )
        lookup_infos = {
            declaration.filter_key: declaration.lookup_info
            for declaration in filters
            if declaration.filter_key is not None and declaration.lookup_info is not None
        }
        if search is not None:
            lookup_infos.update(self.build_search(queryset, dialects, search))
        ordering_infos = self.build_ordering(queryset, ordering, pagination)
        self.described_models.add(queryset.model)
        key_ordering_paths = (
            self.describe_ordering(queryset, "pk").paths if queryset.model._meta.has_primary_key else ()
        )
        return RequestQueryDeclaration(
            queryset=queryset,
            filters=filters,
            bounds=self.build_bounds(filters),
            search=search,
            lookup_infos=lookup_infos,
            ordering=ordering,
            ordering_infos=ordering_infos,
            key_ordering_paths=key_ordering_paths,
            fields=fields,
            include=include,
            include_to_many=include_to_many,
            pagination=pagination,
            join_type=join_type,
            versions=versions,
            deleted=deleted,
            dialects=dialects,
        )

    def get_queryset(self) -> QuerySet[Any]:
        """The class's ``Meta.queryset``.

        Returns:
            The queryset.

        Raises:
            ConfigurationError: The class declares no queryset, or not a ``QuerySet``.
        """
        queryset = self.request_query_class.get_meta_option("queryset")
        if queryset is None:
            raise ConfigurationError(
                f"{self.class_name} declares no Meta.queryset - a class without one only serves as a base"
            )
        if not isinstance(queryset, QuerySet):
            raise ConfigurationError(f"{self.class_name}.Meta.queryset must be a QuerySet, got {queryset!r}")
        return queryset

    def get_dialects(self, queryset: QuerySet[Any]) -> tuple[Dialect, ...]:
        """The dialects the class's filters must run on: its own dialect, else every dialect a
        driver connects to. A class of one dialect also checks that its model's connection is of it.

        Args:
            queryset: The class's queryset.

        Returns:
            The dialects.

        Raises:
            ConfigurationError: The model's connection is of another dialect than the class - checked
                where a Hare context has the connections; with the models bound alone
                (``Hare.bind_models()``) the check waits for ``check_declarations()``.
        """
        dialect_name = self.request_query_class.dialect_name
        if dialect_name is None:
            dialects: dict[str, Dialect] = {}
            for driver in DialectRegistry.get_drivers():
                dialects.setdefault(driver.dialect.name, driver.dialect)
            return tuple(dialects.values())
        if HareContext.get_current() is None:
            return (DialectRegistry.get_dialect(dialect_name),)
        connection_dialect = queryset.model.get_connection().dialect
        if connection_dialect.name != dialect_name:
            raise ConfigurationError(
                f"{self.class_name} is a {dialect_name} request query, but {queryset.model.__name__} "
                f"is on a {connection_dialect.name} connection"
            )
        return (DialectRegistry.get_dialect(dialect_name),)

    def get_option(self, name: str, option_type: Any) -> Any:
        """One ``Meta`` option, checked for its type.

        Args:
            name: The option's name.
            option_type: The type the option must have when set.

        Returns:
            The option, None when unset.

        Raises:
            ConfigurationError: The option has another type.
        """
        option = self.request_query_class.get_meta_option(name)
        if option is not None and not isinstance(option, option_type):
            raise ConfigurationError(f"{self.class_name}.Meta.{name} must be {option_type}, got {option!r}")
        return option

    def get_option_parameters(self) -> set[str]:
        """The parameters the class's options add - they aren't filters.

        Returns:
            The parameter names.
        """
        return {
            parameter
            for option in self.request_query_class.get_request_options()
            for parameter in option.get_parameters()
        }

    def build_filter(
        self, queryset: QuerySet[Any], dialects: tuple[Dialect, ...], parameter: str, field_info: FieldInfo
    ) -> FilterDeclaration | None:
        """Reads one parameter: a ``NoFilter`` one, a filter method's, or a filter the ORM
        describes.

        Args:
            queryset: The class's queryset.
            dialects: The dialects the filter must run on.
            parameter: The parameter's name.
            field_info: The parameter's pydantic field.

        Returns:
            The filter, None for a ``NoFilter`` parameter.

        Raises:
            ConfigurationError: See ``build()``.
        """
        parameter_filter = self.get_parameter_filter(parameter, field_info)
        if parameter_filter is None:
            return None
        filter_key = parameter_filter.filter_key
        if filter_key is None:
            return FilterDeclaration(
                parameter=parameter, filter_key=None, lookup_info=None, method_name=parameter_filter.method_name
            )
        lookup_info = self.get_lookup_info(queryset, dialects, f"{self.class_name}.{parameter}", filter_key)
        annotation = field_info.annotation
        if not ValueAnnotation.accepts_shape(annotation, lookup_info.value_shape, lookup_info.value_type):
            expected = ValueAnnotation.describe(lookup_info.value_shape, lookup_info.value_type)
            raise ConfigurationError(
                f"{self.class_name}.{parameter}: the filter {filter_key!r} takes {expected}, but the parameter "
                f"is annotated {annotation!r}"
            )
        return FilterDeclaration(parameter=parameter, filter_key=filter_key, lookup_info=lookup_info, method_name=None)

    def get_parameter_filter(self, parameter: str, field_info: FieldInfo) -> ParameterFilter | None:
        """How a parameter the class declares filters: by its own name, by the key of its
        ``Filter`` marker, or by its ``filter_<parameter>`` method.

        Args:
            parameter: The parameter's name.
            field_info: The parameter's pydantic field.

        Returns:
            The parameter's filter, None for a ``NoFilter`` parameter.

        Raises:
            ConfigurationError: The parameter is named as the request argument, has more than one
                marker, or has a marker and a filter method.
        """
        if parameter == REQUEST_ARGUMENT_NAME:
            raise ConfigurationError(
                f"{self.class_name} can't declare a parameter named {REQUEST_ARGUMENT_NAME!r} - the request "
                "query receives the request under that name"
            )
        markers = [item for item in field_info.metadata if isinstance(item, (Filter, NoFilter))]
        if len(markers) > 1:
            raise ConfigurationError(f"{self.class_name}.{parameter} has more than one Filter/NoFilter marker")
        marker = markers[0] if markers else None
        method_name = f"{FILTER_METHOD_PREFIX}{parameter}"
        has_method = callable(getattr(self.request_query_class, method_name, None))
        if isinstance(marker, NoFilter):
            if has_method:
                raise ConfigurationError(f"{self.class_name}.{parameter} is marked NoFilter but has {method_name}()")
            return None
        if has_method:
            if marker is not None:
                raise ConfigurationError(
                    f"{self.class_name}.{parameter} has both a Filter marker and {method_name}() - keep one"
                )
            return ParameterFilter(filter_key=None, method_name=method_name)
        return ParameterFilter(filter_key=marker.filter_key if marker is not None else parameter, method_name=None)

    @staticmethod
    def build_bounds(filters: tuple[FilterDeclaration, ...]) -> tuple[ParameterBounds, ...]:
        """Reads the bounds the filters give fields: each lower bound (``gt``/``gte``) with each
        upper bound (``lt``/``lte``) of the same field path, and each ``range``.

        Args:
            filters: The class's filters.

        Returns:
            The checks of the bounds.
        """
        bounds: list[ParameterBounds] = []
        lower_bounds: dict[str, list[FilterDeclaration]] = {}
        upper_bounds: dict[str, list[FilterDeclaration]] = {}
        for filter_declaration in filters:
            lookup_info = filter_declaration.lookup_info
            if lookup_info is None or filter_declaration.filter_key is None:
                continue
            lookup = lookup_info.lookup
            if lookup == Lookup.RANGE:
                bounds.append(RangeBounds(filter_declaration.parameter))
                continue
            path = filter_declaration.filter_key.removesuffix(f"{LOOKUP_SEPARATOR}{lookup}")
            if lookup in LOWER_BOUND_LOOKUPS:
                lower_bounds.setdefault(path, []).append(filter_declaration)
            elif lookup in UPPER_BOUND_LOOKUPS:
                upper_bounds.setdefault(path, []).append(filter_declaration)
        for path, lowers in lower_bounds.items():
            for lower in lowers:
                for upper in upper_bounds.get(path, []):
                    bounds.append(
                        LookupPairBounds(
                            lower_parameter=lower.parameter,
                            upper_parameter=upper.parameter,
                            strict=any(
                                bound.lookup_info is not None and bound.lookup_info.lookup in STRICT_BOUND_LOOKUPS
                                for bound in (lower, upper)
                            ),
                        )
                    )
        return tuple(bounds)

    def check_fields(self, queryset: QuerySet[Any], fields: FieldsConfig) -> None:
        """Checks that the fields a request may ask to load are the model's own fields.

        Args:
            queryset: The class's queryset.
            fields: The fields option.

        Raises:
            ConfigurationError: A name isn't a field of the model, or is a relation or a composite key.
        """
        for name in fields.fields:
            lookup_info = self.describe_filter(queryset, f"{self.class_name}.Meta.fields", name)
            if lookup_info.relations or isinstance(lookup_info.field, tuple) or lookup_info.transforms:
                raise ConfigurationError(
                    f"{self.class_name}.Meta.fields: {name!r} isn't a field of {queryset.model.__name__} itself - "
                    "relations are loaded with Meta.include"
                )

    def build_include(self, queryset: QuerySet[Any], include: IncludeConfig) -> dict[str, bool]:
        """Reads the relations a request may ask to load.

        Args:
            queryset: The class's queryset.
            include: The include option.

        Returns:
            Each relation path, with whether it holds many rows.

        Raises:
            ConfigurationError: A path isn't a chain of relations of the model.
        """
        include_to_many: dict[str, bool] = {}
        for path in include.relations:
            lookup_info = self.describe_filter(queryset, f"{self.class_name}.Meta.include", path)
            if len(lookup_info.relations) != len(path.split("__")) or lookup_info.transforms:
                raise ConfigurationError(
                    f"{self.class_name}.Meta.include: {path!r} isn't a relation of {queryset.model.__name__}"
                )
            include_to_many[path] = lookup_info.crosses_to_many
        return include_to_many

    def get_lookup_info(
        self, queryset: QuerySet[Any], dialects: tuple[Dialect, ...], owner: str, filter_key: str
    ) -> LookupInfo:
        """The ORM's description of a filter key, checked against the dialects.

        Args:
            queryset: The class's queryset.
            dialects: The dialects the filter must run on.
            owner: What declares the key, for the error.
            filter_key: The ``.filter()`` key.

        Returns:
            The description.

        Raises:
            ConfigurationError: The model has no such filter, or a dialect doesn't run it.
        """
        lookup_info = self.describe_filter(queryset, owner, filter_key)
        for dialect in dialects:
            if not lookup_info.is_supported(dialect):
                raise ConfigurationError(
                    f"{owner}: the filter {filter_key!r} doesn't run on {dialect.name} - a request query using it "
                    f"must be of the dialect it runs on"
                )
        return lookup_info

    def describe_filter(self, queryset: QuerySet[Any], owner: str, filter_key: str) -> LookupInfo:
        """The ORM's description of a filter key - its models join ``described_models``.

        Args:
            queryset: The class's queryset.
            owner: What declares the key, for the error.
            filter_key: The ``.filter()`` key.

        Returns:
            The description.

        Raises:
            ConfigurationError: The model has no such filter.
        """
        try:
            lookup_info = queryset.get_lookup_info(filter_key)
        except (FieldError, QueryError) as error:
            raise ConfigurationError(f"{owner}: {error}") from error
        self.described_models.update(RequestQueryDeclaration.get_models(lookup_info))
        return lookup_info

    def describe_ordering(self, queryset: QuerySet[Any], name: str) -> OrderingInfo:
        """The ORM's description of an ordering name - its models join ``described_models``.

        Args:
            queryset: The class's queryset.
            name: The ordering name, without its direction.

        Returns:
            The description.

        Raises:
            FieldError: The name names no field or relation.
        """
        ordering_info = queryset.get_ordering_info(name)
        self.described_models.update(RequestQueryDeclaration.get_models(ordering_info))
        return ordering_info

    def build_search(
        self, queryset: QuerySet[Any], dialects: tuple[Dialect, ...], search: SearchConfig
    ) -> dict[str, LookupInfo]:
        """Reads the searched fields: each takes one text value with the search's lookup.

        Args:
            queryset: The class's queryset.
            dialects: The dialects the search must run on.
            search: The search option.

        Returns:
            The ORM's description of each field's filter key, by key.

        Raises:
            ConfigurationError: A field doesn't exist, doesn't run the lookup on a dialect, or its
                lookup doesn't take text.
        """
        lookup_infos = {}
        for field_name in search.fields:
            filter_key = search.get_filter_key(field_name)
            lookup_info = self.get_lookup_info(queryset, dialects, f"{self.class_name}.Meta.search", filter_key)
            if lookup_info.value_shape is not LookupValueShape.VALUE or not ValueAnnotation.accepts_value(
                str, lookup_info.value_type
            ):
                raise ConfigurationError(
                    f"{self.class_name}.Meta.search: {filter_key!r} takes "
                    f"{ValueAnnotation.describe(lookup_info.value_shape, lookup_info.value_type)}, not text"
                )
            lookup_infos[filter_key] = lookup_info
        return lookup_infos

    def build_ordering(
        self,
        queryset: QuerySet[Any],
        ordering: OrderingConfig | None,
        pagination: Pagination | None,
    ) -> dict[str, OrderingInfo]:
        """Reads the names a request may order by and the default ordering.

        Args:
            queryset: The class's queryset.
            ordering: The ordering option.
            pagination: The pagination option - it checks each name it pages by.

        Returns:
            The ORM's description of each name, by name without its direction.

        Raises:
            ConfigurationError: A name doesn't exist, crosses a relation to many rows, or the
                pagination can't page by it.
        """
        names = list(ordering.fields) if ordering else []
        if ordering:
            names.extend(name.removeprefix(DESCENDING_PREFIX) for name in ordering.default)
        ordering_infos: dict[str, OrderingInfo] = {}
        for name in names:
            if name in ordering_infos:
                continue
            ordering_infos[name] = self.get_ordering_info(queryset, name)
            if pagination is not None:
                pagination.check_ordering(self.class_name, name, queryset)
        return ordering_infos

    def get_ordering_info(self, queryset: QuerySet[Any], name: str) -> OrderingInfo:
        """The ORM's description of one ordering name, checked.

        Args:
            queryset: The class's queryset.
            name: The ordering name, without its direction.

        Returns:
            The description.

        Raises:
            ConfigurationError: See ``build_ordering()``.
        """
        try:
            ordering_info = self.describe_ordering(queryset, name)
        except (FieldError, QueryError) as error:
            raise ConfigurationError(f"{self.class_name}.Meta.ordering: {error}") from error
        if ordering_info.crosses_to_many:
            raise ConfigurationError(
                f"{self.class_name}.Meta.ordering: {name!r} crosses a relation to many rows - each row would repeat "
                "once per related row"
            )
        return ordering_info
