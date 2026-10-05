from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.caching.cache import Cache
from hare.exceptions import FieldError, QueryError
from hare.fields.data.json.json_field import JSONField
from hare.fields.encrypted.encrypted_json_field import EncryptedJSONField
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.enums import Lookup, LookupTarget, LookupValueShape
from hare.query.expressions.constants import TO_MANY_RELATION_SHORTCUT_LOOKUPS
from hare.query.filters.constants import (
    BOOLEAN_VALUE_LOOKUPS,
    DATE_TRANSFORM_VALUE_TYPES,
    JSON_PATH_LIST_LOOKUPS,
    KEY_LIST_LOOKUPS,
    LIST_VALUE_LOOKUPS,
    TEXT_VALUE_LOOKUPS,
)
from hare.query.filters.lookups.field_lookup import FieldLookup
from hare.query.filters.lookups.field_lookups import FieldLookups
from hare.query.filters.lookups.field_transforms import FieldTransforms
from hare.query.filters.lookups.json.json_path_lookups import JsonPathLookups
from hare.query.generic_foreign_keys.generic_foreign_key_paths import GenericForeignKeyPaths
from hare.query.lookup_info.declarations import LookupKeyPosition
from hare.query.lookup_info.lookup_info import LookupInfo
from hare.query.lookup_info.ordering_info import OrderingInfo
from hare.query.lookup_info.value_paths import ValuePaths

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.fields.registrations.registered_lookup import RegisteredLookup
    from hare.models import Model


