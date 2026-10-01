from hare.query.expressions import Function
from hare.query.functions.text.text_function import TextFunction
from hare.sql import functions
from hare.utils.declared_subclass import DeclaredSubclass

Trim = DeclaredSubclass.make(
    Function,
    "Trim",
    __package__,
    """Trims whitespace off edges of text, e.g. ``Trim("field_name")``.""",
    function_name="TRIM",
)


Lower = DeclaredSubclass.make(
    Function,
    "Lower",
    __package__,
    """Converts text to lower case, e.g. ``Lower("field_name")``.""",
    database_func=functions.Lower,
)


Upper = DeclaredSubclass.make(
    Function,
    "Upper",
    __package__,
    """Converts text to upper case, e.g. ``Upper("field_name")``.""",
    database_func=functions.Upper,
)


Left = DeclaredSubclass.make(
    TextFunction,
    "Left",
    __package__,
    """``Left(text, length)`` - the first characters; a negative length drops the last ones.""",
    function_name="LEFT",
    arity=(2, 2),
)


Right = DeclaredSubclass.make(
    TextFunction,
    "Right",
    __package__,
    """``Right(text, length)`` - the last characters; a negative length drops the first ones.""",
    function_name="RIGHT",
    arity=(2, 2),
)


Substr = DeclaredSubclass.make(
    TextFunction,
    "Substr",
    __package__,
    """``Substr(text, position, length=None)`` - from a 1-based position, to the end without a length.""",
    function_name="SUBSTR",
    arity=(2, 3),
)


StrIndex = DeclaredSubclass.make(
    TextFunction,
    "StrIndex",
    __package__,
    """``StrIndex(text, substring)`` - the 1-based position of the first occurrence, 0 without one.""",
    function_name="STRPOS",
    arity=(2, 2),
    returns_integer=True,
)


Repeat = DeclaredSubclass.make(
    TextFunction,
    "Repeat",
    __package__,
    """``Repeat(text, count)``.""",
    function_name="REPEAT",
    arity=(2, 2),
)


Reverse = DeclaredSubclass.make(
    TextFunction,
    "Reverse",
    __package__,
    """The characters in reverse order.""",
    function_name="REVERSE",
)


LTrim = DeclaredSubclass.make(
    TextFunction,
    "LTrim",
    __package__,
    """Leading spaces removed.""",
    function_name="LTRIM",
)


RTrim = DeclaredSubclass.make(
    TextFunction,
    "RTrim",
    __package__,
    """Trailing spaces removed.""",
    function_name="RTRIM",
)


Chr = DeclaredSubclass.make(
    TextFunction,
    "Chr",
    __package__,
    """The character of a Unicode code point.""",
    function_name="CHR",
)


Ord = DeclaredSubclass.make(
    TextFunction,
    "Ord",
    __package__,
    """The Unicode code point of the first character, 0 of an empty text.""",
    function_name="ASCII",
    returns_integer=True,
)


MD5 = DeclaredSubclass.make(
    TextFunction,
    "MD5",
    __package__,
    """The MD5 hex digest of the UTF-8 text.""",
    function_name="MD5",
)


SHA1 = DeclaredSubclass.make(
    TextFunction,
    "SHA1",
    __package__,
    """The SHA-1 hex digest of the UTF-8 text - needs the ``pgcrypto`` extension on Postgres.""",
    function_name="SHA1",
)


SHA224 = DeclaredSubclass.make(
    TextFunction,
    "SHA224",
    __package__,
    """The SHA-224 hex digest of the UTF-8 text.""",
    function_name="SHA224",
)


SHA256 = DeclaredSubclass.make(
    TextFunction,
    "SHA256",
    __package__,
    """The SHA-256 hex digest of the UTF-8 text.""",
    function_name="SHA256",
)


SHA384 = DeclaredSubclass.make(
    TextFunction,
    "SHA384",
    __package__,
    """The SHA-384 hex digest of the UTF-8 text.""",
    function_name="SHA384",
)


SHA512 = DeclaredSubclass.make(
    TextFunction,
    "SHA512",
    __package__,
    """The SHA-512 hex digest of the UTF-8 text.""",
    function_name="SHA512",
)
