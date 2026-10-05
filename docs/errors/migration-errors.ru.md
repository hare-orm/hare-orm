# Ошибки миграций

Миграции бросают собственные подклассы `HareError` из `hare.migrations.exceptions`:

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

| Исключение | Когда |
|---|---|
| `UnknownMigrationError` | Метка приложения, имя миграции или цель `migrate` не называет ничего существующего — или имени миграции соответствует несколько миграций. Это ещё и `LookupError`. |
| `MigrationLoadError` | Файл миграции не загружается: не импортируется, в нём нет класса `Migration`, миграция определена дважды зависимость называет несуществующую миграцию или приложение, или объединённая миграция заменяет миграции, из которых применены лишь некоторые, а файлов остальных уже нет. |
| `CircularDependencyError` | Зависимости миграций образуют цикл. |
| `IncompatibleStateError` | Операцию нельзя выполнить на состоянии, которое оставляют предыдущие миграции. |
| `IrreversibleMigrationError` | Миграцию откатывают, а одну из её операций обратить нельзя. |
| `PartiallyAppliedMigrationError` | Неатомарная миграция упала посередине — часть её операций уже выполнилась. |
| `FieldNarrowingDataLossError` | `AlterField`, сужающий колонку, обрезал бы хранящиеся значения. |
| `ForeignKeyTargetChangeError` | `AlterField` переводит связь на другую целевую колонку (`to_field`), а строки хранят значения ключа по прежней. |
| `InconsistentMigrationStateError` | После прогона файлов миграций связь указывает на модель, которой больше нет. |

Операция миграции или значение, которое нельзя объявить или записать в файл миграции (`lambda` в
`default`, `RemoveIndex()` без имени и полей), дают `ConfigurationError`; противоречащие друг другу
цели `migrate` дают `QueryError`.
