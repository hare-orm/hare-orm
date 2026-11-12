from hare.inspectdb.enums import CatalogDiagnosticCode

CATALOG_DIAGNOSTIC_COMMENTS = {
    CatalogDiagnosticCode.GENERATED_COLUMN: (
        "# TODO: column {name!r} is a generated (GENERATED ALWAYS AS (...)) column, but "
        "its expression text wasn't in a shape this could parse back out of the table's raw SQL "
        "({definition!r}) - it's been dropped entirely; add it back by hand as a GeneratedField."
    ),
    CatalogDiagnosticCode.FOREIGN_KEY: (
        "# TODO: composite foreign key {name!r} ({columns}) wasn't reconstructed as "
        "a ForeignKeyField - either it doesn't reference the target's whole primary key, or its "
        'columns aren\'t named "<field>_<target key column>", the only names a composite relation '
        "declares. The member columns are plain fields above; to declare the relation, rename the "
        "columns so (a RunSQL migration) and replace them with the ForeignKeyField."
    ),
    CatalogDiagnosticCode.INDEX: (
        "# TODO: index {name!r} wasn't reconstructed ({definition!r}) - an expression term carries "
        "something Index(*expressions)/its subclasses can't represent (an operator class, sort order "
        "or collation next to an expression, or a definition that couldn't be parsed). Add it back by hand."
    ),
    CatalogDiagnosticCode.EXCLUSION_CONSTRAINT: (
        "# TODO: exclusion constraint {name!r} wasn't in a shape ExclusionConstraint(...) "
        "could reconstruct ({definition!r}) - add it back by hand."
    ),
    CatalogDiagnosticCode.CHECK_CONSTRAINT: (
        "# TODO: CHECK constraint {name!r} wasn't in a shape CheckConstraint(...) could "
        "reconstruct ({definition!r}) - add it back by hand."
    ),
    CatalogDiagnosticCode.TRIGGER: (
        "# TODO: trigger {name!r} wasn't in a shape Trigger(...) could reconstruct "
        "({definition!r}) - add it back by hand."
    ),
    CatalogDiagnosticCode.PARTITIONING: (
        "# TODO: the table is partitioned as {definition!r}, which no partitioning of "
        "hare declares - a migration of the model creates it unpartitioned; partition it by hand."
    ),
    CatalogDiagnosticCode.GRANT: "# TODO: {definition} - grant it with a RunSQL migration",
}
