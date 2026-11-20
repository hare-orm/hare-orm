from typing import Any

from hare import fields
from hare.models import Model


def make_model(
    model_name: str, table: str | None = None, *, app_label: str = "shop", **model_fields: Any
) -> type[Model]:
    """Build a migration test model with an explicit integer primary key."""
    attributes: dict[str, Any] = {"id": fields.IntField(primary_key=True), **model_fields}
    meta_options = {"app": app_label} if table is None else {"app": app_label, "table": table}
    attributes["Meta"] = type("Meta", (), meta_options)
    return type(model_name, (Model,), attributes)
