# Свои функции и выражения

## Своя функция {: #writing-a-custom-function }

Каждый класс из раздела [Функции базы данных](../querying/functions.ru.md) — подкласс `hare.query.expressions.function.Function` (или, для
агрегата, `Aggregate`; оба определены в `hare.query.expressions` и здесь просто повторно
экспортируются). Свою функцию пишите так же, если встроенных не хватает для нужной функции базы.
Функции вида `ИМЯ(аргументы...)` достаточно имени SQL в `function_name`; аргументы — поле (имя,
`F()` или другое выражение), а за ним значения, переданные после него:

```python
from hare.query.expressions import F
from hare.query.expressions.function import Function

class JsonSet(Function):
    function_name = "JSON_SET"
```

Функция со своей формой SQL (`EXTRACT(part FROM value)`, `x::type`) оставляет `function_name`
пустым и задаёт в `database_func` подкласс `hare.sql.terms.Function`, который строит эту форму. Если
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

## Своё выражение {: #writing-a-custom-expression }

Подкласс `Expression`, который не является `Function` (его собственный `get_result()` строит
SQL-терм), тоже говорит, как запрос с ним хранит [план](../querying/query-plan-cache.ru.md#plan-descriptions): либо он
описывает себя методом `get_plan_description(context)`, либо объявляет `plannable = False`. Класс,
который не делает ни того ни другого, отклоняется уже при определении (`TypeError`), поэтому новое
выражение никогда случайно не воспользуется планом, построенным для другого.

`get_plan_description()` возвращает `PlanDescription(structure, values)`: `structure` — всё, что
меняет текст SQL, который строит выражение (это часть ключа плана), а `values` — значения, которые
выражение подставляет параметрами, в том порядке, в каком их записывает его `get_result()`. Аргумент
описывайте его собственным описанием — `Expression.get_argument_plan_description(value, context)`
даёт описание выражения, а для литерала его тип и сам литерал как значение, — и возвращайте `None`,
когда экземпляр не хранит план:

```python
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.plans.description import PlanContext, PlanDescription


class Clamp(Expression):
    def __init__(self, field: str, low: int, high: int) -> None:
        self.field, self.low, self.high = field, low, high

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        ...  # LEAST(GREATEST(field, ?), ?) - границы подставляются параметрами

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        low = self.get_argument_plan_description(self.low, context)
        high = self.get_argument_plan_description(self.high, context)
        if low is None or high is None:
            return None
        return PlanDescription((Clamp, self.field, low.structure, high.structure), low.values + high.values)


class Unplanned(Expression):
    plannable = False  # запрос с ним каждый раз строится полностью

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult: ...
```

Выражение, текст SQL которого вообще не берёт значений (`PI()`, текущий момент), наследует
`hare.query.expressions.base.ConstantExpression` — он описывает его одним только классом.
