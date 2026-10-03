from hare.query.functions.comparison.greatest_least_function import GreatestLeastFunction
from hare.utils.declared_subclass import DeclaredSubclass

Greatest = DeclaredSubclass.make(
    GreatestLeastFunction,
    "Greatest",
    __package__,
    """The greatest of the values: ``Greatest("updated_at", "created_at")``.""",
    function_name="GREATEST",
)


Least = DeclaredSubclass.make(
    GreatestLeastFunction,
    "Least",
    __package__,
    """The least of the values: ``Least("price", "sale_price")``.""",
    function_name="LEAST",
)
