from __future__ import annotations

from typing import Any

from hare.exceptions import ConfigurationError
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.fields.data.numeric.int_field import IntField
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.models.enums import ModelOption


class PrimaryKeyDeclaration:
    """The primary key a model class declares: a CompositePrimaryKey, the field declared with
    primary_key=True, an id field added when there is neither, or none at all with Meta.primary_key
    = None - and the check that a database-generated field outside the key is of a class supporting
    generation."""

    @staticmethod
    def parse_composite_pk(attributes: dict[str, Any], name: str) -> tuple[dict[str, Any], tuple[str, ...]] | None:
        """Finds a ``pk = CompositePrimaryKey(...)`` declaration, validates it and takes the marker out
        of ``attrs`` - it isn't a field. None when there is none.
        """
        marker_key = None
        for key, value in attributes.items():
            if isinstance(value, CompositePrimaryKey):
                if marker_key is not None:
                    raise ConfigurationError(f"Can't create model {name} with two CompositePrimaryKey declarations")
                marker_key = key
        if marker_key is None:
            return None

        composite_pk = attributes[marker_key]
        component_names: list[str] = []
        for field_name in composite_pk.field_names:
            component_name = PrimaryKeyDeclaration.get_component_name(attributes, field_name)
            field = attributes.get(component_name)
            if not isinstance(field, Field):
                raise ConfigurationError(f"CompositePrimaryKey field '{field_name}' is not a field on model {name}")
            if component_names and component_names[-1] == component_name and field_name != component_name:
                # Another key column of the same relation.
                continue
            component_names.append(component_name)
            if field.pk:
                raise ConfigurationError(
                    f"CompositePrimaryKey field '{field_name}' on model {name} must not itself be "
                    "primary_key=True - the composite declaration is what makes it part of the PK"
                )
            if field.generated:
                raise ConfigurationError(
                    f"CompositePrimaryKey field '{field_name}' on model {name} can't be DB-generated "
                    "- every component of a composite key must be assigned explicitly"
                )
            if not field.has_db_field and not isinstance(field, ForeignKeyFieldInstance):
                raise ConfigurationError(
                    f"CompositePrimaryKey field '{field_name}' on model {name} is a many-to-many relation "
                    "- it has no column of the model's table"
                )
            if field.null:
                nullable_name = "a nullable relation" if isinstance(field, ForeignKeyFieldInstance) else "nullable"
                raise ConfigurationError(
                    f"CompositePrimaryKey field '{field_name}' on model {name} is {nullable_name} - every "
                    "component of a primary key holds a value"
                )
        # The composite declaration is the primary key - no field beside it is one.
        other_primary_keys = [
            key
            for key, value in attributes.items()
            if isinstance(value, Field) and value.pk and key not in component_names
        ]
        if other_primary_keys:
            raise ConfigurationError(
                f"Can't create model {name} with the primary key field {other_primary_keys[0]!r} and a "
                "CompositePrimaryKey - only one primary key is supported"
            )

        attributes = dict(attributes)
        del attributes[marker_key]
        return attributes, tuple(component_names)

    @staticmethod
    def get_component_name(attributes: dict[str, Any], field_name: str) -> str:
        """The field a composite primary key's component names - a foreign key's own name for one of
        its key columns (``author_id``, ``edition_number`` of a relation to a composite key), the
        longest relation name the column starts with; the relation's key columns take its place once
        it is linked."""
        if field_name in attributes:
            return field_name
        relation_names = [
            relation_name
            for relation_name, field in attributes.items()
            if isinstance(field, ForeignKeyFieldInstance) and field_name.startswith(f"{relation_name}_")
        ]
        return max(relation_names, key=len, default=field_name)

    @staticmethod
    def parse_custom_pk(
        attributes: dict[str, Any],
        primary_key_attribute: str | tuple[str, ...],
        name: str,
        is_abstract: bool,
        declares_no_primary_key: bool = False,
    ) -> tuple[dict[str, Any], str | tuple[str, ...]]:
        """Finds the model's primary key: a ``CompositePrimaryKey``, the field declared with
        ``primary_key=True``, an ``id`` added when there is neither - or none at all, with
        ``Meta.primary_key = None`` (the key is then ``()``).

        Args:
            attributes: The model class attributes.
            primary_key_attribute: The primary key found so far.
            name: The model class name.
            is_abstract: Whether the model is abstract - an abstract model gets no ``id``.
            declares_no_primary_key: Whether ``Meta.primary_key = None``.

        Returns:
            The attributes, with an added ``id`` or without the composite key marker, and the
            primary key's field name(s).

        Raises:
            ConfigurationError: Two primary keys, a primary key alongside
                ``Meta.primary_key = None``, a generated field that can't be, or an ``id`` field
                that isn't the primary key of a model declaring none.
        """
        if composite := PrimaryKeyDeclaration.parse_composite_pk(attributes, name):
            if declares_no_primary_key:
                raise ConfigurationError(
                    f"Model {name} declares both Meta.primary_key = None and a CompositePrimaryKey"
                )
            PrimaryKeyDeclaration.check_generated_non_pk_fields(composite[0], name)
            return composite
        PrimaryKeyDeclaration.check_generated_non_pk_fields(attributes, name)

        custom_pk_present = False
        for key, value in attributes.items():
            if isinstance(value, Field) and value.pk:
                if custom_pk_present:
                    raise ConfigurationError(
                        f"Can't create model {name} with two primary keys, only single primary key is supported"
                    )
                if value.generated and not value.allows_generated:
                    raise ConfigurationError(f"Field '{key}' ({value.__class__.__name__}) can't be DB-generated")
                custom_pk_present = True
                primary_key_attribute = key

        if declares_no_primary_key:
            if custom_pk_present:
                raise ConfigurationError(
                    f"Model {name} declares both Meta.primary_key = None and the primary key field "
                    f"{primary_key_attribute!r}"
                )
            return attributes, ()
        if not custom_pk_present and not is_abstract:
            if "id" not in attributes:
                attributes = {"id": IntField(primary_key=True), **attributes}

            if not isinstance(attributes["id"], Field) or not attributes["id"].pk:
                raise ConfigurationError(
                    f"Can't create model {name} without explicit primary key if field 'id' already present"
                )
        return attributes, primary_key_attribute

    @staticmethod
    def declares_no_primary_key(meta_class: type, name: str) -> bool:
        """Whether a model's ``Meta`` declares ``primary_key = None`` - a table with no primary key,
        such as an append-only log or a columnar store's table.

        Args:
            meta_class: The model's ``Meta``, with what abstract ancestors declared merged in.
            name: The model class name.

        Returns:
            True for ``primary_key = None``.

        Raises:
            ConfigurationError: ``Meta.primary_key`` is anything but None - a primary key is
                declared on its field (``primary_key=True``) or with ``CompositePrimaryKey``.
        """
        if not hasattr(meta_class, ModelOption.PRIMARY_KEY):
            return False
        if getattr(meta_class, ModelOption.PRIMARY_KEY) is not None:
            raise ConfigurationError(
                f"Model {name}: Meta.primary_key only takes None, for a table without a primary key - "
                "declare a primary key with primary_key=True on its field or with CompositePrimaryKey"
            )
        return True

    @staticmethod
    def check_generated_non_pk_fields(attributes: dict[str, Any], name: str) -> None:
        """Rejects a DB-generated non-primary-key field whose class has no generation support.

        Args:
            attributes: The model class attributes.
            name: The model class name.

        Raises:
            ConfigurationError: If such a field is found.
        """
        for key, value in attributes.items():
            if not isinstance(value, Field) or value.pk or not value.generated:
                continue
            if not value.allows_generated:
                raise ConfigurationError(
                    f"Field '{key}' ({value.__class__.__name__}) on model {name} can't be DB-generated"
                )