class LookupInfoBuilder:
    """Reads filter keys and ordering names through a model's fields and relations. The descriptions
    are cached per model and, for a key starting with an annotation, per queryset; a change to any
    model's fields, lookups or relations drops them all.
    """

    #: A model's descriptions, in a bucket the model keeps: filter key -> ``get_lookup_info()``,
    #: ordering name -> ``get_ordering_info()``, (path, dialect name) -> ``get_lookups()``. A
    #: description follows relations into other models, so a change to any model drops them all.
    lookup_infos: ClassVar[Cache[Any]] = Cache(
        holds_sql=False, keyed_by_model=False, depends_on_other_models=True, model_attribute="lookup_infos"
    )
    ordering_infos: ClassVar[Cache[Any]] = Cache(
        holds_sql=False, keyed_by_model=False, depends_on_other_models=True, model_attribute="ordering_infos"
    )
    lookups_by_path: ClassVar[Cache[Any]] = Cache(
        holds_sql=False, keyed_by_model=False, depends_on_other_models=True, model_attribute="lookups_by_path"
    )

    #: The descriptions of keys starting with an annotation, in a bucket a queryset shares with
    #: its clones: (description type, key, dialect name) -> the description.
    annotation_descriptions: ClassVar[Cache[Any]] = Cache(
        holds_sql=False, keyed_by_model=False, depends_on_other_models=True
    )

    @classmethod
    def forget_descriptions(cls) -> None:
        """Drops every cached filter and ordering description, the ones a queryset keeps of its
        annotations too - a model's fields, lookups or relations changed."""
        cls.lookup_infos.clear()
        cls.ordering_infos.clear()
        cls.lookups_by_path.clear()
        cls.annotation_descriptions.clear()

    @classmethod
    def get_lookup_info(
        cls,
        model: type[Model],
        key: str,
        *,
        annotations: Mapping[str, Any] | None = None,
        annotation_fields: Mapping[str, Field[Any] | None] | None = None,
        label: str | None = None,
    ) -> LookupInfo:
        """Describes a ``.filter()`` key.

        Args:
            model: The model the key starts at.
            key: The filter key.
            annotations: The query's annotations - a key may start with one.
            annotation_fields: The output field of each annotation, where known.
            label: What the key is named as in an error, ``Unknown filter param '<key>'`` by
                default.

        Returns:
            The description.

        Raises:
            FieldError: The key names no field, relation or annotation, a lookup the field
                doesn't have, or one outside the field's ``supported_lookups``.
            QueryError: The key is a lookup other than equality, membership or ``isnull`` on a
                relation to a composite key.
        """
        label = label or f"Unknown filter param '{key}'"
        segments = key.split("__")
        generic_lookup_info = GenericForeignKeyPaths.get_lookup_info(model, key)
        if generic_lookup_info is not None:
            return generic_lookup_info
        if annotations is not None and segments[0] in annotations:
            return cls._get_annotation_info(
                model,
                key,
                segments,
                (annotation_fields or {}).get(segments[0]),
                label,
            )
        relations: list[Field[Any]] = []
        current_model = model
        position = 0
        while True:
            segment = segments[position]
            rest = segments[position + 1 :]
            meta = current_model._meta
            if segment == "pk" and not meta.has_primary_key:
                raise FieldError(f"{label}: {current_model.__name__} has no primary key (Meta.primary_key = None)")
            if segment == "pk" and meta.has_composite_primary_key:
                return cls._get_composite_pk_info(
                    LookupKeyPosition(model, key, label, current_model, tuple(relations)), rest
                )
            field_object = meta.pk if segment == "pk" else meta.fields_map.get(segment)
            if field_object is None:
                raise FieldError(f"{label}: {current_model.__name__} has no field '{segment}'")
            if segment not in meta.fetch_fields:
                return cls._get_field_info(
                    LookupKeyPosition(model, key, label, current_model, tuple(relations)), segment, field_object, rest
                )
            relation = cast("RelationalField[Model]", field_object)
            relation_lookup = cls._get_relation_lookup(current_model, relation, rest, key)
            if relation_lookup is not None:
                return cls._get_relation_info(
                    LookupKeyPosition(model, key, label, current_model, (*relations, relation)),
                    relation,
                    relation_lookup,
                )
            relations.append(relation)
            current_model = relation.related_model
            position += 1

    @classmethod
    def get_lookups(
        cls,
        model: type[Model],
        path: str,
        dialect: Dialect,
        *,
        annotations: Mapping[str, Any] | None = None,
        annotation_fields: Mapping[str, Field[Any] | None] | None = None,
    ) -> dict[str, LookupInfo]:
        """Describes every lookup of a field that a dialect runs.

        Args:
            model: The model the path starts at.
            path: A field, relation or annotation, after any relations (``author__name``,
                ``tags``, ``pk``).
            dialect: The dialect.
            annotations: The query's annotations.
            annotation_fields: The output field of each annotation, where known.

        Returns:
            Each lookup's suffix after ``path`` (``""`` for plain equality, ``"icontains"``,
            ``"year__gte"``) to its description.

        Raises:
            FieldError: The path names no field, relation or annotation.
        """
        suffixes = cls._get_lookup_suffixes(model, path, annotations)
        lookups: dict[str, LookupInfo] = {}
        for suffix in suffixes:
            lookup_info = cls.get_lookup_info(
                model,
                f"{path}__{suffix}" if suffix else path,
                annotations=annotations,
                annotation_fields=annotation_fields,
            )
            if dialect.filter_operators.supports_lookup(lookup_info):
                lookups[suffix] = lookup_info
        return lookups

    @classmethod
    def get_ordering_info(
        cls,
        model: type[Model],
        name: str,
        *,
        annotations: Mapping[str, Any] | None = None,
        annotation_fields: Mapping[str, Field[Any] | None] | None = None,
        label: str | None = None,
    ) -> OrderingInfo:
        """Describes an ``.order_by()`` name.

        Args:
            model: The model the name starts at.
            name: The ordering name, optionally with a leading ``-``.
            annotations: The query's annotations - a name may start with one.
            annotation_fields: The output field of each annotation, where known.
            label: What the name is named as in an error, ``Unknown field <name> for ordering``
                by default.

        Returns:
            The description.

        Raises:
            FieldError: The name names no field, relation or annotation, or reads a path inside
                a field that has none.
        """
        descending = name.startswith("-")
        path = name.removeprefix("-")
        label = label or f"Unknown field {path} for ordering"
        segments = path.split("__")
        if annotations is not None and (path in annotations or segments[0] in annotations):
            # An annotation named with "__" (a generic foreign key's ``target__type``) is read whole.
            annotation_name = path if path in annotations else segments[0]
            return OrderingInfo(
                name=name,
                model=model,
                relations=(),
                fields=((annotation_fields or {}).get(annotation_name),),
                paths=(path,),
                transforms=tuple(path.removeprefix(annotation_name).removeprefix("__").split("__"))
                if path != annotation_name
                else (),
                descending=descending,
                crosses_to_many=False,
            )
        relations: list[Field[Any]] = []
        current_model = model
        for position, segment in enumerate(segments):
            meta = current_model._meta
            prefix = "".join(f"{relation_segment}__" for relation_segment in segments[:position])
            is_last = position == len(segments) - 1
            if segment == "pk":
                if not meta.has_primary_key:
                    raise FieldError(f"{label}: {current_model.__name__} has no primary key (Meta.primary_key = None)")
                if not is_last:
                    raise FieldError(
                        f"{label}: {current_model.__name__}.pk has no path '{'__'.join(segments[position + 1 :])}'"
                    )
                key_fields = meta.pk_fields or (meta.pk,)
                # The model's own pk orders by its key field(s); a related one (``author__pk``) is
                # read through the relation as named.
                key_paths = tuple(key_field.model_field_name for key_field in key_fields) if not prefix else (path,)
                return cls._get_ordering(name, model, relations, key_fields, key_paths, (), descending)
            field_object = meta.fields_map.get(segment)
            if field_object is None:
                raise FieldError(f"{label}: {current_model.__name__} has no field '{segment}'")
            if segment in meta.fetch_fields:
                relation = cast("RelationalField[Model]", field_object)
                if not is_last:
                    relations.append(relation)
                    current_model = relation.related_model
                    continue
                if segment in meta.foreign_key_fields or segment in meta.one_to_one_fields:
                    # A forward relation orders by its own key column(s), with no join.
                    source_fields = tuple(meta.fields_map[source_field] for source_field in relation.source_fields)
                    return cls._get_ordering(
                        name,
                        model,
                        relations,
                        source_fields,
                        tuple(f"{prefix}{source}" for source in relation.source_fields),
                        (),
                        descending,
                    )
                # A reverse or many-to-many relation orders by the related primary key.
                related_meta = relation.related_model._meta
                key_fields = related_meta.pk_fields or (related_meta.pk,)
                return cls._get_ordering(name, model, [*relations, relation], key_fields, (path,), (), descending)
            transforms = tuple(segments[position + 1 :])
            if transforms:
                cls._raise_if_not_value_path(current_model, segment, field_object, list(transforms), label)
            return cls._get_ordering(name, model, relations, (field_object,), (path,), transforms, descending)
        raise FieldError(f"{label}: an ordering name can't be empty")  # pragma: nocoverage

    @staticmethod
    def _get_ordering(
        name: str,
        model: type[Model],
        relations: list[Field[Any]],
        fields: tuple[Field[Any], ...],
        paths: tuple[str, ...],
        transforms: tuple[str, ...],
        descending: bool,
    ) -> OrderingInfo:
        return OrderingInfo(
            name=name,
            model=model,
            relations=tuple(relations),
            fields=fields,
            paths=paths,
            transforms=transforms,
            descending=descending,
            crosses_to_many=any(getattr(relation, "is_multi_valued", False) for relation in relations),
        )

    @staticmethod
    def _raise_if_not_value_path(
        model: type[Model], name: str, field_object: Field[Any], path: list[str], label: str
    ) -> None:
        """Rejects a path inside a field whose value has none - only a JSON value has keys, and an
        array, range, text or hstore value its transforms.

        Raises:
            FieldError: The field's value has no such path.
        """
        effective_field = GeneratedField.get_effective_field(field_object)
        if isinstance(effective_field, JSONField):
            return
        if len(path) == 1 and path[0] in ValuePaths.get_date_part_segments(field_object):
            return
        __, __, rest = FieldTransforms.get_path(field_object, path)
        if rest:
            raise FieldError(f"{label}: {model.__name__}.{name} has no path '{'__'.join(path)}'")

    @staticmethod
    def _get_relation_lookup(
        model: type[Model], relation: RelationalField[Model], rest: list[str], key: str
    ) -> str | None:
        """The lookup a key applies to a relation itself (``author=``, ``author__in=``,
        ``tags__isnull=``), None when the key goes on through the relation (``author__name``).

        Raises:
            QueryError: The key is a lookup other than equality, membership or ``isnull`` on a
                forward relation to a composite key.
        """
        if not rest:
            return Lookup.EXACT
        if len(rest) > 1:
            return None
        lookup_name = rest[0]
        related_meta = relation.related_model._meta
        if lookup_name == "pk" or lookup_name in related_meta.fields_map:
            return None
        meta = model._meta
        if relation.model_field_name in meta.foreign_key_fields or relation.model_field_name in meta.one_to_one_fields:
            source_fields = [meta.fields_map[source_field] for source_field in relation.source_fields]
            if len(source_fields) == 1:
                return lookup_name if lookup_name in FieldLookups.get(source_fields[0]) else None
            if lookup_name in TO_MANY_RELATION_SHORTCUT_LOOKUPS:
                return lookup_name
            if all(lookup_name in FieldLookups.get(source_field) for source_field in source_fields):
                source_filter_keys = [f"{source_field}__{lookup_name}" for source_field in relation.source_fields]
                raise QueryError(
                    f"'{key}' - {model.__name__}.{relation.model_field_name} has a composite key, so a lookup on "
                    f"the relation itself is ambiguous; filter each key column instead "
                    f"({', '.join(source_filter_keys)})."
                )
            return None
        return lookup_name if lookup_name in TO_MANY_RELATION_SHORTCUT_LOOKUPS else None

    @classmethod
    def _get_relation_info(
        cls,
        position: LookupKeyPosition,
        relation: RelationalField[Model],
        lookup_name: str,
    ) -> LookupInfo:
        """Describes a lookup on a relation itself - it compares the related model's key: the
        relation's target field(s) for a forward relation, the related primary key otherwise."""
        owner_meta = position.owner_model._meta
        related_meta = relation.related_model._meta
        is_forward = (
            relation.model_field_name in owner_meta.foreign_key_fields
            or relation.model_field_name in owner_meta.one_to_one_fields
        )
        if not related_meta.has_primary_key and lookup_name not in BOOLEAN_VALUE_LOOKUPS:
            raise FieldError(
                f"{position.label}: {relation.related_model.__name__} has no primary key (Meta.primary_key = None) - "
                f"its rows can't be compared as a whole; test {relation.model_field_name}__isnull or filter "
                "through its fields"
            )
        if not related_meta.has_primary_key:
            # IS [NOT] NULL is tested on the related row's key column to this one.
            backward_relation = cast("BackwardForeignKeyRelation[Model]", relation)
            key_field = related_meta.fields_map[
                related_meta.fields_db_projection_reverse[backward_relation.relation_source_fields[0]]
            ]
            return LookupInfo(
                key=position.key,
                model=position.model,
                relations=position.relations,
                field=key_field,
                transforms=(),
                lookup=cls._get_lookup(lookup_name),
                value_shape=LookupValueShape.VALUE,
                value_type=bool,
                crosses_to_many=cls._crosses_to_many(position.relations),
                requires_extension=None,
                dialects=None,
                target=LookupTarget.RELATION,
                field_lookup=FieldLookups.get(relation).get(lookup_name),
            )
        key_fields: tuple[Field[Any], ...] = (
            tuple(relation.to_field_instances) if is_forward else (related_meta.pk_fields or (related_meta.pk,))
        )
        field_lookup: FieldLookup | None
        value_field: Field[Any] | None = None
        if is_forward and len(relation.source_fields) == 1:
            value_field = owner_meta.fields_map[relation.source_fields[0]]
            cls._raise_if_rejected(value_field, lookup_name, position.label)
            field_lookup = FieldLookups.get(value_field)[lookup_name]
        else:
            field_lookup = None if is_forward else FieldLookups.get(relation).get(lookup_name)
        lookup = cls._get_lookup(lookup_name)
        if lookup in BOOLEAN_VALUE_LOOKUPS:
            value_type: Any = bool
        elif len(key_fields) > 1:
            value_type = tuple(key_field.field_type for key_field in key_fields)
        else:
            value_type = key_fields[0].field_type
        return LookupInfo(
            key=position.key,
            model=position.model,
            relations=position.relations,
            field=key_fields[0] if len(key_fields) == 1 else key_fields,
            transforms=(),
            lookup=lookup,
            value_shape=cls._get_plain_value_shape(lookup),
            value_type=value_type,
            crosses_to_many=cls._crosses_to_many(position.relations),
            requires_extension=None,
            dialects=cls._get_dialects(*key_fields),
            target=LookupTarget.RELATION,
            field_lookup=field_lookup,
            value_field=value_field,
        )

    @classmethod
    def _get_composite_pk_info(
        cls,
        position: LookupKeyPosition,
        rest: list[str],
    ) -> LookupInfo:
        """Describes ``pk``/``pk__in``/``pk__not``/``pk__not_in`` of a composite primary key - a tuple
        of the key's values, or a list of them."""
        meta = position.owner_model._meta
        if rest not in ([], [Lookup.IN], [Lookup.NOT], [Lookup.NOT_IN]):
            raise FieldError(
                f"{position.label}: {position.owner_model.__name__}.pk is a composite primary key - it takes "
                f"pk=, pk__in=, pk__not= and pk__not_in=, not '{'__'.join(rest)}'"
            )
        lookup = Lookup(rest[0]) if rest else Lookup.EXACT
        return LookupInfo(
            key=position.key,
            model=position.model,
            relations=position.relations,
            field=meta.pk_fields,
            transforms=(),
            lookup=lookup,
            value_shape=LookupValueShape.LIST if lookup in {Lookup.IN, Lookup.NOT_IN} else LookupValueShape.VALUE,
            value_type=tuple(pk_field.field_type for pk_field in meta.pk_fields),
            crosses_to_many=cls._crosses_to_many(position.relations),
            requires_extension=None,
            dialects=cls._get_dialects(*meta.pk_fields),
            target=LookupTarget.PRIMARY_KEY,
        )

    @classmethod
    def _get_field_info(
        cls,
        position: LookupKeyPosition,
        name: str,
        field_object: Field[Any],
        rest: list[str],
    ) -> LookupInfo:
        """Describes a lookup on a model field, through any path inside its value."""
        effective_field = GeneratedField.get_effective_field(field_object)
        if isinstance(effective_field, JSONField):
            return cls._get_json_field_info(position, name, field_object, rest)
        transforms: tuple[str, ...] = ()
        term_transforms: list[Any] = []
        date_value_type: Any = None
        suffix = "__".join(rest)
        output_field = field_object
        if suffix in FieldLookups.get(field_object):
            value_field = effective_field
            lookup_field = field_object
            if rest and rest[0] in DATE_TRANSFORM_VALUE_TYPES:
                transforms = (rest[0],)
                lookup_name = rest[1] if len(rest) == 2 else ""
                date_value_type = DATE_TRANSFORM_VALUE_TYPES[rest[0]]
            else:
                lookup_name = suffix
        else:
            # A path through an array, range, text or hstore value, then the lookup of what it reads.
            term_transforms, output_field, remaining = FieldTransforms.get_path(field_object, rest)
            if not term_transforms or len(remaining) > 1:
                raise FieldError(f"{position.label}: {position.owner_model.__name__}.{name} has no lookup '{suffix}'")
            transforms = tuple(rest[: len(rest) - len(remaining)])
            value_field = GeneratedField.get_effective_field(output_field)
            lookup_name = remaining[0] if remaining else ""
            lookup_field = output_field
            suffix = lookup_name
        field_lookup = FieldLookups.get(lookup_field).get(suffix)
        if field_lookup is None:
            raise FieldError(
                f"{position.label}: {position.owner_model.__name__}.{name} has no lookup '{'__'.join(rest)}'"
            )
        cls._raise_if_rejected(lookup_field, suffix, position.label)
        lookup = cls._get_lookup(lookup_name)
        registered_lookup = (
            None if date_value_type is not None else cls._get_registered_lookup(value_field, lookup_name)
        )
        if registered_lookup is not None:
            value_shape = registered_lookup.value_shape
            value_type = registered_lookup.value_type or value_field.field_type
        elif date_value_type is not None:
            value_shape = cls._get_plain_value_shape(lookup)
            value_type = bool if lookup in BOOLEAN_VALUE_LOOKUPS else date_value_type
        else:
            value_shape, value_type = cls._get_field_value_description(value_field, lookup)
        requires_extension = (
            None if registered_lookup is None else registered_lookup.required_extension
        ) or FieldTransforms.get_path_required_extension(field_object, list(transforms))
        dialects = cls._get_dialects(effective_field, value_field)
        if registered_lookup is not None and registered_lookup.dialects is not None:
            dialects = registered_lookup.dialects if dialects is None else dialects & registered_lookup.dialects
        return LookupInfo(
            key=position.key,
            model=position.model,
            relations=position.relations,
            field=field_object,
            transforms=transforms,
            lookup=lookup,
            value_shape=value_shape,
            value_type=value_type,
            crosses_to_many=cls._crosses_to_many(position.relations),
            requires_extension=requires_extension,
            dialects=dialects,
            target=LookupTarget.FIELD,
            field_lookup=field_lookup,
            value_field=output_field if term_transforms else field_object,
            term_transforms=tuple(term_transforms),
        )

    @classmethod
    def _get_json_field_info(
        cls,
        position: LookupKeyPosition,
        name: str,
        field_object: Field[Any],
        rest: list[str],
    ) -> LookupInfo:
        """Describes a lookup on a JSON field - of the whole value (``data__contains``,
        ``data__has_key``) or of the value at a key path (``data__owner__name__icontains``)."""
        suffix = "__".join(rest)
        path_lookup_names = JsonPathLookups.get_lookup_names()
        whole_value_lookups = FieldLookups.get(field_object)
        target = LookupTarget.FIELD
        if len(rest) <= 1 and suffix in whole_value_lookups:
            cls._raise_if_rejected(field_object, suffix, position.label)
            field_lookup: FieldLookup | None = whole_value_lookups[suffix]
            lookup = cls._get_lookup(suffix)
            registered_lookup = cls._get_registered_lookup(GeneratedField.get_effective_field(field_object), lookup)
            if registered_lookup is not None:
                value_shape = registered_lookup.value_shape
                value_type = registered_lookup.value_type or object
            else:
                value_shape, value_type = cls._get_json_value_description(lookup, whole_value=True)
            transforms: tuple[str, ...] = ()
        elif len(rest) == 1 and rest[0] in path_lookup_names:
            # A lookup name right after the field is that lookup of the whole value, never a key.
            raise FieldError(f"{position.label}: {position.owner_model.__name__}.{name} has no lookup '{rest[0]}'")
        else:
            if isinstance(field_object, EncryptedJSONField):
                raise FieldError(
                    f"{position.label}: {field_object.get_field_label()} is encrypted - a key path would read a "
                    "stored Fernet token, not the key's value. Filter on the whole field instead."
                )
            if len(rest) > 1 and rest[-1] in path_lookup_names:
                transforms, lookup = tuple(rest[:-1]), cls._get_lookup(rest[-1])
            else:
                transforms, lookup = tuple(rest), Lookup.EXACT
            field_lookup = JsonPathLookups.get_lookups()[lookup]
            value_shape, value_type = cls._get_json_value_description(lookup, whole_value=False)
            target = LookupTarget.JSON_PATH
        return LookupInfo(
            key=position.key,
            model=position.model,
            relations=position.relations,
            field=field_object,
            transforms=transforms,
            lookup=lookup,
            value_shape=value_shape,
            value_type=value_type,
            crosses_to_many=cls._crosses_to_many(position.relations),
            requires_extension=None,
            dialects=cls._get_dialects(GeneratedField.get_effective_field(field_object)),
            target=target,
            field_lookup=field_lookup,
            value_field=field_object,
        )

    @classmethod
    def _get_annotation_info(
        cls,
        model: type[Model],
        key: str,
        segments: list[str],
        annotation_field: Field[Any] | None,
        label: str,
    ) -> LookupInfo:
        """Describes a lookup on an annotation - of its value, or of the value at a key path when
        the annotation is a JSON value."""
        name, rest = segments[0], segments[1:]
        suffix = "__".join(rest)
        effective_field = None if annotation_field is None else GeneratedField.get_effective_field(annotation_field)
        transforms: tuple[str, ...] = ()
        value_type: Any = None
        field_blind_lookups = FieldLookups.get(None)
        if not rest or suffix in field_blind_lookups:
            field_lookup: FieldLookup | None = field_blind_lookups.get(suffix)
            if rest and rest[0] in DATE_TRANSFORM_VALUE_TYPES:
                transforms = (rest[0],)
                lookup = cls._get_lookup(rest[1] if len(rest) == 2 else "")
                value_type = bool if lookup in BOOLEAN_VALUE_LOOKUPS else DATE_TRANSFORM_VALUE_TYPES[rest[0]]
                value_shape = cls._get_plain_value_shape(lookup)
            else:
                lookup = cls._get_lookup(suffix)
                if isinstance(effective_field, JSONField):
                    value_shape, value_type = cls._get_json_value_description(lookup, whole_value=True)
                elif effective_field is not None:
                    value_shape, value_type = cls._get_field_value_description(effective_field, lookup)
                else:
                    value_shape = cls._get_plain_value_shape(lookup)
                    value_type = bool if lookup in BOOLEAN_VALUE_LOOKUPS else object
        elif effective_field is None or isinstance(effective_field, JSONField):
            path_lookup_names = JsonPathLookups.get_lookup_names()
            if len(rest) > 1 and rest[-1] in path_lookup_names:
                transforms, lookup = tuple(rest[:-1]), cls._get_lookup(rest[-1])
            else:
                transforms, lookup = tuple(rest), Lookup.EXACT
            field_lookup = JsonPathLookups.get_lookups().get(lookup)
            value_shape, value_type = cls._get_json_value_description(lookup, whole_value=False)
        else:
            raise FieldError(f"{label}: the annotation '{name}' has no lookup '{suffix}'")
        dialects = cls._get_dialects(effective_field)
        # A lookup registered on Field itself is a lookup of an annotation too.
        registered_lookup = None if transforms else Field.registered_lookups.get(str(lookup))
        if registered_lookup is not None:
            value_shape = registered_lookup.value_shape
            value_type = registered_lookup.value_type or value_type or object
            if registered_lookup.dialects is not None:
                dialects = registered_lookup.dialects if dialects is None else dialects & registered_lookup.dialects
        return LookupInfo(
            key=key,
            model=model,
            relations=(),
            field=annotation_field,
            transforms=transforms,
            lookup=lookup,
            value_shape=value_shape,
            value_type=value_type,
            crosses_to_many=False,
            requires_extension=None if registered_lookup is None else registered_lookup.required_extension,
            dialects=dialects,
            target=LookupTarget.ANNOTATION,
            field_lookup=field_lookup,
            value_field=annotation_field,
        )

    @classmethod
    def _get_lookup_suffixes(
        cls,
        model: type[Model],
        path: str,
        annotations: Mapping[str, Any] | None,
    ) -> list[str]:
        """The lookup suffixes a field path takes, before asking any dialect.

        Raises:
            FieldError: The path names no field, relation or annotation.
        """
        segments = path.split("__")
        if annotations is not None and segments[0] in annotations and len(segments) == 1:
            return list(FieldLookups.get(None))
        current_model = model
        for position, segment in enumerate(segments):
            meta = current_model._meta
            is_last = position == len(segments) - 1
            if segment == "pk" and meta.has_composite_primary_key:
                if not is_last:
                    raise FieldError(f"{model.__name__} has no field path '{path}'")
                return ["", Lookup.IN, Lookup.NOT, Lookup.NOT_IN]
            field_object = meta.pk if segment == "pk" else meta.fields_map.get(segment)
            if field_object is None:
                raise FieldError(
                    f"{model.__name__} has no field path '{path}': {current_model.__name__} has no field '{segment}'"
                )
            if segment in meta.fetch_fields:
                relation = cast("RelationalField[Model]", field_object)
                if not is_last:
                    current_model = relation.related_model
                    continue
                if (segment in meta.foreign_key_fields or segment in meta.one_to_one_fields) and len(
                    relation.source_fields
                ) == 1:
                    source_field_name = relation.source_fields[0]
                    return cls._get_field_filter_suffixes(
                        meta.fields_map[source_field_name], exclude=relation.related_model._meta.fields_map
                    )
                return [str(lookup) for lookup in TO_MANY_RELATION_SHORTCUT_LOOKUPS]
            if not is_last:
                raise FieldError(
                    f"{model.__name__} has no field path '{path}': "
                    f"{current_model.__name__}.{segment} is not a relation"
                )
            return cls._get_field_filter_suffixes(field_object)
        return []  # pragma: nocoverage

    @staticmethod
    def _get_field_filter_suffixes(field_object: Field[Any], exclude: Mapping[str, Any] | None = None) -> list[str]:
        """The lookup suffixes of a model field's own lookups - those outside its
        ``supported_lookups`` left out."""
        return [
            suffix
            for suffix in FieldLookups.get(field_object)
            if FieldLookups.is_supported(field_object, suffix) and (exclude is None or suffix not in exclude)
        ]

    @staticmethod
    def _raise_if_rejected(field_object: Field[Any], suffix: str, label: str) -> None:
        """Rejects a lookup outside the field's ``supported_lookups``.

        Raises:
            FieldError: The lookup isn't one of them.
        """
        if not FieldLookups.is_supported(field_object, suffix):
            raise FieldError(f"{label}: {field_object.get_unsupported_lookup_message()}")

    @staticmethod
    def _get_registered_lookup(field_object: Field[Any], lookup_name: str) -> RegisteredLookup | None:
        """The ``register_lookup()`` lookup of this name on the field's class or a base class."""
        if not lookup_name:
            return None
        for field_class in type(field_object).__mro__:
            registered_lookup = field_class.__dict__.get("registered_lookups", {}).get(lookup_name)
            if registered_lookup is not None:
                return cast("RegisteredLookup", registered_lookup)
        return None

    @staticmethod
    def _get_lookup(lookup_name: str) -> Lookup | str:
        """A built-in lookup as a ``Lookup``, a custom one by its name."""
        try:
            return Lookup(lookup_name)
        except ValueError:
            return lookup_name

    @staticmethod
    def _get_plain_value_shape(lookup: Lookup | str) -> LookupValueShape:
        """What the value of a lookup comparing plain values looks like."""
        if lookup in LIST_VALUE_LOOKUPS:
            return LookupValueShape.LIST
        if lookup == Lookup.RANGE:
            return LookupValueShape.RANGE
        return LookupValueShape.VALUE

    @classmethod
    def _get_field_value_description(
        cls, value_field: Field[Any], lookup: Lookup | str
    ) -> tuple[LookupValueShape, Any]:
        """What the value of a lookup on a field's value looks like, and its type."""
        if lookup in BOOLEAN_VALUE_LOOKUPS:
            return LookupValueShape.VALUE, bool
        own_description = value_field.get_lookup_value_description(lookup)
        if own_description is not None:
            return own_description
        if lookup == Lookup.LENGTH:
            return LookupValueShape.VALUE, int
        if lookup == Lookup.HAS_KEY:
            return LookupValueShape.VALUE, str
        if lookup in KEY_LIST_LOOKUPS:
            return LookupValueShape.LIST, str
        if lookup in TEXT_VALUE_LOOKUPS:
            return LookupValueShape.VALUE, str
        return cls._get_plain_value_shape(lookup), value_field.field_type

    @classmethod
    def _get_json_value_description(cls, lookup: Lookup | str, *, whole_value: bool) -> tuple[LookupValueShape, Any]:
        """What the value of a lookup on a JSON value looks like, and its type."""
        if lookup in BOOLEAN_VALUE_LOOKUPS:
            return LookupValueShape.VALUE, bool
        if lookup == Lookup.HAS_KEY:
            return LookupValueShape.VALUE, str
        if lookup in KEY_LIST_LOOKUPS:
            return LookupValueShape.LIST, str
        if lookup == Lookup.FILTER:
            return LookupValueShape.VALUE, dict
        if lookup in JSON_PATH_LIST_LOOKUPS:
            return cls._get_plain_value_shape(lookup), object
        if not whole_value and lookup in TEXT_VALUE_LOOKUPS:
            return LookupValueShape.VALUE, str
        return LookupValueShape.VALUE, object

    @staticmethod
    def _crosses_to_many(relations: tuple[Field[Any], ...]) -> bool:
        return any(getattr(relation, "is_multi_valued", False) for relation in relations)

    @staticmethod
    def _get_dialects(*fields: Field[Any] | None) -> frozenset[str] | None:
        """The dialects every field exists on, None for every dialect."""
        dialects: frozenset[str] | None = None
        for field_object in fields:
            # A field taking its column type from the dialect is checked against the one dialect a
            # query runs on (FilterOperators.supports_lookup()).
            if field_object is None or field_object.SUPPORTED_DIALECTS is None:
                continue
            dialects = (
                field_object.SUPPORTED_DIALECTS if dialects is None else dialects & field_object.SUPPORTED_DIALECTS
            )
        return dialects
