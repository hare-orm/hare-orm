# Схема на арендатора

При схеме на арендатора (PostgreSQL) таблицы части моделей у каждого арендатора лежат в его
собственной схеме базы — `tenant_acme.invoice`, `tenant_7.invoice`, — а остальные модели держат одну
таблицу на всех арендаторов в собственной схеме подключения (`public`). Запрос не несёт фильтра по
арендатору: схему выбирает активная [`Tenancy.scope(tenant)`](multi-tenancy.ru.md#tenancy-scope), а
подключение попадает в неё через путь поиска схем PostgreSQL (`search_path`). Это другой способ, чем
[`Meta.tenant_field`](multi-tenancy.ru.md) (колонка арендатора в общей таблице): строки арендаторов
разделены на уровне базы, данные арендатора удаляются одним `DROP SCHEMA`, а каждый индекс и каждое
ограничение — свои у каждого арендатора.

## <a id="setup"></a>Настройка

Подключение называет схемы арендаторов шаблоном `tenant_schema_template` — строчные латинские буквы,
цифры и `_` вокруг одного `{tenant}`:

```python
config = {
    "connections": {
        "default": {
            "engine": "postgresql",
            "credentials": {
                "host": "localhost", "database": "app", "user": "app", "password": "...",
                "tenant_schema_template": "tenant_{tenant}",
            },
        },
    },
    "apps": {"billing": {"models": ["billing.models"], "default_connection": "default"}},
}
```

В адресе подключения это параметр с закодированными скобками:
`postgresql://app@localhost/app?tenant_schema_template=tenant_%7Btenant%7D`. Любой другой шаблон (без
подстановки, с двумя подстановками, с заглавными буквами, с `-`) даёт `ConfigurationError` при
настройке подключения. В SQLite и ClickHouse схем на арендатора нет — `Features.supports_tenant_schemas` равен
False, и `TenantSchemas` даёт `UnSupportedError` до отправки SQL.

Модель, таблица которой лежит в схеме каждого арендатора, объявляет `Meta.tenant_schema = True`:

```python
class Customer(Model):          # общая — одна таблица в "public"
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=100)


class Invoice(Model):           # своя таблица в схеме каждого арендатора
    id = fields.IntField(primary_key=True)
    customer = fields.ForeignKeyField("billing.Customer", related_name="invoices")
    total = fields.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        tenant_schema = True
```

- `Meta.tenant_schema` — `True` или `False`, любое другое значение даёт `ConfigurationError`.
- Его нельзя задать вместе с `Meta.schema` (`ConfigurationError`): схема таблицы — схема активного
  арендатора.
- Модель арендатора может ссылаться на общую модель (`Invoice.customer` выше): её внешний ключ в
  схеме каждого арендатора ссылается на общую таблицу. У общей модели не может быть
  `ForeignKeyField`, `OneToOneField` или `ManyToManyField` на модель арендатора — на таблицу какого
  арендатора она бы ссылалась? — `ConfigurationError` называет поле; объявите связь на модели
  арендатора.
- Модель с `Meta.tenant_schema` на подключении без `tenant_schema_template` даёт
  `ConfigurationError` на первом запросе и при создании её таблиц.

## <a id="tenant-values"></a>Значения арендатора

Схема арендатора — шаблон, в котором `{tenant}` заменён значением арендатора: `int`
(`7` → `tenant_7`) или строкой из строчных латинских букв, цифр и `_` (`"acme"` → `tenant_acme`).
Любое другое значение — `"Acme"`, `"acme-1"`, `""`, `UUID` (передавайте `uuid.hex`), `float` —
даёт `ValidationError`, как и имя схемы длиннее 63 байт PostgreSQL. Значение не приводится к другому
виду: `"Acme"` отклоняется, а не превращается в схему другого арендатора. `7` и `"7"` — одна схема.

## <a id="tenant-schemas"></a>Создание, удаление и список арендаторов

`hare.models.tenancy.tenant_schemas.TenantSchemas`:

```python
from hare.models.tenancy.tenant_schemas import TenantSchemas

await TenantSchemas.create("acme")                      # CREATE SCHEMA tenant_acme + таблицы всех моделей с tenant_schema
await TenantSchemas.create(7, create_tables=False)       # только схема — таблицы создаст `migrate`
await TenantSchemas.get_tenants()                       # ["7", "acme"] — схемы, имена которых даёт шаблон, текстом
await TenantSchemas.drop("acme")                        # DROP SCHEMA tenant_acme CASCADE — все таблицы и строки арендатора
```

Каждый метод принимает `using=` — имя подключения, необязательное при одном подключении; `create()`
возвращает имя схемы. Они работают
вне области арендатора и вне транзакции — внутри дают `QueryError` (схема, созданная в транзакции,
ещё не видна собственному подключению арендатора). `create()` с `create_tables=True` (по умолчанию)
создаёт таблицы по текущим моделям, как `generate_schemas()`, — для проекта без миграций и для
тестов. Проект с миграциями создаёт схему с `create_tables=False` и запускает `migrate`, который
доводит до актуального состояния схему каждого арендатора ([Миграции](#migrations)). `get_tenants()`
перечисляет схемы, имя которых даёт шаблон, поэтому схема `tenant_beta`, созданная вручную,
считается арендатором `"beta"`.

## <a id="queries"></a>Запросы

Внутри `Tenancy.scope(tenant)` одного арендатора каждый запрос подключения выполняется клиентом схемы
этого арендатора: у его соединений путь поиска `tenant_<арендатор>, public` (вместо `public` —
собственная настройка `schema` подключения, если она задана). Модель арендатора читает и пишет
таблицу своего арендатора; общая модель находится в `public` через тот же путь поиска — соединения
таблиц двух видов (`select_related("customer")`) обычные.

```python
with Tenancy.scope("acme"):
    await Invoice.objects.create(id=1, customer=customer, total=10)
    await Invoice.objects.select_related("customer").all()      # tenant_acme.invoice JOIN public.customer
with Tenancy.scope(7):
    await Invoice.objects.count()                                # tenant_7.invoice — 0
```

- Модель арендатора без области, в области нескольких арендаторов (`Tenancy.any_of(...)`), в
  `Tenancy.ALL` или в области, заданной по моделям, даёт `QueryError` — её таблица доступна только
  в области одного арендатора. Общая модель работает в любой из них через собственного клиента
  подключения.
- QuerySet, построенный внутри области, выполняется в схеме этого арендатора, даже если его
  дождались после конца области: подключение выбирается для арендатора, для которого построен
  запрос, как и у [маршрутизатора](multi-tenancy.ru.md#routers).
- Клиент схемы каждого арендатора — отдельный пул соединений, открывается при первом запросе и
  закрывается вместе с подключением (`Hare.close_connections()`). Внутри области его даёт
  `Connections.get(connection_alias)`; `ConnectionHandler.get_own(connection_alias)` всегда даёт само подключение.
- При `tenant_schema_template` значение `Tenancy.scope()` должно быть допустимым значением
  арендатора этого подключения даже для запроса к общей модели — область `"Acme"` даёт
  `ValidationError` на первом запросе подключения.
- `Meta.tenant_field` работает рядом: модель арендатора может объявить и колонку арендатора — она
  фильтруется внутри её схемы как обычно.

## <a id="transactions"></a>Транзакции

Транзакция работает в схеме арендатора, активного при её начале: все её запросы идут через одно
соединение. Внутри неё запрос в области другого арендатора или без арендатора, если она началась в
схеме арендатора (и наоборот), даёт `QueryError`, а не выполняется молча в схеме транзакции:

```python
with Tenancy.scope("acme"):
    async with Transactions.atomic():
        await Invoice.objects.create(...)              # tenant_acme
        with Tenancy.scope("beta"):
            await Invoice.objects.count()              # QueryError — транзакция работает в tenant_acme
```

`Transactions.autonomous()` внутри области открывает своё соединение в схеме того же арендатора.

## <a id="migrations"></a>Миграции

`migrate` на подключении с `tenant_schema_template` работает в два шага:

1. **Общая схема** — с журналом `public.hare_migrations`. Операции над таблицами общих моделей
   выполняются, над таблицами моделей с `Meta.tenant_schema` — пропускаются.
2. **Схема каждого арендатора** (схемы из `TenantSchemas.get_tenants()`), по порядку имён схем —
   каждая со своим журналом `tenant_<арендатор>.hare_migrations`, внутри `Tenancy.scope(tenant)`
   (значение арендатора текстом). Операции над таблицами моделей арендатора выполняются, над
   таблицами общих моделей — пропускаются.

Поэтому арендатор, добавленный позже (`create(..., create_tables=False)`), получает все миграции при
следующем `migrate`, а остальные — только недостающие. Состояние миграций одно для всех схем —
`CreateModel(..., options={"tenant_schema": True})` записывает опцию.

- Операция над таблицей модели (`CreateModel`, `AddField`, `AddIndex`, представление или политика,
  объявленные на модели, ...) следует `Meta.tenant_schema` модели.
- Операция, не привязанная к модели, — `RunSQL`, `RunPython`, `CreateExtension`, `CreateSchema`,
  тип-перечисление — выполняется в общей схеме. `RunSQL(..., tenant_schema=True)` и
  `RunPython(..., tenant_schema=True)` вместо этого выполняются в схеме каждого арендатора; функция
  такого `RunPython` вызывается по разу на арендатора, в его области, поэтому её запросы к моделям
  арендатора попадают в его таблицу.
- Внутри `Tenancy.scope(tenant)` `migrate` доводит до цели только схему этого арендатора — общая схема
  и остальные арендаторы не меняются.
- `migrate` до `app.zero` откатывает и общую схему, и схему каждого арендатора.
- `sqlmigrate` показывает SQL всех операций — и общих, и арендаторских — как для одной схемы.
- Смену `Meta.tenant_schema` у существующей модели `makemigrations` отклоняет
  (`ConfigurationError`): её строки переехали бы между общей схемой и схемами всех арендаторов.
  Напишите миграцию вручную — новая модель, `RunPython`, копирующий строки, затем удаление старой.

## <a id="choosing"></a>Столбец арендатора или схема на арендатора

| | `Meta.tenant_field` | `Meta.tenant_schema` |
|---|---|---|
| Где строки арендатора | одна общая таблица, колонка арендатора | таблица в схеме арендатора |
| Разделение | фильтр ORM | база: таблицы другого арендатора нет на пути поиска |
| Запросы по нескольким арендаторам | `Tenancy.any_of(...)`, `Tenancy.ALL` | не через ORM — одна область, один арендатор |
| Удаление арендатора | `DELETE` по колонке арендатора | `TenantSchemas.drop(tenant)` |
| Много арендаторов | одна таблица на всех | свой набор таблиц и индексов и свой пул соединений у каждого арендатора |
| Базы данных | все диалекты | PostgreSQL |
