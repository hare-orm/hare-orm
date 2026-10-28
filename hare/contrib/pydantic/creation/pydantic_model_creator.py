from __future__ import annotations

from base64 import b32encode
from hashlib import sha3_224
from typing import TYPE_CHECKING, Any, ClassVar, TypeAlias, cast

from pydantic import ConfigDict, Field as PydanticField, create_model
from pydantic.fields import ComputedFieldInfo

from hare import (
    BackwardForeignKeyRelation,
    BackwardOneToOneRelation,
    ForeignKeyFieldInstance,
    OneToOneFieldInstance,
)
from hare.contrib.pydantic.constants import (
    HASH_LENGTH,
    MODEL_INDEX_MAX_SIZE,
    PYDANTIC_MODELS_MODULE,
    QUERYSET_MODEL_INDEX_MAX_SIZE,
)
from hare.contrib.pydantic.creation.computed_field_schemas import ComputedFieldSchemas
from hare.contrib.pydantic.creation.data_field_schemas import DataFieldSchemas
from hare.contrib.pydantic.creation.extra_field_schemas import ExtraFieldSchemas
from hare.contrib.pydantic.creation.field_descriptions import FieldDescriptions
from hare.contrib.pydantic.creation.field_map import FieldMap
from hare.contrib.pydantic.creation.model_annotations import ModelAnnotations
from hare.contrib.pydantic.creation.pydantic_meta_reading import PydanticMetaReading
from hare.contrib.pydantic.creation.relation_field_schemas import RelationFieldSchemas
from hare.contrib.pydantic.descriptions.computed_field_description import ComputedFieldDescription
from hare.contrib.pydantic.descriptions.model_description import ModelDescription
from hare.contrib.pydantic.models.pydantic_list_model import PydanticListModel
from hare.contrib.pydantic.models.pydantic_model import PydanticModel
from hare.core.caching.cache import Cache
from hare.exceptions import ConfigurationError
from hare.fields import Field, JSONField
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance

if TYPE_CHECKING:
    from hare.models import Model


# Type alias for a single entry in the recursion stack: (model_class, field_name, max_recursion)
StackEntry: TypeAlias = tuple["type[Model]", str, int]


# Type alias for property values stored in _properties.
# Regular fields are stored as (type, FieldInfo) tuples; computed fields as decorator instances.
PropertyValue: TypeAlias = "tuple[type, Any] | Any"


