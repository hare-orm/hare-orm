"""What the typing tools - the mypy plugin, ``hare stubs`` - know of the models: the types of the
values a query takes and gives (``FilterValueTypes``), create/update inputs (``WriteValueTypes``), and
the models bound without a database (``BoundModels``). ``ValueType`` keeps accepted inputs separate
from read types so each frontend can render the same contracts in its own type system."""
