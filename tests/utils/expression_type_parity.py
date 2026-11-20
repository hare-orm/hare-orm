from tests.shared_models.testmodels_postgres import ExpressionTypeParity


async def annotated_values(expression) -> list:
    return await ExpressionTypeParity.objects.all().annotate(c=expression).order_by("num").values_list("c", flat=True)


def assert_same_values(actual: list, expected: list) -> None:
    assert actual == expected
    assert [type(value) for value in actual] == [type(value) for value in expected]
    assert [str(value) for value in actual] == [str(value) for value in expected]
