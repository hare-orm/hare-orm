"""What each parameter of a request query is, for building a UI over it - a filter panel, a
relation picker, a sort menu - beyond what the JSON schema says: the filter a parameter applies,
the relation it goes through, the bound it pairs with, the choices of an option."""

from hare.contrib.request_query.descriptions.parameter_choice import ParameterChoice
from hare.contrib.request_query.descriptions.parameter_describer import ParameterDescriber
from hare.contrib.request_query.descriptions.parameter_description import ParameterDescription
from hare.contrib.request_query.descriptions.relation_description import RelationDescription

__all__ = [
    "ParameterChoice",
    "RelationDescription",
    "ParameterDescription",
    "ParameterDescriber",
]
