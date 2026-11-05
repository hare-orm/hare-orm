from hare.migrations.operations.enums import DependencySubject

DEPENDENCY_MEMBER_SUBJECTS = frozenset(
    {DependencySubject.FIELD, DependencySubject.SCHEMA_OBJECT, DependencySubject.OPTIONS}
)
