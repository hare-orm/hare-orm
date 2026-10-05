"""A request query of a model with Meta.tenant_field sees the rows of the current tenant only -
its Meta.queryset is built at import, the tenant applies when the query runs."""

import pytest
import pytest_asyncio

from hare.models.tenancy.tenancy import Tenancy
from tests.contrib.request_query.models import Ticket
from tests.contrib.request_query.queries import TicketQuery


@pytest_asyncio.fixture
async def tickets(db_request_query):
    for ticket_id, company_id, subject in ((1, 1, "printer"), (2, 1, "mail"), (3, 2, "printer")):
        await Ticket.objects.create(id=ticket_id, company_id=company_id, subject=subject)


@pytest.mark.asyncio
async def test_each_tenant_sees_its_own_rows(tickets):
    with Tenancy.scope(1):
        page = await TicketQuery().page()
        assert [ticket.id for ticket in page.result] == [1, 2]
        assert page.count == 2
        assert await TicketQuery().count_by("subject") == {"subject": {"mail": 1, "printer": 1}}
    with Tenancy.scope(2):
        assert [ticket.id for ticket in await TicketQuery(subject__icontains="print").fetch()] == [3]
        assert await TicketQuery(subject__icontains="mail").exists() is False


@pytest.mark.asyncio
async def test_a_scope_of_several_tenants_or_by_model(tickets):
    with Tenancy.scope(Tenancy.any_of(1, 2)):
        page = await TicketQuery().page()
        assert [ticket.id for ticket in page.result] == [1, 2, 3]
        assert await TicketQuery().count_by("subject") == {"subject": {"mail": 1, "printer": 2}}
    with Tenancy.scope({Ticket: 2}):
        assert [ticket.id for ticket in await TicketQuery().fetch()] == [3]
    with Tenancy.scope({Ticket: Tenancy.ALL}):
        assert await TicketQuery(subject__icontains="print").count() == 2
    with Tenancy.scope(1):
        assert [ticket.id for ticket in await TicketQuery().fetch()] == [1, 2]
