# Custom functions and expressions

A project adds a SQL function hare doesn't have as a `Function` subclass, and anything else a query
can compute as an `Expression` subclass; both then work in `annotate()`, `filter()`, `order_by()`,
aggregates and `update()` like the built-in ones.

## <a id="writing-a-custom-function"></a>Writing a custom function

Every class of [Database functions](../querying/functions.md) is a subclass of `Function` (or, for an
aggregate, `Aggregate`), both from `hare.query.expressions` — write your own the same way when the built-in set doesn't cover a database function you need. A function written
as `NAME(arguments...)` only needs its SQL name in `function_name`; the arguments are the field (a
name, `F()` or another expression) followed by the values given after it:

```python
from hare.query.expressions import F, Function

class JsonSet(Function):
    function_name = "JSON_SET"
```

A function with a SQL shape of its own (`EXTRACT(part FROM value)`, `x::type`) leaves
`function_name` unset and sets `database_function` to a `hare.sql.terms.Function` subclass building that
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
function describes its plan — its class, its field, the type of each literal argument (the values
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

## <a id="writing-a-custom-expression"></a>Writing a custom expression

An `Expression` subclass that isn't a `Function` — its own `get_result()` building the SQL term —
also says how a query holding it keeps a [statement plan](../querying/query-plan-cache.md#plan-descriptions): it
declares how each of its attributes meets the plan in `plan_parts`, and its description is generated
from them; or it declares `plannable = False`. A class doing neither is rejected when it is defined
(`TypeError`), so a new expression never reuses a plan built for another one by accident.

Each part is an attribute (or a method, for the `*_METHOD` types) with its `PlanPartType`, in the
order of the key:

| Part type | The attribute is |
|---|---|
| `KEY` | written into the SQL text as it is — an operator, a field or annotation name, an option |
| `KEYS` | a sequence written into the SQL text — field names, orderings |
| `KEY_METHOD` | a method returning what is written into the SQL text |
| `LITERAL` | a literal bound as a parameter (as `Value` binds one) |
| `ARGUMENT` / `ARGUMENTS` | an argument (a sequence of them): an expression by its own description, `None` as `NULL`, a `hare.sql` term by its text and values, any other literal by its type, bound; a list or tuple literal keeps no plan |
| `ENCODED_ARGUMENT` | an argument whose literal the build encodes into one bound value — a point as its text, a vector: a list or tuple literal is bound whole |
| `ARGUMENT_METHOD` | a method giving an argument, described as an `ARGUMENT` |
| `VALUE_METHOD` | a method giving a value bound as a parameter |
| `PARAMETERS` | the `ValueWrapper` parameters the object made when it was made (`RawSQL`'s) |
| `FIELD` / `FIELDS` | what a function reads (a sequence of them) — an expression, a field or annotation name, a `hare.sql` term |
| `EXPRESSION` / `EXPRESSIONS` | an expression or condition (a sequence of them) by its own description, `None` as absent |
| `EXPRESSION_METHOD` | a method giving an expression or condition |
| `JOIN_CONDITION_METHOD` | a method giving a condition the build folds into JOINs — its values are bound into each JOIN it is folded into, into none when there is none |
| `FILTERS` | the keyword filters of a condition |
| `QUERY` / `QUERY_METHOD` | a query built into the statement (a method giving one) |
| `PLAN_KEY` | an object giving its own plan key (`get_plan_key()`) |
| `KEEPS_PLAN_METHOD` | a method telling whether the object keeps a plan at all — `False` keeps none |
| `NONE` | described through another part (a method reading it), or no part of the SQL text at all — a cache, bookkeeping |

Every attribute of the class is classified: a slot of a class with `__slots__` that no part names is
rejected when the class is defined, and a pytest run with `--verify-plans` checks the attributes of
every object it describes. A base class lists the bookkeeping attributes of all its objects in
`unplanned_attributes` (`Expression`'s record of its constructor's arguments).

The build resolves each argument the way its part describes it — `ExpressionArguments.get_result(self,
attribute, value, expression_context)` for an `ARGUMENT` (`binds_whole=True` for an
`ENCODED_ARGUMENT`, `encoder=` converting a literal into its bound form first),
`ExpressionArguments.get_field_result(...)` for a `FIELD`: an expression is resolved into its result, a
SQL term records its values, and a literal is bound as a parameter recorded under the attribute
holding it while the query records its plan. A plan binds a value into every reference recorded
under it, however many times the build resolved it.

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
    plannable = False  # a query holding it is built in full every time

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult: ...
```

A class whose description no part type covers writes `get_plan_description(context)` itself and
returns a `PlanDescription(structure, values, origins)`, `None` when an instance keeps no plan; while
a query records its plan (`PlanOrigins.records`) `origins` lists where each value comes from —
`PlanOrigins.get_value_origin(self, attribute)` — and it is `None` otherwise.

An expression whose SQL text takes no value at all (`PI()`, the current moment) subclasses
`hare.query.expressions.ConstantExpression`, which describes it by its class alone.
