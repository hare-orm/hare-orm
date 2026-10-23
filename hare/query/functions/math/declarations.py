from __future__ import annotations

from hare.classes.declared_subclass import DeclaredSubclass
from hare.query.functions.math.enums import MathResultType
from hare.query.functions.math.math_function import MathFunction

Abs = DeclaredSubclass.make(
    MathFunction,
    "Abs",
    __package__,
    """The absolute value, of the argument's type.""",
    function_name="ABS",
    output=MathResultType.SAME,
)


Ceil = DeclaredSubclass.make(
    MathFunction,
    "Ceil",
    __package__,
    """The smallest whole number not below the argument, of the argument's type.""",
    function_name="CEIL",
    output=MathResultType.SAME,
)


Floor = DeclaredSubclass.make(
    MathFunction,
    "Floor",
    __package__,
    """The largest whole number not above the argument, of the argument's type.""",
    function_name="FLOOR",
    output=MathResultType.SAME,
)


Sign = DeclaredSubclass.make(
    MathFunction,
    "Sign",
    __package__,
    """-1, 0 or 1, of the argument's type.""",
    function_name="SIGN",
    output=MathResultType.SAME,
)


Power = DeclaredSubclass.make(
    MathFunction,
    "Power",
    __package__,
    """``Power(base, exponent)``.""",
    function_name="POWER",
    arity=2,
)


Sqrt = DeclaredSubclass.make(
    MathFunction,
    "Sqrt",
    __package__,
    """The square root.""",
    function_name="SQRT",
)


Exp = DeclaredSubclass.make(
    MathFunction,
    "Exp",
    __package__,
    """e raised to the argument.""",
    function_name="EXP",
)


Ln = DeclaredSubclass.make(
    MathFunction,
    "Ln",
    __package__,
    """The natural logarithm.""",
    function_name="LN",
)


Log = DeclaredSubclass.make(
    MathFunction,
    "Log",
    __package__,
    """``Log(base, x)`` - the logarithm of ``x`` to ``base``, as Django.""",
    function_name="LOG",
    arity=2,
)


Sin = DeclaredSubclass.make(
    MathFunction,
    "Sin",
    __package__,
    """The sine of an angle in radians.""",
    function_name="SIN",
    output=MathResultType.FLOAT,
)


Cos = DeclaredSubclass.make(
    MathFunction,
    "Cos",
    __package__,
    """The cosine of an angle in radians.""",
    function_name="COS",
    output=MathResultType.FLOAT,
)


Tan = DeclaredSubclass.make(
    MathFunction,
    "Tan",
    __package__,
    """The tangent of an angle in radians.""",
    function_name="TAN",
    output=MathResultType.FLOAT,
)


Cot = DeclaredSubclass.make(
    MathFunction,
    "Cot",
    __package__,
    """The cotangent of an angle in radians.""",
    function_name="COT",
    output=MathResultType.FLOAT,
)


ASin = DeclaredSubclass.make(
    MathFunction,
    "ASin",
    __package__,
    """The arcsine, in radians.""",
    function_name="ASIN",
    output=MathResultType.FLOAT,
)


ACos = DeclaredSubclass.make(
    MathFunction,
    "ACos",
    __package__,
    """The arccosine, in radians.""",
    function_name="ACOS",
    output=MathResultType.FLOAT,
)


ATan = DeclaredSubclass.make(
    MathFunction,
    "ATan",
    __package__,
    """The arctangent, in radians.""",
    function_name="ATAN",
    output=MathResultType.FLOAT,
)


ATan2 = DeclaredSubclass.make(
    MathFunction,
    "ATan2",
    __package__,
    """``ATan2(y, x)`` - the arctangent of ``y / x``, in radians, in the quadrant of the point.""",
    function_name="ATAN2",
    output=MathResultType.FLOAT,
    arity=2,
)


Degrees = DeclaredSubclass.make(
    MathFunction,
    "Degrees",
    __package__,
    """Radians converted to degrees.""",
    function_name="DEGREES",
    output=MathResultType.FLOAT,
)


Radians = DeclaredSubclass.make(
    MathFunction,
    "Radians",
    __package__,
    """Degrees converted to radians.""",
    function_name="RADIANS",
    output=MathResultType.FLOAT,
)
