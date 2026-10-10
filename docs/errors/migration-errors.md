# Migration errors

The migrations raise their own `HareError` subclasses, from `hare.migrations.exceptions`:

```text
HareError
└── HareMigrationError
    ├── UnknownMigrationError(HareMigrationError, LookupError)
    ├── MigrationLoadError
    ├── CircularDependencyError
    ├── IncompatibleStateError
    ├── IrreversibleMigrationError
    ├── PartiallyAppliedMigrationError
    ├── FieldNarrowingDataLossError
    ├── ForeignKeyTargetChangeError
    └── InconsistentMigrationStateError(HareMigrationError, RuntimeError)
```

| Exception | When |
|---|---|
| `UnknownMigrationError` | An app label, a migration name or a `migrate` target names nothing that exists — or a migration name matches several migrations. Also a `LookupError`. |
| `MigrationLoadError` | A migration file can't be loaded: it doesn't import, has no `Migration` class, a migration is defined twice, a dependency names a migration or app that doesn't exist, or a squashed migration replaces migrations only some of which are applied while the files of the others are gone. |
| `CircularDependencyError` | The dependencies of the migrations form a cycle. |
| `IncompatibleStateError` | An operation can't run on the state the migrations before it leave. |
| `IrreversibleMigrationError` | A migration is unapplied while one of its operations can't be reversed. |
| `PartiallyAppliedMigrationError` | A non-atomic migration failed partway through — some of its operations already ran. |
| `FieldNarrowingDataLossError` | An `AlterField` narrowing a column would cut stored values. |
| `ForeignKeyTargetChangeError` | An `AlterField` points a relation at another target column (`to_field`) while rows store key values of the old one. |
| `InconsistentMigrationStateError` | Replaying the migration files leaves a relation pointing at a model that no longer exists. |

A migration operation or a value that can't be declared or written into a migration file (a
`lambda` default, `RemoveIndex()` without a name or fields) raises `ConfigurationError`; `migrate`
targets that contradict each other raise `QueryError`.
