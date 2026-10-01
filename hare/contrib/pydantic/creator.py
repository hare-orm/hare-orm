from __future__ import annotations

import dataclasses
import functools
import inspect
from base64 import b32encode
from collections.abc import Callable
from copy import copy
from enum import Enum
from hashlib import sha3_224
from typing import TYPE_CHECKING, Any, ClassVar, TypeAlias, cast

from pydantic import ConfigDict, Field as PydanticField, computed_field, create_model, field_validator
from pydantic.fields import ComputedFieldInfo

from hare import (
    BackwardFKRelation,
    BackwardOneToOneRelation,
    ForeignKeyFieldInstance,
    ManyToManyFieldInstance,
    OneToOneFieldInstance,
)
from hare.contrib.pydantic.base.pydantic_list_model import PydanticListModel
from hare.contrib.pydantic.base.pydantic_model import PydanticModel
from hare.contrib.pydantic.constants import HASH_LENGTH, MODEL_INDEX_MAX_SIZE, QUERYSET_MODEL_INDEX_MAX_SIZE
from hare.contrib.pydantic.descriptions.computed_field_description import ComputedFieldDescription
from hare.contrib.pydantic.descriptions.model_description import ModelDescription
from hare.contrib.pydantic.descriptions.pydantic_meta_data import PydanticMetaData
from hare.contrib.pydantic.field_map import FieldMap
from hare.contrib.pydantic.utils import ModelAnnotations
from hare.core.cache import Cache
from hare.exceptions import NoValuesFetched, ValidationError as HareValidationError
from hare.fields import Field, JSONField
from hare.fields.data.choices.char_enum_field_instance import CharEnumFieldInstance
from hare.fields.data.choices.int_enum_field_instance import IntEnumFieldInstance
from hare.fields.generated import GeneratedField
from hare.query.scopes.row_scopes import RowScopes

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


# Type alias for a single entry in the recursion stack: (model_class, field_name, max_recursion)
StackEntry: TypeAlias = tuple["type[Model]", str, int]


# Type alias for property values stored in _properties.
# Regular fields are stored as (type, FieldInfo) tuples; computed fields as decorator instances.
PropertyValue: TypeAlias = "tuple[type, Any] | Any"
"""
The index works as follows:
1. the hash is calculated from the following:
    - the model's own (schema, db_table) - two distinct classes built by the same factory
      function (the common shape for a register_live_models()-style dynamically-created model)
      share the exact same __module__/__qualname__ below, so without this a structurally
      identical second class would silently reuse the first class's cached schema, including
      its orig_model pointing at the WRONG source class.
    - the fully qualified name of the model
    - the names of the contained fields
    - the names of all relational fields and the corresponding names of the pydantic model.
      This is because if the model is not yet fully initialized, the relational fields are not yet present.
    - model_config, validators and module, since these also affect the resulting pydantic model.
2. the hash does not take into account the resulting name of the model (a ":leaf" submodel or an
   explicit name= shares its root schema's hash), so the index key is the hash plus that name.
3. the hash can only be calculated after a complete analysis of the given model.
"""


