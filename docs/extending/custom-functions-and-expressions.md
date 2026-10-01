# Custom functions and expressions

## Writing a custom function {: #writing-a-custom-function }

Every class of [Database functions](../querying/functions.md) is a subclass of `hare.query.expressions.function.Function` (or, for an aggregate,
`Aggregate`, both really defined in `hare.query.expressions` and re-exported here) — write your own
the same way when the built-in set doesn't cover a database function you need. A function written
as `NAME(arguments...)` only needs its SQL name in `function_name`; the arguments are the field (a
name, `F()` or another expression) followed by the values given after it:

```python
from hare.query.expressions import F
from hare.query.expressions.function import Function

class JsonSet(Function):
    function_name = "JSON_SET"
```

A function with a SQL shape of its own (`EXTRACT(part FROM value)`, `x::type`) leaves
`function_name` unset and sets `database_func` to a `hare.sql.terms.Function` subclass building that
shape. Where one database writes the function differently, its dialect registers a renderer for the
name (see [Functions and lookups](writing-a-dialect.md#functions-and-lookups)).

Works both as an assigned attribute value before `.save()` and directly in `.update()` — either way
it's a real SQL-side expression, not a Python-computed value:

```python
obj.data = JsonSet(F("data"), "$.a", 2)
await obj.save()

await Model.objects.filter(pk=obj.pk).update(data=JsonSet(F("data"), "$.a", 3))
```

A `Function` subclass needs nothing more for [statement plans](../querying/query-plan-cache.md): it inherits how a
function describes its plan - its class, its field, the type of each literal argument (the values
themselves are bound per query) and `get_plan_options()`. Override `get_plan_options()` when an
option of your function is written into its SQL text rather than bound, so two calls with different
options never share a plan:

```python
class RoundTo(Function):
    def __init__(self, field: str, precision: int = 0) -> None:
        super().__init__(field)
        self.precision = precision

    def get_plan_options(self) -> tuple[Any, ...]:
        return (self.precision,)
```

## Writing a custom expression {: #writing-a-custom-expression }

An `Expression` subclass that isn't a `Function` - its own `get_result()` building the SQL term -
also says how a query holding it keeps a [statement plan](../querying/query-plan-cache.md#plan-descriptions): either it
describes itself with `get_plan_description(context)`, or it declares `plannable = False`. A class
doing neither is rejected when it is defined (`TypeError`), so a new expression never reuses a plan
built for another one by accident.

`get_plan_description()` returns a `PlanDescription(structure, values)`: `structure` is everything
that changes the SQL text the expression builds (it becomes part of the plan key), and `values` are
the values the expression binds as parameters, in the order its `get_result()` records them.
Describe an argument through its own description - `Expression.get_argument_plan_description(value,
context)` gives an expression's own, and a literal's type with the literal bound - and return `None`
when an instance keeps no plan:

```python
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.plans.description import PlanContext, PlanDescription


class Clamp(Expression):
    def __init__(self, field: str, low: int, high: int) -> None:
        self.field, self.low, self.high = field, low, high

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        ...  # LEAST(GREATEST(field, ?), ?) - the bounds are bound as parameters

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        low = self.get_argument_plan_description(self.low, context)
        high = self.get_argument_plan_description(self.high, context)
        if low is None or high is None:
            return None
        return PlanDescription((Clamp, self.field, low.structure, high.structure), low.values + high.values)


class Unplanned(Expression):
    plannable = False  # a query holding it is built in full every time

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult: ...
```

An expression whose SQL text takes no value at all (`PI()`, the current moment) subclasses
`hare.query.expressions.base.ConstantExpression`, which describes it by its class alone.
