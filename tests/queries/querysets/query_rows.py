"""The pydantic schema of the rows of the query API tests - imported by the tests of the pydantic environment."""

from pydantic import BaseModel


class QueryRow(BaseModel):
    id: int
    name: str
