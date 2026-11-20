from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.cache import Cache
from hare.exceptions import FieldError, QueryError
from hare.fields.data.json.json_field import JSONField
from hare.fields.encrypted.encrypted_json_field import EncryptedJSONField
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.constants import PRIMARY_KEY_ALIAS
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

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.fields.registrations.registered_lookup import RegisteredLookup
    from hare.models import Model
    from hare.models.model_binding import ModelBinding


class LookupInfoBuilder:
    """Reads filter keys and ordering names through a model's fields and relations. The descriptions
    are cached per model and, for a key starting with an annotation, per queryset; a change to any
    model's fields, lookups or relations drops them all.
    """

    #: A model's descriptions, in a bucket the model keeps: filter key -> ``get_lookup_info()``,
    #: ordering name -> ``get_ordering_info()``, (path, dialect name) -> ``get_lookups()``. A
    #: description follows relations into other models, so a change to any model drops them all.
    lookup_infos: ClassVar[Cache[Any]] = Cache(depends_on_other_models=True)
    ordering_infos: ClassVar[Cache[Any]] = Cache(depends_on_other_models=True)
    lookups_by_path: ClassVar[Cache[Any]] = Cache(depends_on_other_models=True)

    #: The descriptions of keys starting with an annotation, in a bucket a queryset shares with
    #: its clones: (description type, key, dialect name) -> the description.
    annotation_descriptions: ClassVar[Cache[Any]] = Cache(depends_on_other_models=True)

    @classmethod
    def get_lookup_info(
        cls,
        binding: ModelBinding,
        key: str,
        *,
        annotations: Mapping[str, Any] | None = None,
        annotation_fields: Mapping[str, Field[Any] | None] | None = None,
        label: str | None = None,
    ) -> LookupInfo:
        """Describes a ``.filter()`` key.

        Args:
            binding: The model the key starts at.
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
        if "" in segments:
            raise FieldError(f"{label}: an empty name between or around '__'")
        generic_lookup_info = GenericForeignKeyPaths.get_lookup_info(binding, key)
        if generic_lookup_info is not None:
            return generic_lookup_info
        if annotations is not None and segments[0] in annotations:
            return cls._get_annotation_info(
                binding,
                key,
                segments,
                (annotation_fields or {}).get(segments[0]),
                label,
            )
        relations: list[Field[Any]] = []
        current_binding = binding
        position = 0
        while True:
            segment = segments[position]
            rest = segments[position + 1 :]
            meta = current_binding
            if segment == PRIMARY_KEY_ALIAS and not meta.has_primary_key:
                raise FieldError(
                    f"{label}: {current_binding._model.__name__} has no primary key (Meta.primary_key = None)"
                )
            if segment == PRIMARY_KEY_ALIAS and meta.has_composite_primary_key:
                return cls._get_composite_pk_info(
                    LookupKeyPosition(binding, key, label, current_binding, tuple(relations)), rest
                )
            if segment == PRIMARY_KEY_ALIAS:
                segment, field_object = cls._get_primary_key_lookup_field(meta)
            else:
                field_object = meta.fields_map.get(segment)
            if field_object is None:
                raise FieldError(f"{label}: {current_binding._model.__name__} has no field {segment!r}")
            if segment not in meta.fetch_fields:
                return cls._get_field_info(
                    LookupKeyPosition(binding, key, label, current_binding, tuple(relations)),
                    segment,
                    field_object,
                    rest,
                )
            relation: RelationalField[Model] = field_object  # type: ignore[assignment]
            relation_lookup = cls._get_relation_lookup(binding, relation, rest, key)
            if relation_lookup is not None:
                return cls._get_relation_info(
                    LookupKeyPosition(binding, key, label, current_binding, (*relations, relation)),
                    relation,
                    relation_lookup,
                )
            relations.append(relation)
            current_binding = relation.get_related_binding()
            position += 1

    @classmethod
    def get_lookups(
        cls,
        binding: ModelBinding,
        path: str,
        dialect: Dialect,
        *,
        annotations: Mapping[str, Any] | None = None,
        annotation_fields: Mapping[str, Field[Any] | None] | None = None,
    ) -> dict[str, LookupInfo]:
        """Describes every lookup of a field that a dialect runs.

        Args:
            binding: The model the path starts at.
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
        suffixes = cls._get_lookup_suffixes(binding, path, annotations)
        lookups: dict[str, LookupInfo] = {}
        for suffix in suffixes:
            lookup_info = cls.get_lookup_info(
                binding,
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
        binding: ModelBinding,
        name: str,
        *,
        annotations: Mapping[str, Any] | None = None,
        annotation_fields: Mapping[str, Field[Any] | None] | None = None,
        label: str | None = None,
    ) -> OrderingInfo:
        """Describes an ``.order_by()`` name.

        Args:
            binding: The model the name starts at.
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
            annotation_field = (annotation_fields or {}).get(annotation_name)
            annotation_path = (
                path.removeprefix(annotation_name).removeprefix("__").split("__") if path != annotation_name else []
            )
            if annotation_field is not None and annotation_path:
                if not isinstance(GeneratedField.get_effective_field(annotation_field), JSONField):
                    raise FieldError(
                        f"{label}: annotation {annotation_name!r} has no path {'__'.join(annotation_path)!r}"
                    )
            return OrderingInfo(
                name=name,
                binding=binding,
                relations=(),
                fields=(annotation_field,),
                paths=(path,),
                transforms=tuple(annotation_path),
                descending=descending,
                crosses_to_many=False,
            )
        relations: list[Field[Any]] = []
        current_binding = binding
        for position, segment in enumerate(segments):
            meta = current_binding
            prefix = "".join(f"{relation_segment}__" for relation_segment in segments[:position])
            is_last = position == len(segments) - 1
            if segment == PRIMARY_KEY_ALIAS:
                if not meta.has_primary_key:
                    raise FieldError(
                        f"{label}: {current_binding._model.__name__} has no primary key (Meta.primary_key = None)"
                    )
                if not is_last:
                    raise FieldError(
                        f"{label}: {current_binding._model.__name__}.pk has no path "
                        f"'{'__'.join(segments[position + 1 :])}'"
                    )
                key_fields = meta.pk_fields
                # The model's own pk orders by its key field(s); a related one (``author__pk``) is
                # read through the relation as named.
                key_paths = tuple(key_field.model_field_name for key_field in key_fields) if not prefix else (path,)
                return cls._get_ordering(name, binding, relations, key_fields, key_paths, (), descending)
            field_object = meta.fields_map.get(segment)
            if field_object is None:
                raise FieldError(f"{label}: {current_binding._model.__name__} has no field {segment!r}")
            if segment in meta.fetch_fields:
                relation: RelationalField[Model] = field_object  # type: ignore[assignment]
                if not is_last:
                    relations.append(relation)
                    current_binding = relation.get_related_binding()
                    continue
                if segment in meta.foreign_key_fields or segment in meta.one_to_one_fields:
                    # A forward relation orders by its own key column(s), with no join.
                    source_field_names = relation.source_fields
                    source_fields = tuple(meta.fields_map[source_field] for source_field in source_field_names)
                    return cls._get_ordering(
                        name,
                        binding,
                        relations,
                        source_fields,
                        tuple(f"{prefix}{source}" for source in source_field_names),
                        (),
                        descending,
                    )
                # A reverse or many-to-many relation orders by the related primary key.
                related_meta = relation.get_related_binding()
                key_fields = related_meta.pk_fields
                return cls._get_ordering(name, binding, [*relations, relation], key_fields, (path,), (), descending)
            transforms = tuple(segments[position + 1 :])
            if transforms:
                cls._raise_if_not_value_path(binding, segment, field_object, list(transforms), label)
            return cls._get_ordering(name, binding, relations, (field_object,), (path,), transforms, descending)
        raise FieldError(f"{label}: an ordering name can't be empty")

    @staticmethod
    def _get_ordering(
        name: str,
        binding: ModelBinding,
        relations: list[Field[Any]],
        fields: tuple[Field[Any], ...],
        paths: tuple[str, ...],
        transforms: tuple[str, ...],
        descending: bool,
    ) -> OrderingInfo:
        return OrderingInfo(
            name=name,
            binding=binding,
            relations=tuple(relations),
            fields=fields,
            paths=paths,
            transforms=transforms,
            descending=descending,
            crosses_to_many=any(getattr(relation, "is_multi_valued", False) for relation in relations),
        )

    @staticmethod
    def _raise_if_not_value_path(
        binding: ModelBinding, name: str, field_object: Field[Any], path: list[str], label: str
    ) -> None:
        """Rejects a path inside a field whose value has none - only a JSON value has keys, and an
        array, range, text or key-value value its transforms.

        Raises:
            FieldError: The field's value has no such path.
        """
        effective_field = GeneratedField.get_effective_field(field_object)
        if isinstance(effective_field, JSONField):
            return
        __, output_field, rest = FieldTransforms.get_path(field_object, path)
        if len(rest) == 1 and rest[0] in ValuePaths.get_date_part_segments(output_field):
            return
        if rest:
            raise FieldError(f"{label}: {binding._model.__name__}.{name} has no path {'__'.join(path)!r}")

    @staticmethod
    def _get_relation_lookup(
        binding: ModelBinding, relation: RelationalField[Model], rest: list[str], key: str
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
        related_meta = relation.get_related_binding()
        if lookup_name == PRIMARY_KEY_ALIAS or lookup_name in related_meta.fields_map:
            return None
        relation_name = relation.model_field_name
        if relation_name in binding.foreign_key_fields or relation_name in binding.one_to_one_fields:
            source_field_names = relation.source_fields
            source_fields = [binding.fields_map[source_field] for source_field in source_field_names]
            if len(source_fields) == 1:
                return lookup_name if lookup_name in FieldLookups.get(source_fields[0]) else None
            if lookup_name in TO_MANY_RELATION_SHORTCUT_LOOKUPS:
                return lookup_name
            if all(lookup_name in FieldLookups.get(source_field) for source_field in source_fields):
                source_filter_keys = [f"{source_field}__{lookup_name}" for source_field in source_field_names]
                raise QueryError(
                    f"{key!r} - {binding._model.__name__}.{relation_name} has a composite key, so a "
                    f"lookup on "
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
        owner_meta = position.owner_binding
        related_meta = relation.get_related_binding()
        relation_name = relation.model_field_name
        is_forward = relation_name in owner_meta.foreign_key_fields or relation_name in owner_meta.one_to_one_fields
        has_primary_key = related_meta.has_primary_key
        if not has_primary_key and lookup_name not in BOOLEAN_VALUE_LOOKUPS:
            raise FieldError(
                f"{position.label}: {related_meta._model.__name__} has no primary key "
                f"(Meta.primary_key = None) - "
                f"its rows can't be compared as a whole; test {relation_name}__isnull or filter "
                "through its fields"
            )
        if not has_primary_key:
            # IS [NOT] NULL is tested on the related row's key column to this one.
            backward_relation: BackwardForeignKeyRelation[Model] = relation  # type: ignore[assignment]
            key_field = related_meta.fields_map[
                related_meta.fields_db_projection_reverse[backward_relation.relation_source_fields[0]]
            ]
            return LookupInfo(
                key=position.key,
                binding=position.binding,
                relations=position.relations,
                field=key_field,
                transforms=(),
                lookup=cls._get_lookup(lookup_name),
                value_shape=LookupValueShape.VALUE,
                value_type=bool,
                crosses_to_many=cls._crosses_to_many(position.relations),
                requires_extension=None,
                target=LookupTarget.RELATION,
                field_lookup=FieldLookups.get(relation).get(lookup_name),
            )
        key_fields: tuple[Field[Any], ...] = relation.to_field_instances if is_forward else related_meta.pk_fields
        field_lookup: FieldLookup | None
        value_field: Field[Any] | None = None
        requires_extension = None
        if is_forward and len(source_fields := relation.source_fields) == 1:
            value_field = owner_meta.fields_map[source_fields[0]]
            cls._raise_if_rejected(value_field, lookup_name, position.label)
            field_lookup = FieldLookups.get(value_field)[lookup_name]
            # The key column's own lookup (a trigram or text search one) - its extension too.
            registered_lookup = cls._get_registered_lookup(value_field, lookup_name)
            if registered_lookup is not None:
                requires_extension = registered_lookup.required_extension
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
            binding=position.binding,
            relations=position.relations,
            field=key_fields[0] if len(key_fields) == 1 else key_fields,
            transforms=(),
            lookup=lookup,
            value_shape=cls._get_plain_value_shape(lookup),
            value_type=value_type,
            crosses_to_many=cls._crosses_to_many(position.relations),
            requires_extension=requires_extension,
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
        meta = position.owner_binding
        if rest not in ([], [Lookup.IN], [Lookup.NOT], [Lookup.NOT_IN]):
            raise FieldError(
                f"{position.label}: {position.owner_binding._model.__name__}.pk is a composite primary key - it takes "
                f"pk=, pk__in=, pk__not= and pk__not_in=, not {'__'.join(rest)!r}"
            )
        lookup = Lookup(rest[0]) if rest else Lookup.EXACT
        return LookupInfo(
            key=position.key,
            binding=position.binding,
            relations=position.relations,
            field=meta.pk_fields,
            transforms=(),
            lookup=lookup,
            value_shape=LookupValueShape.LIST if lookup in {Lookup.IN, Lookup.NOT_IN} else LookupValueShape.VALUE,
            value_type=tuple(pk_field.field_type for pk_field in meta.pk_fields),
            crosses_to_many=cls._crosses_to_many(position.relations),
            requires_extension=None,
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
        lookup_field = field_object
        remaining = rest
        if suffix not in FieldLookups.get(field_object):
            # A path through an array, range, text or key-value value, then the lookup of what it reads.
            term_transforms, output_field, remaining = FieldTransforms.get_path(field_object, rest)
            if not term_transforms:
                raise FieldError(
                    f"{position.label}: {position.owner_binding._model.__name__}.{name} has no lookup {suffix!r}"
                )
            transforms = tuple(rest[: len(rest) - len(remaining)])
            lookup_field = output_field
            suffix = "__".join(remaining)
        value_field = GeneratedField.get_effective_field(lookup_field)
        if remaining and remaining[0] in DATE_TRANSFORM_VALUE_TYPES:
            transforms = (*transforms, remaining[0])
            lookup_name = remaining[1] if len(remaining) == 2 else ""
            date_value_type = DATE_TRANSFORM_VALUE_TYPES[remaining[0]]
        else:
            lookup_name = suffix
        field_lookup = FieldLookups.get(lookup_field).get(suffix)
        if field_lookup is None:
            raise FieldError(
                f"{position.label}: {position.owner_binding._model.__name__}.{name} has no lookup {'__'.join(rest)!r}"
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
        return LookupInfo(
            key=position.key,
            binding=position.binding,
            relations=position.relations,
            field=field_object,
            transforms=transforms,
            lookup=lookup,
            value_shape=value_shape,
            value_type=value_type,
            crosses_to_many=cls._crosses_to_many(position.relations),
            requires_extension=requires_extension,
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
        path_lookup_suffix = JsonPathLookups.get_lookup_suffix(rest)
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
        elif len(rest) == 1 and path_lookup_suffix is not None:
            # A lookup name right after the field is that lookup of the whole value, never a key.
            raise FieldError(
                f"{position.label}: {position.owner_binding._model.__name__}.{name} has no lookup {rest[0]!r}"
            )
        else:
            if isinstance(field_object, EncryptedJSONField):
                raise FieldError(
                    f"{position.label}: {field_object.get_field_label()} is encrypted - a key path would read a "
                    "stored Fernet token, not the key's value. Filter on the whole field instead."
                )
            transforms, lookup = cls._get_json_path_parts(rest, path_lookup_suffix)
            field_lookup = JsonPathLookups.get_lookups()[lookup]
            value_shape, value_type = cls._get_json_value_description(lookup, whole_value=False)
            target = LookupTarget.JSON_PATH
        return LookupInfo(
            key=position.key,
            binding=position.binding,
            relations=position.relations,
            field=field_object,
            transforms=transforms,
            lookup=lookup,
            value_shape=value_shape,
            value_type=value_type,
            crosses_to_many=cls._crosses_to_many(position.relations),
            requires_extension=None,
            target=target,
            field_lookup=field_lookup,
            value_field=field_object,
        )

    @classmethod
    def _get_json_path_parts(cls, segments: list[str], suffix: str | None) -> tuple[tuple[str, ...], Lookup | str]:
        """A terminal lookup follows at least one JSON key."""
        if len(segments) > 1 and suffix is not None:
            return tuple(segments[:-1]), cls._get_lookup(suffix)
        return tuple(segments), Lookup.EXACT

    @classmethod
    def _get_annotation_info(
        cls,
        binding: ModelBinding,
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
            transforms, lookup = cls._get_json_path_parts(rest, JsonPathLookups.get_lookup_suffix(rest))
            field_lookup = JsonPathLookups.get_lookups().get(lookup)
            value_shape, value_type = cls._get_json_value_description(lookup, whole_value=False)
        else:
            raise FieldError(f"{label}: the annotation {name!r} has no lookup {suffix!r}")
        # A lookup registered on Field itself is a lookup of an annotation too.
        registered_lookup = None if transforms else Field.get_class_lookups(Field).get(str(lookup))
        if registered_lookup is not None:
            value_shape = registered_lookup.value_shape
            value_type = registered_lookup.value_type or value_type or object
        return LookupInfo(
            key=key,
            binding=binding,
            relations=(),
            field=annotation_field,
            transforms=transforms,
            lookup=lookup,
            value_shape=value_shape,
            value_type=value_type,
            crosses_to_many=False,
            requires_extension=None if registered_lookup is None else registered_lookup.required_extension,
            target=LookupTarget.ANNOTATION,
            field_lookup=field_lookup,
            value_field=annotation_field,
        )

    @staticmethod
    def _get_primary_key_lookup_field(binding: ModelBinding) -> tuple[str, Field[Any] | None]:
        primary_key = binding.pk
        # A relation is the primary key: pk can be compared by a key or an instance.
        if primary_key is not None and (field_name := primary_key.model_field_name) in binding.fetch_fields:
            return field_name, binding.fields_map.get(field_name)
        return PRIMARY_KEY_ALIAS, primary_key

    @classmethod
    def _get_lookup_suffixes(
        cls,
        binding: ModelBinding,
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
        current_binding = binding
        for position, segment in enumerate(segments):
            meta = current_binding
            is_last = position == len(segments) - 1
            if segment == PRIMARY_KEY_ALIAS and meta.has_composite_primary_key:
                if not is_last:
                    raise FieldError(f"{binding._model.__name__} has no field path {path!r}")
                return ["", Lookup.IN, Lookup.NOT, Lookup.NOT_IN]
            if segment == PRIMARY_KEY_ALIAS:
                segment, field_object = cls._get_primary_key_lookup_field(meta)
            else:
                field_object = meta.fields_map.get(segment)
            if field_object is None:
                raise FieldError(
                    f"{binding._model.__name__} has no field path {path!r}: {current_binding._model.__name__} has no "
                    f"field {segment!r}"
                )
            if segment in meta.fetch_fields:
                relation: RelationalField[Model] = field_object  # type: ignore[assignment]
                if not is_last:
                    current_binding = relation.get_related_binding()
                    continue
                if (segment in meta.foreign_key_fields or segment in meta.one_to_one_fields) and len(
                    source_fields := relation.source_fields
                ) == 1:
                    source_field_name = source_fields[0]
                    return cls._get_field_filter_suffixes(
                        meta.fields_map[source_field_name], exclude=relation.get_related_binding().fields_map
                    )
                return [str(lookup) for lookup in TO_MANY_RELATION_SHORTCUT_LOOKUPS]
            if not is_last:
                raise FieldError(
                    f"{binding._model.__name__} has no field path {path!r}: "
                    f"{current_binding._model.__name__}.{segment} is not a relation"
                )
            return [
                *cls._get_field_filter_suffixes(field_object),
                *cls._get_path_suffixes(binding, path, field_object, ""),
            ]
        return []

    @classmethod
    def _get_path_suffixes(cls, binding: ModelBinding, path: str, field_object: Field[Any], prefix: str) -> list[str]:
        """The suffixes reading inside a field's value by a named segment (``startswith__year``,
        ``isempty``, ``span__upper_inf``) - the lookups of what each segment reads, and of its own
        segments.

        Args:
            binding: The model the path starts at.
            path: The path to the field.
            field_object: The field.
            prefix: The segments read before - ``""`` at the field itself.

        Returns:
            The suffixes after ``path`` a filter key takes.
        """
        suffixes: list[str] = []
        for segment in field_object.get_path_segments():
            path_transform = FieldTransforms.get_transform(field_object, segment)
            if path_transform is None:
                continue
            output_field = path_transform[1]
            segment_path = f"{prefix}{segment}"
            for lookup_suffix in cls._get_field_filter_suffixes(output_field):
                suffix = f"{segment_path}__{lookup_suffix}" if lookup_suffix else segment_path
                try:
                    cls.get_lookup_info(binding, f"{path}__{suffix}")
                except FieldError:
                    # A lookup the segment's value doesn't take after a path (``year__gte``).
                    continue
                suffixes.append(suffix)
            suffixes.extend(cls._get_path_suffixes(binding, path, output_field, f"{segment_path}__"))
        return suffixes

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
            registered_lookup = Field.get_class_lookups(field_class).get(lookup_name)
            if registered_lookup is not None:
                return registered_lookup
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
        value_field_description = value_field.get_lookup_value_description(lookup)
        if value_field_description is not None:
            return value_field_description
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
