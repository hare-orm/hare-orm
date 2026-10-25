# Модели, зарегистрированные во время работы

Класс модели, построенный во время работы приложения — например, по таблице, прочитанной из базы, —
регистрируется после `Hare.init()` и снимается с регистрации без перезапуска.

```python
from hare.inspectdb import DatabaseCatalog, ModelFactory

table_info = await DatabaseCatalog.inspect_table(connection, "loans")
Loan = ModelFactory.from_table_info(table_info, connection.dialect.name, app_label="live")
Hare.register_live_models([Loan], app_label="live", connection_alias="default", managed=False)
...
Hare.unregister_live_models([Loan])
```

## <a id="registering"></a>Регистрация: `register_live_models()`

```python
class Hare:
    @classmethod
    def register_live_models(
        cls,
        models: Iterable[type[Model]],
        app_label: str,
        connection_alias: str = "default",
        *,
        managed: bool | None = None,
    ) -> None: ...
```

Регистрирует подклассы `Model` **после** того, как `Hare.init()` уже выполнен, — для класса, который
появляется только во время работы (например, построен по схеме живой базы), а не объявлен заранее в
`apps` настроек. Принимает коллекцию классов моделей — одна модель передаётся списком из одного элемента.
Нужен активный `HareContext` (иначе `ConfigurationError`) и `connection_alias`, который действительно
настроен (иначе `ConfigurationError` с этим именем); `ConfigurationError` будет и если имя таблицы модели
совпадает с уже зарегистрированной. `app_label` может быть любой непустой строкой без точки — не
обязательно одним из настроенных приложений; пустая строка или строка с точкой (метка модели —
`<app>.<Model>`) даёт `ConfigurationError`.

Регистрация атомарна: если она не удалась (цель связи не найдена, имя обратной связи совпадает с полем
связанной модели и т. п.), модель не остаётся в реестре, ни одна связанная модель не сохраняет
добавленную ею обратную связь, а её `managed` и подключение восстанавливаются — исправьте класс и
зарегистрируйте его снова. Регистрация *другого* класса под именем, уже занятым в `app_label`, даёт
`ConfigurationError` — сначала вызовите `unregister_live_models()` для старого; повторная регистрация
того же класса ничего не делает.

Несколько моделей, зарегистрированных одним вызовом, — это один шаг; он нужен моделям, связи которых
указывают друг на друга (A → B и B → A): по одной их зарегистрировать нельзя (цели первой ещё нет).
Регистрация атомарна для всей группы: если не удалась хотя бы одна модель, не остаётся ни одной.

У живой модели почти всегда `managed=False` — иначе её попытаются вести и `makemigrations`/ `hare drift`,
споря с тем, кто на самом деле управляет её таблицей. `managed` записывается в
[`Meta.managed`](meta-options.ru.md#managed) *до* настройки связей; `None` (по умолчанию) оставляет
собственный `Meta.managed` модели.

## <a id="unregistering"></a>Снятие с регистрации: `unregister_live_models()`

```python
class Hare:
    @classmethod
    def unregister_live_models(cls, models: Iterable[type[Model] | str]) -> None: ...
```

Модель задаётся классом или меткой `"app.Model"` (`Hare.unregister_live_models(["content.Notice"])`).
Отменяет регистрацию: модель уходит из реестра, обратные связи (обратные ссылки внешних ключей и
«один-к-одному», созданные поля «многие-ко-многим»), которые она добавила моделям, на которые указывают
её связи, удаляются, и все кэши, построенные для неё или для этих моделей, сбрасываются. Колонки её
внешних ключей тоже сбрасываются, поэтому новый класс с тем же именем — или тот же класс — потом можно
зарегистрировать снова. Класс, регистрация которого отменена, не может выполнять запросы
(`ConfigurationError`).

Отмена регистрации модели, на которую ещё указывает другая зарегистрированная модель (внешний ключ,
«один-к-одному», «многие-ко-многим»), даёт `ConfigurationError` с именами ссылающихся полей и ничего не
меняет — сначала отмените регистрацию ссылающихся моделей или снимите их вместе: связи между моделями,
снимаемыми одним вызовом, не мешают, как и связь модели с самой собой.

## <a id="reading-a-table"></a>Чтение таблицы: `DatabaseCatalog`

```python
class DatabaseCatalog:
    @staticmethod
    async def inspect_table(
        connection: DatabaseClient, table: str, schema: str | None = None, *, verify_exists: bool = True
    ) -> TableInfo: ...

    @staticmethod
    async def inspect_tables(
        connection: DatabaseClient, tables: list[str], schema: str | None = None, *, verify_exists: bool = True
    ) -> list[TableInfo]: ...
```

Читает колонки, ключи, индексы и ограничения таблицы как `TableInfo`; `inspect_tables()` возвращает по
одному `TableInfo` на каждое имя в том же порядке — в PostgreSQL за постоянное число запросов к
системному каталогу, сколько бы таблиц ни было. `schema` при `None` — схема подключения по умолчанию;
база без схем её не учитывает. Несуществующая таблица даёт `TableNotFoundError`, а диалект без чтения
схемы — `UnsupportedDialectError`. `verify_exists=False` пропускает запрос списка таблиц — для
вызывающего, который только что сам сверил имена с таким списком.

## <a id="building-a-model-from-an-introspected-table"></a>Модель по прочитанной схеме таблицы

```python
class ModelFactory:
    @staticmethod
    def from_table_info(
        table_info: TableInfo,
        dialect: str,
        *,
        app_label: str,
        foreign_key_target_overrides: dict[str, str] | None = None,
        skip_many_to_many_through_tables: bool = True,
    ) -> type[Model]: ...
```

Строит класс модели для одной таблицы прямо через `type()` — это тот же класс, что получился бы при
выполнении кода, который создаёт для неё [`hare inspectdb`](../migrations/inspectdb.ru.md): оба пути
используют одно состояние модели (`hare.inspectdb.InspectedModelBuilder` превращает таблицу в тот же
`ModelState`, что есть у объявленной модели), поэтому поля, составные первичные ключи, индексы и
ограничения не могут разойтись. Класс не регистрируется — передайте его в `register_live_models()`.

- `app_label` — метка, которой уточняются цели внешних ключей, в том числе составных
  (`"<app_label>.<TargetClass>"`); обычно та же метка, под которой вы регистрируете модель. Модели
  целевых таблиц должны быть зарегистрированы раньше.
- `foreign_key_target_overrides` — `{target_table: "app.Model"}`, полностью заменяет ссылку для этой
  таблицы; нужно, чтобы направить внешний ключ на уже зарегистрированную модель, имя класса которой не
  следует правилам именования `inspectdb`.
- `skip_many_to_many_through_tables` — таблица только из колонок внешних ключей похожа на промежуточную
  таблицу `ManyToManyField`; по умолчанию она отклоняется с `ManyToManyThroughTableSkippedError` (там,
  где генератор кода вставил бы комментарий `# NOTE`). `False` строит для неё обычную модель.

Таблица без первичного ключа становится моделью с `Meta.primary_key = None` (см.
[Модели без первичного ключа](meta-options.ru.md#primary_key)).