class PydanticModelCreator:
    #: (model, index key) -> the Pydantic model generated from the model. Dropped with the model: a
    #: re-registered class for the same table would otherwise get the old class's schema, whose
    #: ``orig_model`` still points at the unregistered class.
    MODEL_INDEX: ClassVar[Cache[type[PydanticModel]]] = Cache(MODEL_INDEX_MAX_SIZE, holds_sql=False)
    #: (model, its Pydantic model, list name) -> the list wrapper around the Pydantic model.
    QUERYSET_MODEL_INDEX: ClassVar[Cache[type[PydanticListModel]]] = Cache(
        QUERYSET_MODEL_INDEX_MAX_SIZE, holds_sql=False
    )

    @staticmethod
    def _collect_pydantic_meta(cls: type[Model]) -> PydanticMetaData:
        """Merges the ``PydanticMeta`` of every class in ``cls``'s MRO declaring its own - with several
        abstract bases, none may silently win by base order. List settings
        (include/exclude/computed) accumulate; scalar settings and ``model_config`` take the most
        derived class's value.
        """
        accumulated: PydanticMetaData | None = None
        for klass in reversed(cls.__mro__):
            meta = klass.__dict__.get("PydanticMeta")
            if meta is None:
                continue
            klass_meta = PydanticMetaData.from_pydantic_meta(meta)
            if accumulated is None:
                accumulated = klass_meta
            else:
                accumulated = dataclasses.replace(
                    klass_meta,
                    include=accumulated.include + klass_meta.include,
                    exclude=accumulated.exclude + klass_meta.exclude,
                    computed=accumulated.computed + klass_meta.computed,
                )
        return accumulated if accumulated is not None else PydanticMetaData()

    @staticmethod
    def _get_meta(
        cls: type[Model],
        meta_override: type | None,
        exclude: tuple[str, ...],
        include: tuple[str, ...],
        computed: tuple[str, ...],
        allow_cycles: bool | None,
        sort_alphabetically: bool | None,
        max_recursion: int | None,
        model_config: ConfigDict | None,
    ) -> PydanticMetaData:
        meta_from_class = PydanticModelCreator._collect_pydantic_meta(cls)
        if meta_override:
            meta_from_class = meta_from_class.construct_pydantic_meta(meta_override)
        return meta_from_class.finalize_meta(
            exclude=exclude,
            include=include,
            computed=computed,
            allow_cycles=allow_cycles,
            sort_alphabetically=sort_alphabetically,
            max_recursion=max_recursion,
            model_config=model_config,
        )

    def __init__(
        self,
        cls: type[Model],
        name: str | None = None,
        exclude: tuple[str, ...] | None = None,
        include: tuple[str, ...] | None = None,
        computed: tuple[str, ...] | None = None,
        optional: tuple[str, ...] | None = None,
        allow_cycles: bool | None = None,
        sort_alphabetically: bool | None = None,
        exclude_readonly: bool = False,
        meta_override: type | None = None,
        model_config: ConfigDict | None = None,
        validators: dict[str, Any] | None = None,
        module: str = __name__,
        exclude_sensitive: bool = False,
        relations_as_ids: bool = False,
        _stack: tuple[StackEntry, ...] = (),
        _as_submodel: bool = False,
        _max_recursion: int | None = None,
    ) -> None:
        self._cls: type[Model] = cls
        self._stack: tuple[StackEntry, ...] = _stack
        self._is_default: bool = (
            exclude is None
            and include is None
            and computed is None
            and optional is None
            and sort_alphabetically is None
            and allow_cycles is None
            and meta_override is None
            and not exclude_readonly
            and not exclude_sensitive
            and not relations_as_ids
            and model_config is None
            and validators is None
            and module == __name__
        )
        if exclude is None:
            exclude = ()
        self._exclude_sensitive: bool = exclude_sensitive
        if exclude_sensitive:
            exclude = tuple(exclude) + tuple(sorted(cls._meta.sensitive_fields))
        if include is None:
            include = ()
        if computed is None:
            computed = ()
        if optional is None:
            optional = ()
        self._relations_as_ids: bool = relations_as_ids
        if relations_as_ids:
            optional = self._add_relation_id_names(cls, optional)

        self.meta = self._get_meta(
            cls,
            meta_override=meta_override,
            exclude=exclude,
            include=include,
            computed=computed,
            allow_cycles=allow_cycles,
            sort_alphabetically=sort_alphabetically,
            max_recursion=_max_recursion,
            model_config=model_config,
        )

        self._exclude_read_only: bool = exclude_readonly

        self._fqname = cls.__module__ + "." + cls.__qualname__
        self._name: str
        self._title: str
        self.given_name = name
        self.__hash: str = ""

        self._as_submodel = _as_submodel

        self._annotations = ModelAnnotations.get(cls)

        self._pconfig: ConfigDict

        self._properties: dict[str, PropertyValue] = dict()
        self._relational_fields_index: list[tuple[str, str]] = list()

        self._model_description: ModelDescription = ModelDescription.from_model(cls)

        self._field_map: FieldMap = self._initialize_field_map()
        self._construct_field_map()

        self._optional = optional

        self._validators = validators
        self._module = module

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
                f"{self._cls._meta.schema};{self._cls._meta.db_table};"
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
            # Each flag is appended only when set, so every pre-existing schema name keeps its hash.
            if self._exclude_sensitive:
                hashval += ";exclude_sensitive"
            if self._relations_as_ids:
                hashval += ";relations_as_ids"
            self.__hash = b32encode(sha3_224(hashval.encode("utf-8")).digest()).decode("utf-8").lower()[:HASH_LENGTH]
        return self.__hash

    def get_name(self) -> tuple[str, str]:
        # Arguments other than the defaults append a hash to the class name, to make it unique.
        # Cycles are renamed explicitly.
        if self.given_name is not None:
            return self.given_name, self.given_name
        name = f"{self._fqname}:{self._hash}" if not self._is_default else self._fqname
        name = f"{name}:leaf" if self._as_submodel else name
        return name, self._cls.__name__

    def _initialize_pconfig(self) -> ConfigDict:
        pconfig: ConfigDict = PydanticModel.model_config.copy()
        if self.meta.model_config:
            pconfig.update(self.meta.model_config)
        if "title" not in pconfig:
            pconfig["title"] = self._title
        if "extra" not in pconfig:
            pconfig["extra"] = "forbid"
        # BinaryField values are arbitrary bytes - JSON carries them as base64 (the schema says
        # so too), not as UTF-8 text that non-UTF-8 bytes can't be encoded to.
        pconfig.setdefault("ser_json_bytes", "base64")
        pconfig.setdefault("val_json_bytes", "base64")
        return pconfig

    @staticmethod
    def _add_relation_id_names(cls: type[Model], names: tuple[str, ...]) -> tuple[str, ...]:
        """Adds the shadow id field names of every forward FK/O2O named in ``names``.

        Args:
            cls: The Hare model.
            names: Field names, possibly naming relations.

        Returns:
            ``names`` followed by the shadow id field names of the relations it names.
        """
        relation_id_names: list[str] = []
        for name in names:
            field = cls._meta.fields_map.get(name)
            if isinstance(field, ForeignKeyFieldInstance):
                relation_id_names.extend(field.source_fields)
        return tuple(names) + tuple(relation_id_names)

    def _get_caller_supplied_pk_fields(self) -> list[Field[Any]]:
        """The primary key fields a create payload carries. With ``exclude_readonly`` a component the
        database or ORM assigns is left out, and so is one a forward relation covers.

        Returns:
            The primary key fields.
        """
        pk_fields = self._model_description.pk_fields
        if not self._exclude_read_only:
            return pk_fields
        relation_source_field_names = {
            source_field_name
            for relation_field in (*self._model_description.fk_fields, *self._model_description.o2o_fields)
            for source_field_name in cast("ForeignKeyFieldInstance[Any]", relation_field).source_fields
        }
        return [
            field
            for field in pk_fields
            if not (
                field.generated
                or field.default is not None
                or field.has_db_default()
                or field.model_field_name in relation_source_field_names
            )
        ]

    def _initialize_field_map(self) -> FieldMap:
        return FieldMap(
            self.meta,
            pk_fields=self._get_caller_supplied_pk_fields(),
            model_fields_map=self._cls._meta.fields_map,
            relations_as_ids=self._relations_as_ids,
        )

    def _construct_field_map(self) -> None:
        self._field_map.field_map_update(fields=self._model_description.data_fields, meta=self.meta)
        # Forward relations are never assigned by the database - exclude_readonly keeps them;
        # whether each is required is decided per field.
        for fields in (
            self._model_description.fk_fields,
            self._model_description.o2o_fields,
            self._model_description.m2m_fields,
        ):
            self._field_map.field_map_update(fields, self.meta)
        if not self._exclude_read_only:
            # A reverse relation has no column on this row, so a flat (relations_as_ids) schema
            # leaves it out.
            if self._relations_as_ids:
                pass
            elif self.meta.backward_relations:
                for fields in (
                    self._model_description.backward_fk_fields,
                    self._model_description.backward_o2o_fields,
                ):
                    self._field_map.field_map_update(fields, self.meta)
            else:
                # Include only explicitly annotated backward relations
                for fields in (
                    self._model_description.backward_fk_fields,
                    self._model_description.backward_o2o_fields,
                ):
                    annotated = [f for f in fields if f.model_field_name in self._annotations]
                    if annotated:
                        self._field_map.field_map_update(annotated, self.meta)
            self._field_map.computed_field_map_update(self.meta.own_computed, self._cls, self.meta)
        if self.meta.sort_alphabetically:
            self._field_map.sort_alphabetically()
        else:
            self._field_map.sort_definition_order(self._cls, self.meta.own_computed)

    def create_pydantic_model(self) -> type[PydanticModel]:
        for field_name, field in self._field_map.items():
            # A non-primary-key generated column is assigned by the database, like the primary key.
            if self._exclude_read_only and isinstance(field, Field) and field.generated:
                continue
            self._process_field(field_name, field)

        self._name, self._title = self.get_name()

        # Keyed by hash AND name: a root schema and the ":leaf" submodel of the same model (or
        # two given name= values) can share one hash while being different schemas.
        model_index_key = (self._cls, f"{self._hash};{self._name}")
        if (hashed_model := PydanticModelCreator.MODEL_INDEX.get(model_index_key)) is not None:
            return cast("type[PydanticModel]", hashed_model)

        self._pconfig = self._initialize_pconfig()
        computed_fields: dict[str, Any] = {}
        common_fields: dict[str, Any] = {}
        for k, v in self._properties.items():
            if isinstance(getattr(v, "decorator_info", None), ComputedFieldInfo):
                computed_fields[k] = v
            else:
                common_fields[k] = v
        base_model = type(
            "BasePydanticModel",
            (PydanticModel,),
            {"model_config": self._pconfig, **computed_fields},
        )
        model: type[PydanticModel] = create_model(
            self._name,
            __base__=base_model,
            __module__=self._module,
            __validators__={**self._build_orm_field_validators(), **(self._validators or {})},
            **common_fields,
        )
        model.__doc__ = PydanticModelCreator._get_clean_docstring(self._cls)
        model.model_config["orig_model"] = self._cls  # type: ignore[typeddict-unknown-key]
        PydanticModelCreator.MODEL_INDEX[model_index_key] = model
        return model

    def _build_orm_field_validators(self) -> dict[str, Any]:
        """A pydantic field validator for every field with ORM ``validators=[...]``, so the schema
        enforces what ``Field.validate()`` does.

        Returns:
            ``create_model``'s ``__validators__``, keyed apart from the caller's own.
        """
        generated_validators: dict[str, Any] = {}
        for field_name, field in self._field_map.items():
            if isinstance(field, Field) and field.validators and field_name in self._properties:
                generated_validators[f"_hare_orm_validate_{field_name}"] = field_validator(field_name)(
                    self._make_orm_field_validator(field, accepts_none=field_name in self._optional)
                )
        return generated_validators

    @staticmethod
    def _make_orm_field_validator(field: Field[Any], accepts_none: bool) -> Callable[[type[PydanticModel], Any], Any]:
        """Builds the pydantic validator running ``field``'s ORM validators.

        Args:
            field: The ORM field.
            accepts_none: The field is listed in ``optional=`` - its schema allows null, meaning
                "not provided", which the ORM validators must not reject.
        """

        def validate(cls: type[PydanticModel], value: Any) -> Any:
            if value is None and accepts_none:
                return value
            if isinstance(field, CharEnumFieldInstance) and isinstance(value, Enum):
                # Stored (and length-checked) as the text of the member's value.
                value_to_validate: Any = str(value.value)
            else:
                value_to_validate = value
            try:
                field.validate(value_to_validate)
            except HareValidationError as exc:
                raise ValueError(str(exc)) from exc
            return value

        return validate

    def _process_field(
        self,
        field_name: str,
        field: Field[Any] | ComputedFieldDescription,
    ) -> None:
        if isinstance(field, Field):
            self._process_orm_field(field_name, field)
        elif isinstance(field, ComputedFieldDescription):
            self._process_computed_field_entry(field_name, field)

    def _process_orm_field(self, field_name: str, field: Field[Any]) -> None:
        json_schema_extra: dict[str, Any] = {}
        fconfig: dict[str, Any] = {
            "json_schema_extra": json_schema_extra,
        }
        field_property, _ = self._process_normal_field(field_name, field, json_schema_extra, fconfig)
        if field_property:
            fconfig["title"] = field_name.replace("_", " ").title()
            description = PydanticModelCreator._replace_newlines_with_br(field.docstring or field.description or "")
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
            is_tenant_field = self._is_tenant_field(field_name, field)
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
                self._properties[field_name] = (field_property, pydantic_field)
            else:
                if json_schema_extra.get("nullable") or (
                    self._exclude_read_only and json_schema_extra.get("readOnly")
                ):
                    # see: https://docs.pydantic.dev/latest/migration/#required-optional-and-nullable-fields
                    fconfig["default"] = None
                self._properties[field_name] = (field_property, PydanticField(**fconfig))

    def _process_computed_field_entry(self, field_name: str, field: ComputedFieldDescription) -> None:
        field_property = self._process_computed_field(field)
        if field_property:
            self._properties[field_name] = field_property

    def _process_normal_field(
        self,
        field_name: str,
        field: Field[Any],
        json_schema_extra: dict[str, Any],
        fconfig: dict[str, Any],
    ) -> tuple[Any, bool]:
        if isinstance(field, (ForeignKeyFieldInstance, OneToOneFieldInstance, BackwardOneToOneRelation)):
            return self._process_single_field_relation(field_name, field, json_schema_extra), True
        elif isinstance(field, (BackwardFKRelation, ManyToManyFieldInstance)):
            return self._process_many_field_relation(field_name, field), False
        elif isinstance(json_field := PydanticModelCreator._unwrap_generated_field(field), JSONField):
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
        return self._process_data_field(field_name, field, json_schema_extra, fconfig), False

    @staticmethod
    def _unwrap_generated_field(field: Field[Any]) -> Field[Any]:
        """Unwraps a ``GeneratedField`` to its ``output_field`` - a generated JSON or enum column is
        typed as one.
        """
        return field.output_field if isinstance(field, GeneratedField) else field

    def _is_tenant_field(self, field_name: str, field: Field[Any]) -> bool:
        """Whether ``field`` is ``Meta.tenant_field`` itself or the forward relation owning its
        column.

        Args:
            field_name: The field's name on the model.
            field: The field.
        """
        tenant_field = self._cls._meta.tenant_field
        if tenant_field is None:
            return False
        if field_name == tenant_field:
            return True
        return isinstance(field, (ForeignKeyFieldInstance, OneToOneFieldInstance)) and list(field.source_fields) == [
            tenant_field
        ]

    @staticmethod
    def _can_hide_target(
        field: ForeignKeyFieldInstance[Model] | OneToOneFieldInstance[Model] | BackwardOneToOneRelation[Model],
    ) -> bool:
        """Whether reading forward relation ``field`` can give ``None`` for a set key - its target
        model's default scope (soft delete, tenant, a custom ``Meta.manager`` filter) may hide the
        row.

        Args:
            field: The relation field.
        """
        if not isinstance(field, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
            return False
        related_model = field.related_model
        return bool(RowScopes.of(related_model))

    def _process_single_field_relation(
        self,
        field_name: str,
        field: ForeignKeyFieldInstance[Model] | OneToOneFieldInstance[Model] | BackwardOneToOneRelation[Model],
        json_schema_extra: dict[str, Any],
    ) -> type[PydanticModel] | None:
        python_type = field.get_python_type()
        model: type[PydanticModel] | None = self._get_submodel(python_type, field_name)
        if model:
            self._relational_fields_index.append((field_name, model.__name__))
            if field.null:
                json_schema_extra["nullable"] = True
            if (
                field.null
                or field.default is not None
                or field_name in self._optional
                or self._is_tenant_field(field_name, field)
                or PydanticModelCreator._can_hide_target(field)
            ):
                return cast("type[PydanticModel] | None", model | None)
            return model
        return None

    def _process_many_field_relation(
        self,
        field_name: str,
        field: BackwardFKRelation[Model] | ManyToManyFieldInstance[Model],
    ) -> type[list[type[PydanticModel]]] | None:
        python_type = field.related_model
        model = self._get_submodel(python_type, field_name)
        if model:
            self._relational_fields_index.append((field_name, model.__name__))
            return list[model]  # type: ignore[valid-type]
        return None

    def _process_data_field(
        self,
        field_name: str,
        field: Field[Any],
        json_schema_extra: dict[str, Any],
        fconfig: dict[str, Any],
    ) -> Any:
        annotation = self._annotations.get(field_name, None)
        constraints = copy(field.constraints)
        if "readOnly" in constraints:
            json_schema_extra["readOnly"] = constraints["readOnly"]
            del constraints["readOnly"]
        python_type = field.get_value_annotation()
        underlying_field = PydanticModelCreator._unwrap_generated_field(field)
        if isinstance(underlying_field, (IntEnumFieldInstance, CharEnumFieldInstance)):
            # The enum already pins the exact allowed values - the column's own numeric range /
            # length constraints next to its $ref are redundant and not valid for an enum schema.
            constraints = {}
        fconfig.update(constraints)
        ptype = python_type
        if field.null:
            json_schema_extra["nullable"] = True
        if field_name in self._optional or (field.null and not field.pk):
            ptype = ptype | None
        if not (self._exclude_read_only and json_schema_extra.get("readOnly") is True):
            return annotation or ptype
        return None

    def _process_computed_field(
        self,
        field: ComputedFieldDescription,
    ) -> Any:
        func: Callable[..., Any] | None
        if isinstance(field.function, property):
            func = field.function.fget
        elif isinstance(field.function, functools.cached_property):
            func = field.function.func
        else:
            func = field.function
        if func is None:
            return None
        annotation = ModelAnnotations.get(self._cls, func).get("return", None)
        if annotation is not None:
            original_func = func

            @functools.wraps(original_func)
            def wrapped_func(self_pydantic):
                # __orm_obj__ is set only when validated from an ORM instance; from a plain payload
                # the computed function runs on the pydantic instance.
                orm_obj = getattr(self_pydantic, "__orm_obj__", None)
                if orm_obj is not None:
                    try:
                        return original_func(orm_obj)
                    except NoValuesFetched:
                        raise NoValuesFetched(
                            f"Computed field '{original_func.__name__}' tried to access a "
                            f"relation that has not been fetched. Either include the relation "
                            f"in the Pydantic model so it is auto-prefetched, or call "
                            f"prefetch_related_objects() before serialization."
                        )
                return original_func(self_pydantic)

            comment = PydanticModelCreator._get_clean_docstring(func)
            c_f = computed_field(return_type=annotation, description=comment)
            return c_f(wrapped_func)
        return None

    @staticmethod
    def _create_submodel(
        cls: type[Model],
        *,
        stack: tuple[StackEntry, ...],
        exclude: tuple[str, ...] = (),
        include: tuple[str, ...] = (),
        computed: tuple[str, ...] = (),
        name: str | None = None,
        allow_cycles: bool = False,
        sort_alphabetically: bool | None = None,
        max_recursion: int,
        exclude_sensitive: bool = False,
        relations_as_ids: bool = False,
    ) -> type[PydanticModel] | None:
        """Create a Pydantic submodel with recursion protection against cyclic references."""
        if not allow_cycles and cls in (c[0] for c in stack[:-1]):
            return None

        # Level 0 is the root model itself, so the Nth stack entry (0-indexed) is level N -
        # reject once level reaches the configured max_recursion, so max_recursion=1 permits
        # exactly one level of nested submodels.
        for level, (_, _, parent_max_recursion) in enumerate(stack):
            if level >= parent_max_recursion:
                return None
        pmc = PydanticModelCreator(
            cls,
            exclude=exclude,
            include=include,
            computed=computed,
            name=name,
            _stack=stack,
            allow_cycles=allow_cycles,
            sort_alphabetically=sort_alphabetically,
            _max_recursion=max_recursion,
            _as_submodel=True,
            exclude_sensitive=exclude_sensitive,
            relations_as_ids=relations_as_ids,
        )
        return pmc.create_pydantic_model()

    def _get_submodel(self, _model: type[Model] | None, field_name: str) -> type[PydanticModel] | None:
        """Get Pydantic model for the submodel"""

        if _model:
            new_stack = self._stack + ((self._cls, field_name, self.meta.max_recursion),)

            prefix_len = len(field_name) + 1

            def get_fields_to_carry_on(field_tuple: tuple[str, ...]) -> tuple[str, ...]:
                return tuple(str(v[prefix_len:]) for v in field_tuple if v.startswith(field_name + "."))

            pmodel = self._create_submodel(
                _model,
                exclude=get_fields_to_carry_on(self.meta.exclude),
                include=get_fields_to_carry_on(self.meta.include),
                computed=get_fields_to_carry_on(self.meta.computed),
                stack=new_stack,
                allow_cycles=self.meta.allow_cycles,
                sort_alphabetically=self.meta.sort_alphabetically,
                max_recursion=self.meta.max_recursion,
                exclude_sensitive=self._exclude_sensitive,
                relations_as_ids=self._relations_as_ids,
            )
        else:
            pmodel = None

        if pmodel is None:
            self.meta.exclude += (field_name,)

        return pmodel

    @staticmethod
    def _replace_newlines_with_br(val: str) -> str:
        return val.replace("\n", "<br/>").strip()

    @staticmethod
    def _get_clean_docstring(obj: Any) -> str:
        return PydanticModelCreator._replace_newlines_with_br(inspect.cleandoc(obj.__doc__ or ""))


def pydantic_model_creator(
    cls: type[Model],
    *,
    name: str | None = None,
    exclude: tuple[str, ...] | None = None,
    include: tuple[str, ...] | None = None,
    computed: tuple[str, ...] | None = None,
    optional: tuple[str, ...] | None = None,
    allow_cycles: bool | None = None,
    sort_alphabetically: bool | None = None,
    exclude_readonly: bool = False,
    meta_override: type | None = None,
    model_config: ConfigDict | None = None,
    validators: dict[str, Any] | None = None,
    module: str = __name__,
    exclude_sensitive: bool = False,
    relations_as_ids: bool = False,
) -> type[PydanticModel]:
    """Builds a Pydantic model (https://docs.pydantic.dev/latest/concepts/models/) off a Hare
    model.

    Args:
        cls: The Hare Model.
        name: Specify a custom name explicitly, instead of a generated name.
        exclude: Extra fields to exclude from the provided model.
        include: Extra fields to include from the provided model.
        computed: Extra computed fields to include from the provided model.
        optional: Extra optional fields for the provided model.
        allow_cycles: Whether to allow cycles in the generated model - only useful for
            recursive/self-referential models. ``False`` (the default) prevents any backtracking.
        sort_alphabetically: Sort the parameters alphabetically instead of field-definition
            order. The default order is: field definition order, then order of reverse relations
            (as discovered), then order of computed functions (as provided).
        exclude_readonly: Build a subset model that excludes any readonly fields.
        meta_override: A PydanticMeta class to override the model's values.
        model_config: A custom config to use as pydantic config.
        validators: A dictionary of methods that validate fields.
        module: The name of the module that the model belongs to. Note: the generated pydantic
            model uses this parameter's config_class and PydanticMeta's own config_class as its
            Config class's bases (only if provided), but ignores a ``fields`` config -
            pydantic_model_creator generates fields from include/exclude/computed automatically.
        exclude_sensitive: Drop every ``sensitive=True`` field (``Model._meta.sensitive_fields``),
            nested models' included - e.g. for a public/export schema.
        relations_as_ids: Render every forward FK/O2O as its flat ``<name>_id`` field (the
            target's pk type, the relation's own nullability) instead of a nested submodel, and
            leave reverse relations out. M2M relations stay nested; nested models inherit the flag.
    """
    pmc = PydanticModelCreator(
        cls=cls,
        name=name,
        exclude=exclude,
        include=include,
        computed=computed,
        optional=optional,
        allow_cycles=allow_cycles,
        sort_alphabetically=sort_alphabetically,
        exclude_readonly=exclude_readonly,
        meta_override=meta_override,
        model_config=model_config,
        validators=validators,
        module=module,
        exclude_sensitive=exclude_sensitive,
        relations_as_ids=relations_as_ids,
    )
    return pmc.create_pydantic_model()


def pydantic_queryset_creator(
    cls: type[Model],
    *,
    name: str | None = None,
    exclude: tuple[str, ...] | None = None,
    include: tuple[str, ...] | None = None,
    computed: tuple[str, ...] | None = None,
    optional: tuple[str, ...] | None = None,
    allow_cycles: bool | None = None,
    sort_alphabetically: bool | None = None,
    exclude_readonly: bool = False,
    meta_override: type | None = None,
    model_config: ConfigDict | None = None,
    validators: dict[str, Any] | None = None,
    module: str = __name__,
    exclude_sensitive: bool = False,
    relations_as_ids: bool = False,
) -> type[PydanticListModel]:
    """Builds a Pydantic model (https://docs.pydantic.dev/latest/concepts/models/) list off a
    Hare model.

    Args:
        cls: The Hare Model to put in a list.
        name: A custom name for the list model, instead of ``<item model name>_list``. The item
            model keeps its own default name.
        exclude: Extra fields to exclude from the provided model.
        include: Extra fields to include from the provided model.
        computed: Extra computed fields to include from the provided model.
        optional: Extra optional fields of the item model.
        allow_cycles: Whether to allow cycles in the generated model - only useful for
            recursive/self-referential models. ``False`` (the default) prevents any backtracking.
        sort_alphabetically: Sort the parameters alphabetically instead of field-definition
            order. The default order is: field definition order, then order of reverse relations
            (as discovered), then order of computed functions (as provided).
        exclude_readonly: Build the item model without readonly fields, as in
            ``pydantic_model_creator``.
        meta_override: A PydanticMeta class to override the model's values.
        model_config: A custom pydantic config of the item model.
        validators: A dictionary of methods that validate fields of the item model.
        module: The name of the module that the item model belongs to.
        exclude_sensitive: Drop every ``sensitive=True`` field, nested models' included.
        relations_as_ids: Render every forward FK/O2O as its flat ``<name>_id`` field instead of a
            nested submodel, as in ``pydantic_model_creator``.
    """

    submodel = pydantic_model_creator(
        cls,
        exclude=exclude,
        include=include,
        computed=computed,
        optional=optional,
        allow_cycles=allow_cycles,
        sort_alphabetically=sort_alphabetically,
        exclude_readonly=exclude_readonly,
        meta_override=meta_override,
        model_config=model_config,
        validators=validators,
        module=module,
        exclude_sensitive=exclude_sensitive,
        relations_as_ids=relations_as_ids,
    )
    lname = name or f"{submodel.__name__}_list"

    queryset_index_key = (cls, submodel, lname)
    if (list_model := PydanticModelCreator.QUERYSET_MODEL_INDEX.get(queryset_index_key)) is not None:
        return cast("type[PydanticListModel]", list_model)

    model = create_model(
        lname,
        __base__=PydanticListModel,
        root=(list[submodel], PydanticField(default_factory=list)),  # type: ignore[valid-type]
    )
    model.__doc__ = PydanticModelCreator._get_clean_docstring(cls)
    model.model_config["title"] = name or f"{submodel.model_config['title']}_list"
    model.model_config["submodel"] = submodel  # type: ignore[typeddict-unknown-key]

    PydanticModelCreator.QUERYSET_MODEL_INDEX[queryset_index_key] = model
    return model
