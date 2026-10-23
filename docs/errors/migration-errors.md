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
    ├── TenantMigrationError
    ├── FieldNarrowingDataLossError
    ├── ForeignKeyTargetChangeError
    ├── InconsistentMigrationStateError(HareMigrationError, RuntimeError)
    └── MigrationAutodetectorError(HareMigrationError, RuntimeError)
```

## <a id="when-raised"></a>When each is raised

| Exception | When |
|---|---|
| `UnknownMigrationError` | An app label, a migration name or a `migrate` target names nothing that exists — or a migration name matches several migrations. Also a `LookupError`. |
| `MigrationLoadError` | A migration file can't be loaded: it doesn't import, has no `Migration` class, a migration is defined twice, a dependency names a migration or app that doesn't exist, or a squashed migration replaces migrations only some of which are applied while the files of the others are gone. |
| `CircularDependencyError` | The dependencies of the migrations form a cycle. |
| `IncompatibleStateError` | An operation can't run on the state the migrations before it leave. |
| `IrreversibleMigrationError` | A migration is unapplied while one of its operations can't be reversed — raised before the first migration of the run is unapplied, so the later ones stay applied. |
| `PartiallyAppliedMigrationError` | A non-atomic migration failed partway through — some of its operations already ran. |
| `TenantMigrationError` | With `tenant_schema_template`, the migrations failed in the schemas of some tenants. The other tenants' schemas are migrated; the error names each failed schema with its error, and the next `migrate` runs in each schema what its own journal lacks. |
| `FieldNarrowingDataLossError` | An `AlterField` narrowing a column would cut stored values. |
| `ForeignKeyTargetChangeError` | An `AlterField` points a relation at another target column (`to_field`) while rows store key values of the old one. |
| `InconsistentMigrationStateError` | Replaying the migration files leaves a relation pointing at a model that no longer exists. |
| `MigrationAutodetectorError` | `makemigrations` checks the migrations it made and finds them wrong — applied, they don't reach the models, their files don't read back as the same operations, or they depend on each other in a cycle. An error of hare's, raised before any file is written. Also a `RuntimeError`. |

## <a id="other-errors"></a>Other errors of migrations

A migration operation or a value that can't be declared or written into a migration file (a `lambda`
default, `RemoveIndex()` without a name or fields) raises `ConfigurationError`; `migrate` targets that
contradict each other raise `QueryError`.
