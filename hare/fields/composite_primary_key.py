from __future__ import annotations

from hare.exceptions import ConfigurationError


class CompositePrimaryKey:
    """
    Declares a composite primary key from already-defined fields on the model - not a real field
    itself (has no DB column of its own), just a marker naming which already-declared fields
    together form the table's primary key.

    .. code-block:: python3

        class DocumentVersion(Model):
            id = fields.UUIDField()
            version = fields.IntField()
            pk = CompositePrimaryKey("id", "version")

    Modeled on ``django.db.models.CompositePrimaryKey`` (Django 5.2), but unlike Django, a
    foreign key pointing AT a model with a composite primary key IS supported: a
    ``ForeignKeyField``/``OneToOneField`` with no explicit ``to_field=`` defaults to the target's
    full composite primary key, generating one shadow column per component
    (``"<field>_<pk_component_name>"``); an explicit ``to_field=`` must name exactly the target's
    composite primary key components, in the same declared order - an arbitrary N-column
    ``UniqueConstraint`` target isn't supported. A ``OneToOneField`` also can't both target a
    composite primary key and be used as its own model's primary key.

    An existing table with a single-column primary key can't be migrated to a composite one -
    that needs a whole separate design pass this mirrors Django in not attempting.
    """

    def __init__(self, *field_names: str) -> None:
        if len(field_names) < 2:
            raise ConfigurationError("CompositePrimaryKey needs at least 2 field names")
        if len(set(field_names)) != len(field_names):
            raise ConfigurationError(f"CompositePrimaryKey field names must be unique, got {field_names}")
        self.field_names: tuple[str, ...] = field_names