class PydanticModelCreator:
    """Builds the Pydantic schema of a model - its fields, relations and computed fields as the model's
    ``PydanticMeta`` and the call's options take them - and keeps each built schema for the next call."""

    #: (model, index key) -> the Pydantic model generated from the model. Dropped with the model: a
    #: re-registered class for the same table would otherwise get the old class's schema, whose
    #: ``source_model`` still points at the unregistered class. The key is a hash of what shapes the
    #: schema - the model's schema and table (two classes a factory builds share their qualified name),
    #: its qualified name, its fields and computed fields, the relational fields with the names of
    #: their schemas, ``optional``, ``allow_cycles``, ``exclude_read_only``, the model config, the
    #: validators, the module and each further option set - plus the schema's name: a ``:leaf``
    #: submodel or an explicit ``name=`` shares its root schema's hash. The hash is known only once
    #: the model is analysed whole.
    MODEL_INDEX: ClassVar[Cache[type[PydanticModel]]] = Cache(MODEL_INDEX_MAX_SIZE, holds_sql=False)
    #: (model, its Pydantic model, list name) -> the list wrapper around the Pydantic model.
    QUERYSET_MODEL_INDEX: ClassVar[Cache[type[PydanticListModel]]] = Cache(
        QUERYSET_MODEL_INDEX_MAX_SIZE, holds_sql=False
    )

    def __init__(
        self,
        model: type[Model],
        name: str | None = None,
        exclude: tuple[str, ...] | None = None,
        include: tuple[str, ...] | None = None,
        computed: tuple[str, ...] | None = None,
        extra_fields: dict[str, Any] | None = None,
        related_values: dict[str, str] | None = None,
        optional: tuple[str, ...] | None = None,
        allow_cycles: bool | None = None,
        sort_alphabetically: bool | None = None,
        exclude_readonly: bool = False,
        meta_override: type | None = None,
        model_config: ConfigDict | None = None,
        validators: dict[str, Any] | None = None,
        module: str = PYDANTIC_MODELS_MODULE,
        exclude_sensitive: bool = False,
        relations_as_ids: bool = False,
        _stack: tuple[StackEntry, ...] = (),
        _as_submodel: bool = False,
        _max_recursion: int | None = None,
    ) -> None:
        self._model_class: type[Model] = model
        self._stack: tuple[StackEntry, ...] = _stack
        self._is_default: bool = (
            exclude is None
            and include is None
            and computed is None
            and extra_fields is None
            and related_values is None
            and optional is None
            and sort_alphabetically is None
            and allow_cycles is None
            and meta_override is None
            and not exclude_readonly
            and not exclude_sensitive
            and not relations_as_ids
            and model_config is None
            and validators is None
            and module == PYDANTIC_MODELS_MODULE
        )
        if exclude is None:
            exclude = ()
        self._exclude_sensitive: bool = exclude_sensitive
        if exclude_sensitive:
            exclude = tuple(exclude) + tuple(sorted(model._meta.sensitive_fields))
        if include is None:
            include = ()
        if computed is None:
            computed = ()
        if optional is None:
            optional = ()
        self._relations_as_ids: bool = relations_as_ids
        if relations_as_ids:
            optional = RelationFieldSchemas.add_relation_id_names(model, optional)

        self.meta = PydanticMetaReading.get_meta(
            model,
            meta_override=meta_override,
            exclude=exclude,
            include=include,
            computed=computed,
            extra_fields=extra_fields,
            related_values=related_values,
            allow_cycles=allow_cycles,
            sort_alphabetically=sort_alphabetically,
            max_recursion=_max_recursion,
            model_config=model_config,
        )
        PydanticModelCreator.raise_if_unknown_names(
            model,
            {"include": include, "exclude": exclude, "optional": optional},
            computed,
            {*self.meta.model_computed, *self.meta.model_extra_fields, *self.meta.model_related_values},
        )
        ExtraFieldSchemas.raise_if_wrong(
            model,
            self.meta.extra_fields,
            self.meta.related_values,
            {*model._meta.fields_map, *model._meta.generic_foreign_key_fields, *self.meta.model_computed},
        )

        self._exclude_read_only: bool = exclude_readonly

        self._fqname = model.__module__ + "." + model.__qualname__
        self._name: str
        self._title: str
        self.given_name = name
        self.__hash: str = ""

        self._as_submodel = _as_submodel

        self._annotations = ModelAnnotations.get(model)

        self._pconfig: ConfigDict

        self._properties: dict[str, PropertyValue] = dict()
        self._relational_fields_index: list[tuple[str, str]] = list()

        self._model_description: ModelDescription = ModelDescription.from_model(model)

        self._field_map: FieldMap = self._initialize_field_map()
        self._construct_field_map()

        self._optional = optional

        self._validators = validators
        self._module = module
        #: The ``before`` validators of the generic foreign keys in the schema, by validator name.
        self._generic_field_validators: dict[str, Any] = {}
        #: The fields the database or the ORM fills that take no NULL - left out of a dump while None.
        self._unset_when_none: list[str] = []

    @property
    def _hash(self) -> str:
        if self.__hash == "":
            field_info = []
            computed_field_info = []
            for name, prop in self._properties.items():
                if isinstance(prop, tuple):
                    field_info.append(f"{name}:{prop[0]}")
                else:
                    computed_field_info.append(f"{name}:computed")
            # Only the computed fields - a trailing block - are sorted among themselves, so their
            # declared order doesn't change the hash.
            field_info += sorted(computed_field_info)
            # (schema, db_table) tells apart classes that would hash alike - a stable identity
            # across runs, unlike id().
            hashval = (
                f"{self._model_class._meta.schema};{self._model_class._meta.db_table};"
                f"{self._fqname};"
                f"{field_info};"
                f"{self._relational_fields_index};"
                f"{self._optional};"
                f"{self.meta.allow_cycles};"
                f"{self._exclude_read_only};"
                f"{tuple(sorted(self.meta.computed))};"
                f"{self.meta.model_config};"
                f"{self._validators};"
                f"{self._module}"
            )
            # Each further option is appended only when set.
            if self._exclude_sensitive:
                hashval += ";exclude_sensitive"
            if self._relations_as_ids:
                hashval += ";relations_as_ids"
            if self.meta.extra_fields:
                extra_fields = sorted((name, repr(annotation)) for name, annotation in self.meta.extra_fields.items())
                hashval += f";extra_fields={extra_fields}"
            if self.meta.related_values:
                hashval += f";related_values={sorted(self.meta.related_values.items())}"
            self.__hash = b32encode(sha3_224(hashval.encode("utf-8")).digest()).decode("utf-8").lower()[:HASH_LENGTH]
        return self.__hash

    @staticmethod
    def raise_if_unknown_names(
        model: type[Model],
        names_by_argument: dict[str, tuple[str, ...]],
        computed: tuple[str, ...],
        declared_names: set[str],
    ) -> None:
        """Refuses a name given to the creator that names nothing of the model - a misspelt ``exclude``
        would leave the field it means in the schema. A path into a relation is checked by the
        relation's own creator.

        Args:
            model: The model.
            names_by_argument: The names and paths given as ``include``/``exclude``/``optional``.
            computed: The names and paths given as ``computed``.
            declared_names: The computed fields, extra fields and related values declared on this schema.

        Raises:
            ConfigurationError: A name is neither a field, a relation nor a computed field of the
                model, or a computed name isn't a method or property of it.
        """
        meta = model._meta
        known_names = {*meta.fields_map, *meta.generic_foreign_key_fields, *declared_names}
        for argument_name, names in names_by_argument.items():
            for name in names:
                if name.split(".", 1)[0] not in known_names:
                    raise ConfigurationError(
                        f"pydantic_model_creator({model.__name__}, {argument_name}=...): {name!r} names no field, "
                        "relation or computed field of the model"
                    )
        for name in computed:
            relation_name, _, rest = name.partition(".")
            if rest and relation_name not in meta.fetch_fields:
                raise ConfigurationError(
                    f"pydantic_model_creator({model.__name__}, computed=...): {name!r} - {relation_name!r} is no "
                    "relation of the model"
                )
            if not rest and not hasattr(model, name):
                raise ConfigurationError(
                    f"pydantic_model_creator({model.__name__}, computed=...): {name!r} is no method or property of "
                    "the model"
                )

    def get_name(self) -> tuple[str, str]:
        # Arguments other than the defaults append a hash to the class name, to make it unique.
        # Cycles are renamed explicitly.
        if self.given_name is not None:
            return self.given_name, self.given_name
        name = f"{self._fqname}:{self._hash}" if not self._is_default else self._fqname
        name = f"{name}:leaf" if self._as_submodel else name
        return name, self._model_class.__name__

    def _initialize_field_map(self) -> FieldMap:
        return FieldMap(
            self.meta,
            pk_fields=DataFieldSchemas.get_caller_supplied_pk_fields(self),
            model_fields_map=self._model_class._meta.fields_map,
            relations_as_ids=self._relations_as_ids,
        )

    def _construct_field_map(self) -> None:
        self._field_map.field_map_update(fields=self._model_description.data_fields, meta=self.meta)
        # Forward relations are never assigned by the database - exclude_readonly keeps them;
        # whether each is required is decided per field.
        for fields in (
            self._model_description.foreign_key_fields,
            self._model_description.one_to_one_fields,
            self._model_description.many_to_many_fields,
        ):
            self._field_map.field_map_update(fields, self.meta)
        if not self._exclude_read_only:
            # A reverse relation has no column on this row, so a flat (relations_as_ids) schema
            # leaves it out.
            if self._relations_as_ids:
                pass
            elif self.meta.backward_relations:
                for fields in (
                    self._model_description.backward_foreign_key_fields,
                    self._model_description.backward_one_to_one_fields,
                ):
                    self._field_map.field_map_update(fields, self.meta)
            else:
                # Include only explicitly annotated backward relations
                for fields in (
                    self._model_description.backward_foreign_key_fields,
                    self._model_description.backward_one_to_one_fields,
                ):
                    annotated = [field for field in fields if field.model_field_name in self._annotations]
                    if annotated:
                        self._field_map.field_map_update(annotated, self.meta)
            self._field_map.computed_field_map_update(self.meta.model_computed, self._model_class, self.meta)
        if self.meta.sort_alphabetically:
            self._field_map.sort_alphabetically()
        else:
            self._field_map.sort_definition_order(self._model_class, self.meta.model_computed)

    def create_pydantic_model(self) -> type[PydanticModel]:
        """Builds the schema of the model - or gives the one kept under the same hash and name.

        Returns:
            The Pydantic model.
        """
        for field_name, field in self._field_map.items():
            # A non-primary-key generated column is assigned by the database, like the primary key.
            if self._exclude_read_only and isinstance(field, Field) and field.generated:
                continue
            self._process_field(field_name, field)
        RelationFieldSchemas.add_generic_foreign_keys(self)
        ExtraFieldSchemas.add_to_schema(self)

        self._name, self._title = self.get_name()

        # Keyed by hash AND name: a root schema and the ":leaf" submodel of the same model (or
        # two given name= values) can share one hash while being different schemas.
        model_index_key = (self._model_class, f"{self._hash};{self._name}")
        if (hashed_model := PydanticModelCreator.MODEL_INDEX.get(model_index_key)) is not None:
            return cast("type[PydanticModel]", hashed_model)

        self._pconfig = PydanticMetaReading.initialize_pconfig(self)
        computed_fields: dict[str, Any] = {}
        common_fields: dict[str, Any] = {}
        for property_name, property_value in self._properties.items():
            if isinstance(getattr(property_value, "decorator_info", None), ComputedFieldInfo):
                computed_fields[property_name] = property_value
            else:
                common_fields[property_name] = property_value
        base_namespace: dict[str, Any] = {"model_config": self._pconfig, **computed_fields}
        if self._unset_when_none:
            base_namespace["drop_unset_values"] = DataFieldSchemas.get_unset_value_dropping_serializer(
                frozenset(self._unset_when_none),
                uses_aliases=bool(self._pconfig.get("alias_generator"))
                or any(getattr(common_fields[name][1], "serialization_alias", None) for name in self._unset_when_none),
            )
        base_model = type("BasePydanticModel", (PydanticModel,), base_namespace)
        model: type[PydanticModel] = create_model(
            self._name,
            __base__=base_model,
            __module__=self._module,
            __validators__={
                **DataFieldSchemas.build_orm_field_validators(self),
                **self._generic_field_validators,
                **(self._validators or {}),
            },
            **common_fields,
        )
        model.__doc__ = FieldDescriptions.get_clean_docstring(self._model_class)
        model.source_model = self._model_class
        # The relations a related value reads through - from_hare_orm()/from_queryset() fetch them.
        model.related_value_paths = (
            () if self._exclude_read_only else tuple(ExtraFieldSchemas.get_shown_related_values(self).values())
        )
        PydanticModelCreator.MODEL_INDEX[model_index_key] = model
        return model

    def _process_field(
        self,
        field_name: str,
        field: Field[Any] | ComputedFieldDescription,
    ) -> None:
        if isinstance(field, Field):
            self._process_orm_field(field_name, field)
        elif isinstance(field, ComputedFieldDescription):
            ComputedFieldSchemas.process_computed_field_entry(self, field_name, field)

    def _process_orm_field(self, field_name: str, field: Field[Any]) -> None:
        json_schema_extra: dict[str, Any] = {}
        fconfig: dict[str, Any] = {
            "json_schema_extra": json_schema_extra,
        }
        field_property, _ = self._process_normal_field(field_name, field, json_schema_extra, fconfig)
        if field_property:
            fconfig["title"] = field_name.replace("_", " ").title()
            description = FieldDescriptions.replace_newlines_with_br(field.docstring or field.description or "")
            if description:
                fconfig["description"] = description
            # A non-primary-key generated column is filled by the database - not required. An
            # auto-increment primary key keeps its own rule.
            non_pk_generated = field.generated and not field.pk
            if non_pk_generated:
                json_schema_extra["readOnly"] = True
            # auto_now/auto_now_add are filled on save - not required.
            auto_now_field = bool(getattr(field, "auto_now", False) or getattr(field, "auto_now_add", False))
            # Meta.tenant_field is filled from the active tenant on save - not required.
            is_tenant_field = DataFieldSchemas.is_tenant_field(self, field_name, field)
            if (
                field_name in self._optional
                or field.has_db_default()
                or non_pk_generated
                or auto_now_field
                or field.default is not None
                or is_tenant_field
            ):
                # A callable default becomes default_factory, as Model.__init__ calls it.
                if field._default_is_coroutine:
                    # An async default can't run in default_factory - None until a value exists, as
                    # Model.__iter__() reports it.
                    pydantic_field = PydanticField(default=None, **fconfig)
                elif field.default is not None and callable(field.default):
                    pydantic_field = PydanticField(default_factory=field.default, **fconfig)
                else:
                    pydantic_field = PydanticField(default=field.default, **fconfig)
                    if field.default is None and not field.null and field_name not in self._optional:
                        # None stands for "not given": a create from the dump leaves the value to
                        # the database or the ORM rather than writing a NULL the column refuses.
                        self._unset_when_none.append(field_name)
                self._properties[field_name] = (field_property, pydantic_field)
            else:
                if json_schema_extra.get("nullable") or (
                    self._exclude_read_only and json_schema_extra.get("readOnly")
                ):
                    # see: https://docs.pydantic.dev/latest/migration/#required-optional-and-nullable-fields
                    fconfig["default"] = None
                self._properties[field_name] = (field_property, PydanticField(**fconfig))

    def _process_normal_field(
        self,
        field_name: str,
        field: Field[Any],
        json_schema_extra: dict[str, Any],
        fconfig: dict[str, Any],
    ) -> tuple[Any, bool]:
        if isinstance(field, (ForeignKeyFieldInstance, OneToOneFieldInstance, BackwardOneToOneRelation)):
            return RelationFieldSchemas.process_single_field_relation(self, field_name, field, json_schema_extra), True
        if isinstance(field, (BackwardForeignKeyRelation, ManyToManyFieldInstance)):
            return RelationFieldSchemas.process_many_field_relation(self, field_name, field), False
        if isinstance(json_field := DataFieldSchemas.unwrap_generated_field(field), JSONField):
            # A JSONField(null=True) is marked nullable here - it doesn't go through
            # _process_data_field. Its type is field_type when declared, else Any.
            if field.null:
                json_schema_extra["nullable"] = True
            declared_value_type = json_field.declared_value_type
            if not declared_value_type:
                return Any, False
            if field.null or field_name in self._optional:
                declared_value_type = declared_value_type | None
            return self._annotations.get(field_name) or declared_value_type, False
        return DataFieldSchemas.process_data_field(self, field_name, field, json_schema_extra, fconfig), False
