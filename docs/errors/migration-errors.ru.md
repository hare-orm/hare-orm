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
    ├── TenantMigrationError
    ├── FieldNarrowingDataLossError
    ├── ForeignKeyTargetChangeError
    ├── InconsistentMigrationStateError(HareMigrationError, RuntimeError)
    └── MigrationAutodetectorError(HareMigrationError, RuntimeError)
```

## <a id="when-raised"></a>Когда какое бросается

| Исключение | Когда |
|---|---|
| `UnknownMigrationError` | Метка приложения, имя миграции или цель `migrate` не называет ничего существующего — или имени миграции соответствует несколько миграций. Это ещё и `LookupError`. |
| `MigrationLoadError` | Файл миграции не загружается: не импортируется, в нём нет класса `Migration`, миграция определена дважды, зависимость называет несуществующую миграцию или приложение, или объединённая миграция заменяет миграции, из которых применены лишь некоторые, а файлов остальных уже нет. |
| `CircularDependencyError` | Зависимости миграций образуют цикл. |
| `IncompatibleStateError` | Операцию нельзя выполнить на состоянии, которое оставляют предыдущие миграции. |
| `IrreversibleMigrationError` | Миграцию откатывают, а одну из её операций обратить нельзя, — поднимается до отката первой миграции запуска, поэтому более поздние остаются применёнными. |
| `PartiallyAppliedMigrationError` | Неатомарная миграция упала посередине — часть её операций уже выполнилась. |
| `TenantMigrationError` | С `tenant_schema_template` миграции упали в схемах некоторых арендаторов. Схемы остальных арендаторов мигрированы; ошибка называет каждую упавшую схему с её ошибкой, а следующий `migrate` выполняет в каждой схеме то, чего нет в её собственном журнале. |
| `FieldNarrowingDataLossError` | `AlterField`, сужающий колонку, обрезал бы хранящиеся значения. |
| `ForeignKeyTargetChangeError` | `AlterField` переводит связь на другую целевую колонку (`to_field`), а строки хранят значения ключа по прежней. |
| `InconsistentMigrationStateError` | После прогона файлов миграций связь указывает на модель, которой больше нет. |
| `MigrationAutodetectorError` | `makemigrations` проверяет созданные миграции и находит их неверными: применённые, они не приводят к моделям, их файлы не читаются обратно теми же операциями или они зависят друг от друга по кругу. Ошибка самого hare, возникает до записи любого файла. Это ещё и `RuntimeError`. |

## <a id="other-errors"></a>Другие ошибки миграций

Операция миграции или значение, которое нельзя объявить или записать в файл миграции (`lambda` в
`default`, `RemoveIndex()` без имени и полей), дают `ConfigurationError`; противоречащие друг другу цели
`migrate` дают `QueryError`.
