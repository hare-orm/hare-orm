# Свои функции и выражения

Проект добавляет SQL-функцию, которой нет в hare, подклассом `Function`, а всё остальное, что
может вычислять запрос, — подклассом `Expression`; затем оба работают в `annotate()`, `filter()`,
`order_by()`, агрегатах и `update()`, как встроенные.

## <a id="writing-a-custom-function"></a>Своя функция

Каждый класс из раздела [Функции базы данных](../querying/functions.ru.md) — подкласс `Function` (или, для
агрегата, `Aggregate`), оба из `hare.query.expressions`. Свою функцию пишите так же, если встроенных не хватает для нужной функции базы.
Функции вида `ИМЯ(аргументы...)` достаточно имени SQL в `function_name`; аргументы — поле (имя,
`F()` или другое выражение), а за ним значения, переданные после него:

```python
from hare.query.expressions import F, Function

class JsonSet(Function):
    function_name = "JSON_SET"
```

Функция со своей формой SQL (`EXTRACT(part FROM value)`, `x::type`) оставляет `function_name`
пустым и задаёт в `database_function` подкласс `hare.sql.terms.Function`, который строит эту форму. Если
какая-то база пишет функцию иначе, её диалект регистрирует отрисовщик для этого имени (см.
[Функции и операторы фильтра](writing-a-dialect.ru.md#functions-and-lookups)).

Работает и как значение атрибута, присвоенное перед `.save()`, и прямо в `.update()` — в обоих
случаях это настоящее выражение на стороне SQL, а не значение, вычисленное в Python:

```python
obj.data = JsonSet(F("data"), "$.a", 2)
await obj.save()

await Model.objects.filter(pk=obj.pk).update(data=JsonSet(F("data"), "$.a", 3))
```

Подклассу `Function` для [планов запросов](../querying/query-plan-cache.ru.md) больше ничего не нужно: он наследует то, как
функция описывает свой план, — её класс, поле, тип каждого литерального аргумента (сами значения
подставляются в каждом запросе) и `get_plan_options()`. Переопределите `get_plan_options()`, когда
параметр функции записывается в текст SQL, а не подставляется как значение, — тогда два вызова с
разными параметрами никогда не делят один план:

```python
class RoundTo(Function):
    def __init__(self, field: str, precision: int = 0) -> None:
        super().__init__(field)
        self.precision = precision

    def get_plan_options(self) -> tuple[Any, ...]:
        return (self.precision,)
```

## <a id="writing-a-custom-expression"></a>Своё выражение

Подкласс `Expression`, который не является `Function` (его собственный `get_result()` строит
SQL-терм), тоже говорит, как запрос с ним хранит [план](../querying/query-plan-cache.ru.md#plan-descriptions): он
объявляет в `plan_parts`, как каждый его атрибут входит в план, и описание генерируется из этого
объявления; либо он объявляет `plannable = False`. Класс, который не делает ни того ни другого,
отклоняется уже при определении (`TypeError`), поэтому новое выражение никогда случайно не
воспользуется планом, построенным для другого.

Каждая часть — атрибут (или метод, для типов `*_METHOD`) и его `PlanPartType`, в порядке ключа:

| Тип части | Атрибут — это |
|---|---|
| `KEY` | то, что пишется в текст SQL как есть: оператор, имя поля или вычисляемого значения, параметр |
| `KEYS` | последовательность, которая пишется в текст SQL: имена полей, сортировки |
| `KEY_METHOD` | метод, возвращающий то, что пишется в текст SQL |
| `LITERAL` | литерал, подставляемый параметром (как его подставляет `Value`) |
| `ARGUMENT` / `ARGUMENTS` | аргумент (их последовательность): выражение — своим описанием, `None` — как `NULL`, терм `hare.sql` — текстом и значениями, любой другой литерал — типом, а сам он подставляется; литерал-список или кортеж плана не хранит |
| `ENCODED_ARGUMENT` | аргумент, литерал которого построение кодирует в одно подставляемое значение (точка — её текстом, вектор): список или кортеж подставляется целиком |
| `ARGUMENT_METHOD` | метод, дающий аргумент, описанный как `ARGUMENT` |
| `VALUE_METHOD` | метод, дающий значение, подставляемое параметром |
| `PARAMETERS` | параметры `ValueWrapper`, которые объект сделал при создании (у `RawSQL`) |
| `FIELD` / `FIELDS` | то, что читает функция (их последовательность): выражение, имя поля или вычисляемого значения, терм `hare.sql` |
| `EXPRESSION` / `EXPRESSIONS` | выражение или условие (их последовательность) своим описанием, `None` — отсутствие |
| `EXPRESSION_METHOD` | метод, дающий выражение или условие |
| `JOIN_CONDITION_METHOD` | метод, дающий условие, которое построение вплетает в `JOIN`: его значения подставляются в каждый `JOIN`, куда оно вплетено, и никуда, если таких нет |
| `FILTERS` | именованные фильтры условия |
| `QUERY` / `QUERY_METHOD` | запрос, встроенный в выражение (метод, дающий его) |
| `PLAN_KEY` | объект, который сам даёт свой ключ плана (`get_plan_key()`) |
| `KEEPS_PLAN_METHOD` | метод, говорящий, хранит ли объект план вообще: `False` — не хранит |
| `NONE` | описан через другую часть (метод, который его читает) или вовсе не часть текста SQL: кэш, служебное |

Каждый атрибут класса классифицирован: слот класса со `__slots__`, не названный ни одной частью,
отклоняется при определении класса, а запуск pytest с `--verify-plans` проверяет атрибуты каждого
объекта, который описывает. Базовый класс перечисляет служебные атрибуты всех своих объектов в
`unplanned_attributes` (у `Expression` — запись аргументов его конструктора).

Построение разрешает каждый аргумент так, как его описывает часть, — `ExpressionArguments.get_result(self,
attribute, value, expression_context)` для `ARGUMENT` (`binds_whole=True` для `ENCODED_ARGUMENT`,
`encoder=` сначала переводит литерал в подставляемую форму), `ExpressionArguments.get_field_result(...)`
для `FIELD`: выражение разрешается в свой результат, терм SQL записывает свои значения, а литерал
подставляется параметром и, пока запрос записывает свой план, записывается под атрибутом, где лежит.
План подставляет значение во все ссылки, записанные под ним, сколько бы раз построение его ни
разрешало.

```python
from typing import ClassVar

from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType


class Clamp(Expression):
    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("field", PlanPartType.FIELD),
        ("low", PlanPartType.ARGUMENT),
        ("high", PlanPartType.ARGUMENT),
    )

    def __init__(self, field: str, low: int, high: int) -> None:
        self.field, self.low, self.high = field, low, high

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        field = ExpressionArguments.get_field_result(self, "field", self.field, expression_context)
        low = ExpressionArguments.get_result(self, "low", self.low, expression_context)
        high = ExpressionArguments.get_result(self, "high", self.high, expression_context)
        ...  # LEAST(GREATEST(field.term, low.term), high.term)


class Unplanned(Expression):
    plannable = False  # запрос с ним каждый раз строится полностью

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult: ...
```

Класс, описание которого не покрывает ни один тип части, пишет `get_plan_description(context)` сам и
возвращает `PlanDescription(structure, values, origins)`, `None` — когда экземпляр не хранит план;
пока запрос записывает свой план (`PlanOrigins.records`), `origins` перечисляет, откуда взято каждое
значение, — `PlanOrigins.get_value_origin(self, attribute)`, — а иначе это `None`.

Выражение, текст SQL которого вообще не берёт значений (`PI()`, текущий момент), наследует
`hare.query.expressions.ConstantExpression` — он описывает его одним только классом.
