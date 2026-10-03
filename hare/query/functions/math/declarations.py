from hare.query.functions.math.math_function import MathFunction
from hare.utils.declared_subclass import DeclaredSubclass

Abs = DeclaredSubclass.make(
    MathFunction,
    "Abs",
    __package__,
    """The absolute value, of the argument's type.""",
    function_name="ABS",
    output="same",
)


Ceil = DeclaredSubclass.make(
    MathFunction,
    "Ceil",
    __package__,
    """The smallest whole number not below the argument, of the argument's type.""",
    function_name="CEIL",
    output="same",
)


Floor = DeclaredSubclass.make(
    MathFunction,
    "Floor",
    __package__,
    """The largest whole number not above the argument, of the argument's type.""",
    function_name="FLOOR",
    output="same",
)


Sign = DeclaredSubclass.make(
    MathFunction,
    "Sign",
    __package__,
    """-1, 0 or 1, of the argument's type.""",
    function_name="SIGN",
    output="same",
)


Mod = DeclaredSubclass.make(
    MathFunction,
    "Mod",
    __package__,
    """The remainder of ``Mod(dividend, divisor)``, with the dividend's sign - an integer of two integers.""",
    function_name="MOD",
    output="remainder",
    arity=2,
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
    output="float",
)


Cos = DeclaredSubclass.make(
    MathFunction,
    "Cos",
    __package__,
    """The cosine of an angle in radians.""",
    function_name="COS",
    output="float",
)


Tan = DeclaredSubclass.make(
    MathFunction,
    "Tan",
    __package__,
    """The tangent of an angle in radians.""",
    function_name="TAN",
    output="float",
)


Cot = DeclaredSubclass.make(
    MathFunction,
    "Cot",
    __package__,
    """The cotangent of an angle in radians.""",
    function_name="COT",
    output="float",
)


ASin = DeclaredSubclass.make(
    MathFunction,
    "ASin",
    __package__,
    """The arcsine, in radians.""",
    function_name="ASIN",
    output="float",
)


ACos = DeclaredSubclass.make(
    MathFunction,
    "ACos",
    __package__,
    """The arccosine, in radians.""",
    function_name="ACOS",
    output="float",
)


ATan = DeclaredSubclass.make(
    MathFunction,
    "ATan",
    __package__,
    """The arctangent, in radians.""",
    function_name="ATAN",
    output="float",
)


ATan2 = DeclaredSubclass.make(
    MathFunction,
    "ATan2",
    __package__,
    """``ATan2(y, x)`` - the arctangent of ``y / x``, in radians, in the quadrant of the point.""",
    function_name="ATAN2",
    output="float",
    arity=2,
)


Degrees = DeclaredSubclass.make(
    MathFunction,
    "Degrees",
    __package__,
    """Radians converted to degrees.""",
    function_name="DEGREES",
    output="float",
)


Radians = DeclaredSubclass.make(
    MathFunction,
    "Radians",
    __package__,
    """Degrees converted to radians.""",
    function_name="RADIANS",
    output="float",
)
