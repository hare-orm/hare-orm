from hare.migrations.operations.enums import DependencySubject

DATABASE_DEPENDENCY_SUBJECTS = frozenset(
    {DependencySubject.SCHEMA, DependencySubject.EXTENSION, DependencySubject.ENUM_TYPE}
)

DEPENDENCY_MEMBER_SUBJECTS = frozenset(
    {DependencySubject.FIELD, DependencySubject.SCHEMA_OBJECT, DependencySubject.OPTIONS}
)
