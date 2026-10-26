from pydantic import BaseModel


class PydanticDocument(BaseModel):
    """The declared type of the JSON field sample holding a pydantic model."""

    title: str
    count: int
