from __future__ import annotations

from dataclasses import dataclass, field, replace

from hare.migrations.operations.dependencies.constants import DATABASE_DEPENDENCY_SUBJECTS
from hare.migrations.operations.enums import DependencySubject, DependencyType


@dataclass(frozen=True)
class OperationDependency:
    """Something an operation does - created, removed or changed - or needs done before it runs.

    Args:
        subject: What is named - a model, a field, an object a model declares, its options, an
            extension, a schema, an ENUM type.
        app_label: The model's app; empty for an extension, a schema and an ENUM type.
        model_name: The model - compared case-insensitively; None for an extension, a schema and an ENUM
            type.
        name: The field's or the object's name; the extension's, schema's or type's; None for the model
            or its options.
        dependency_type: What is done to it.
    """

    subject: DependencySubject
    app_label: str
    model_name: str | None
    name: str | None
    dependency_type: DependencyType
    #: Native objects belong to a database namespace, independently of their declaring app.
    database_alias: str | None = field(default=None, kw_only=True)

    def for_database(self, alias: str | None) -> OperationDependency:
        """Bind native-object requirements to one database; model requirements retain their apps."""
        if self.subject not in DATABASE_DEPENDENCY_SUBJECTS or self.database_alias is not None or alias is None:
            return self
        return replace(self, database_alias=alias)

    def __post_init__(self) -> None:
        if self.model_name is not None:
            object.__setattr__(self, "model_name", self.model_name.lower())

    @classmethod
    def of_model(cls, app_label: str, model_name: str, dependency_type: DependencyType) -> OperationDependency:
        """A model created, removed or changed.

        Args:
            app_label: The model's app.
            model_name: The model.
            dependency_type: What is done to it.
        """
        return cls(DependencySubject.MODEL, app_label, model_name, None, dependency_type)

    @classmethod
    def of_field(
        cls, app_label: str, model_name: str, field_name: str, dependency_type: DependencyType
    ) -> OperationDependency:
        """A field of a model created, removed or changed.

        Args:
            app_label: The model's app.
            model_name: The model.
            field_name: The field.
            dependency_type: What is done to it.
        """
        return cls(DependencySubject.FIELD, app_label, model_name, field_name, dependency_type)

    @classmethod
    def of_schema_object(
        cls, app_label: str, model_name: str, name: str, dependency_type: DependencyType
    ) -> OperationDependency:
        """A named schema object declared by a model."""
        return cls(DependencySubject.SCHEMA_OBJECT, app_label, model_name, name, dependency_type)

    @classmethod
    def of_extension(cls, name: str, dependency_type: DependencyType) -> OperationDependency:
        """An extension created or removed.

        Args:
            name: The extension.
            dependency_type: What is done to it.
        """
        return cls(DependencySubject.EXTENSION, "", None, name, dependency_type)

    @classmethod
    def of_schema(cls, name: str, dependency_type: DependencyType) -> OperationDependency:
        """A schema created or dropped.

        Args:
            name: The schema.
            dependency_type: What is done to it.
        """
        return cls(DependencySubject.SCHEMA, "", None, name, dependency_type)

    @classmethod
    def of_enum_type(cls, schema: str | None, name: str, dependency_type: DependencyType) -> OperationDependency:
        """An ENUM type created, changed or dropped.

        Args:
            schema: The type's schema, None for the default one.
            name: The type.
            dependency_type: What is done to it.
        """
        return cls(
            DependencySubject.ENUM_TYPE, "", None, name if schema is None else f"{schema}.{name}", dependency_type
        )
