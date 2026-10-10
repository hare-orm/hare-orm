import copy
import typing
import uuid
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ConfigDict, ValidationError, field_validator

from hare import fields, prefetch_related_objects
from hare.contrib.pydantic import (
    PydanticModel,
    pydantic_model_creator,
    pydantic_queryset_creator,
)
from hare.contrib.pydantic.creation.pydantic_model_creator import PydanticModelCreator
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import NoValuesFetched
from hare.models import Model
from tests.testmodels import (
    Address,
    BinaryFields,
    CallableDefault,
    CamelCaseAliasPerson,
    CompositePkThing,
    DatetimeFields,
    DirtyTrackedThing,
    DocumentRevisionNote,
    Employee,
    EnumFields,
    Event,
    IntFields,
    JSONFields,
    ModelTestPydanticMetaBackwardRelations1,
    ModelTestPydanticMetaBackwardRelations2,
    Reporter,
    SoftDeleteChildCascadeSoft,
    Tag,
    Team,
    TenantFkNamedOrder,
    TenantFkOrder,
    Tournament,
    User,
    UUIDFields,
    ValidatorModel,
    VersionedDocument,
    json_pydantic_default,
)


# Fixtures for TestPydantic
@pytest_asyncio.fixture
async def pydantic_setup(db):
    """Setup for pydantic tests with models and data."""
    Event_Pydantic = pydantic_model_creator(Event)
    Event_Pydantic_List = pydantic_queryset_creator(Event)
    Tournament_Pydantic = pydantic_model_creator(Tournament)
    Team_Pydantic = pydantic_model_creator(Team)
    Address_Pydantic = pydantic_model_creator(Address)
    ModelTestPydanticMetaBackwardRelations1_Pydantic = pydantic_model_creator(ModelTestPydanticMetaBackwardRelations1)
    ModelTestPydanticMetaBackwardRelations2_Pydantic = pydantic_model_creator(ModelTestPydanticMetaBackwardRelations2)

    class PydanticMetaOverride:
        backward_relations = False

    Event_Pydantic_non_backward_from_override = pydantic_model_creator(
        Event, meta_override=PydanticMetaOverride, name="Event_non_backward"
    )

    tournament = await Tournament.objects.create(name="New Tournament")
    reporter = await Reporter.objects.create(name="The Reporter")
    event = await Event.objects.create(name="Test", tournament=tournament, reporter=reporter)
    event2 = await Event.objects.create(name="Test2", tournament=tournament)
    address = await Address.objects.create(city="Santa Monica", street="Ocean", event=event)
    team1 = await Team.objects.create(name="Onesies")
    team2 = await Team.objects.create(name="T-Shirts")
    await event.participants.add(team1, team2)
    await event2.participants.add(team1, team2)

    return {
        "Event_Pydantic": Event_Pydantic,
        "Event_Pydantic_List": Event_Pydantic_List,
        "Tournament_Pydantic": Tournament_Pydantic,
        "Team_Pydantic": Team_Pydantic,
        "Address_Pydantic": Address_Pydantic,
        "ModelTestPydanticMetaBackwardRelations1_Pydantic": ModelTestPydanticMetaBackwardRelations1_Pydantic,
        "ModelTestPydanticMetaBackwardRelations2_Pydantic": ModelTestPydanticMetaBackwardRelations2_Pydantic,
        "Event_Pydantic_non_backward_from_override": Event_Pydantic_non_backward_from_override,
        "tournament": tournament,
        "reporter": reporter,
        "event": event,
        "event2": event2,
        "address": address,
        "team1": team1,
        "team2": team2,
    }


@pytest.mark.asyncio
async def test_backward_relations_with_meta_override(db, pydantic_setup):
    Event_Pydantic = pydantic_setup["Event_Pydantic"]
    Event_Pydantic_non_backward_from_override = pydantic_setup["Event_Pydantic_non_backward_from_override"]

    event_schema = copy.deepcopy(dict(Event_Pydantic.model_json_schema()))
    event_non_backward_schema_by_override = copy.deepcopy(
        dict(Event_Pydantic_non_backward_from_override.model_json_schema())
    )
    assert "address" in event_schema["properties"]
    assert "address" not in event_non_backward_schema_by_override["properties"]
    del event_schema["properties"]["address"]
    assert event_schema["properties"] == event_non_backward_schema_by_override["properties"]


@pytest.mark.asyncio
async def test_backward_relations_with_pydantic_meta(db, pydantic_setup):
    ModelTestPydanticMetaBackwardRelations1_Pydantic = pydantic_setup[
        "ModelTestPydanticMetaBackwardRelations1_Pydantic"
    ]
    ModelTestPydanticMetaBackwardRelations2_Pydantic = pydantic_setup[
        "ModelTestPydanticMetaBackwardRelations2_Pydantic"
    ]

    test_model1_schema = ModelTestPydanticMetaBackwardRelations1_Pydantic.model_json_schema()
    test_model2_schema = ModelTestPydanticMetaBackwardRelations2_Pydantic.model_json_schema()
    assert "threes" in test_model2_schema["properties"]
    assert "threes" not in test_model1_schema["properties"]
    del test_model2_schema["properties"]["threes"]
    assert test_model2_schema["properties"] == test_model1_schema["properties"]
    print(test_model2_schema)


@pytest.mark.asyncio
async def test_backward_relations_annotated_kept(db):
    """backward_relations=False should keep explicitly annotated ReverseRelation fields."""
    from tests.testmodels import ModelTestPydanticAnnotatedBackwardRel

    Pydantic = pydantic_model_creator(ModelTestPydanticAnnotatedBackwardRel)
    schema = Pydantic.model_json_schema()
    # Annotated backward relation should be included
    assert "annotated_children" in schema["properties"]
    # Unannotated backward relation should be excluded
    assert "unannotated_children" not in schema["properties"]


@pytest.mark.asyncio
async def test_get_fetch_fields_includes_inherited_relations(db):
    """A subclass that adds its own field used to lose every relation field inherited from its
    base entirely: cls.__annotations__ (unlike get_type_hints()) is only the annotations
    declared directly on cls itself, not merged with its bases - a plain Python subclassing
    gotcha, not something specific to pydantic_model_creator."""
    Event_Pydantic = pydantic_model_creator(Event, name="EventPydanticFetchFieldsBase")

    class Event_Pydantic_Sub(Event_Pydantic):
        extra_field: str = "x"

    base_fetch_fields = Event_Pydantic._get_fetch_fields()
    sub_fetch_fields = Event_Pydantic_Sub._get_fetch_fields()

    assert "tournament" in base_fetch_fields
    assert set(sub_fetch_fields) == set(base_fetch_fields)


@pytest.mark.asyncio
async def test_event_schema(db, pydantic_setup):
    Event_Pydantic = pydantic_setup["Event_Pydantic"]
    assert Event_Pydantic.model_json_schema() == {
        "$defs": {
            "Address_pu7okpsgxmp32yqr_leaf": {
                "additionalProperties": False,
                "properties": {
                    "city": {"maxLength": 64, "title": "City", "type": "string"},
                    "street": {"maxLength": 128, "title": "Street", "type": "string"},
                    "event_id": {
                        "maximum": 9223372036854775807,
                        "minimum": -9223372036854775808,
                        "title": "Event Id",
                        "type": "integer",
                    },
                    "m2mwitho2opks": {
                        "items": {"$ref": "#/$defs/M2mWithO2oPk_2mchlzz2uvulfdyq_leaf"},
                        "title": "M2Mwitho2Opks",
                        "type": "array",
                    },
                },
                "required": ["city", "street", "event_id", "m2mwitho2opks"],
                "title": "Address",
                "type": "object",
            },
            "M2mWithO2oPk_2mchlzz2uvulfdyq_leaf": {
                "additionalProperties": False,
                "properties": {
                    "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
                    "name": {"maxLength": 64, "title": "Name", "type": "string"},
                },
                "required": ["id", "name"],
                "title": "M2mWithO2oPk",
                "type": "object",
            },
            "Reporter_e6s2ho4kgvyzgddv_leaf": {
                "additionalProperties": False,
                "description": "Whom is assigned as the reporter",
                "properties": {
                    "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
                    "name": {"title": "Name", "type": "string"},
                },
                "required": ["id", "name"],
                "title": "Reporter",
                "type": "object",
            },
            "Team_km6q3hpwepuhgvbj_leaf": {
                "additionalProperties": False,
                "description": "Team that is a playing",
                "properties": {
                    "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
                    "name": {"title": "Name", "type": "string"},
                    "alias": {
                        "anyOf": [
                            {"maximum": 2147483647, "minimum": -2147483648, "type": "integer"},
                            {"type": "null"},
                        ],
                        "default": None,
                        "nullable": True,
                        "title": "Alias",
                    },
                },
                "required": ["id", "name"],
                "title": "Team",
                "type": "object",
            },
            "Tournament_ln4hy2ux7h6l3w43_leaf": {
                "additionalProperties": False,
                "properties": {
                    "id": {"maximum": 32767, "minimum": -32768, "title": "Id", "type": "integer"},
                    "name": {"maxLength": 255, "title": "Name", "type": "string"},
                    "desc": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "default": None,
                        "nullable": True,
                        "title": "Desc",
                    },
                    "created": {
                        "default": None,
                        "format": "date-time",
                        "readOnly": True,
                        "title": "Created",
                        "type": "string",
                    },
                },
                "required": ["id", "name"],
                "title": "Tournament",
                "type": "object",
            },
        },
        "additionalProperties": False,
        "description": "Events on the calendar",
        "properties": {
            "event_id": {
                "maximum": 9223372036854775807,
                "minimum": -9223372036854775808,
                "title": "Event Id",
                "type": "integer",
            },
            "name": {"description": "The name", "title": "Name", "type": "string"},
            "tournament": {
                "$ref": "#/$defs/Tournament_ln4hy2ux7h6l3w43_leaf",
                "description": "What tournaments is a happenin'",
            },
            "reporter": {
                "anyOf": [{"$ref": "#/$defs/Reporter_e6s2ho4kgvyzgddv_leaf"}, {"type": "null"}],
                "default": None,
                "nullable": True,
                "title": "Reporter",
            },
            "participants": {
                "items": {"$ref": "#/$defs/Team_km6q3hpwepuhgvbj_leaf"},
                "title": "Participants",
                "type": "array",
            },
            "modified": {
                "default": None,
                "format": "date-time",
                "readOnly": True,
                "title": "Modified",
                "type": "string",
            },
            "token": {"title": "Token", "type": "string"},
            "alias": {
                "anyOf": [{"maximum": 2147483647, "minimum": -2147483648, "type": "integer"}, {"type": "null"}],
                "default": None,
                "nullable": True,
                "title": "Alias",
            },
            "address": {
                "anyOf": [{"$ref": "#/$defs/Address_pu7okpsgxmp32yqr_leaf"}, {"type": "null"}],
                "default": None,
                "nullable": True,
                "title": "Address",
            },
        },
        "required": ["event_id", "name", "tournament", "participants"],
        "title": "Event",
        "type": "object",
    }


@pytest.mark.asyncio
async def test_eventlist_schema(db, pydantic_setup):
    Event_Pydantic_List = pydantic_setup["Event_Pydantic_List"]
    assert Event_Pydantic_List.model_json_schema() == {
        "$defs": {
            "Address_pu7okpsgxmp32yqr_leaf": {
                "additionalProperties": False,
                "properties": {
                    "city": {"maxLength": 64, "title": "City", "type": "string"},
                    "street": {"maxLength": 128, "title": "Street", "type": "string"},
                    "event_id": {
                        "maximum": 9223372036854775807,
                        "minimum": -9223372036854775808,
                        "title": "Event Id",
                        "type": "integer",
                    },
                    "m2mwitho2opks": {
                        "items": {"$ref": "#/$defs/M2mWithO2oPk_2mchlzz2uvulfdyq_leaf"},
                        "title": "M2Mwitho2Opks",
                        "type": "array",
                    },
                },
                "required": ["city", "street", "event_id", "m2mwitho2opks"],
                "title": "Address",
                "type": "object",
            },
            "Event": {
                "additionalProperties": False,
                "description": "Events on the calendar",
                "properties": {
                    "event_id": {
                        "maximum": 9223372036854775807,
                        "minimum": -9223372036854775808,
                        "title": "Event Id",
                        "type": "integer",
                    },
                    "name": {"description": "The name", "title": "Name", "type": "string"},
                    "tournament": {
                        "$ref": "#/$defs/Tournament_ln4hy2ux7h6l3w43_leaf",
                        "description": "What tournaments is a happenin'",
                    },
                    "reporter": {
                        "anyOf": [{"$ref": "#/$defs/Reporter_e6s2ho4kgvyzgddv_leaf"}, {"type": "null"}],
                        "default": None,
                        "nullable": True,
                        "title": "Reporter",
                    },
                    "participants": {
                        "items": {"$ref": "#/$defs/Team_km6q3hpwepuhgvbj_leaf"},
                        "title": "Participants",
                        "type": "array",
                    },
                    "modified": {
                        "default": None,
                        "format": "date-time",
                        "readOnly": True,
                        "title": "Modified",
                        "type": "string",
                    },
                    "token": {"title": "Token", "type": "string"},
                    "alias": {
                        "anyOf": [
                            {"maximum": 2147483647, "minimum": -2147483648, "type": "integer"},
                            {"type": "null"},
                        ],
                        "default": None,
                        "nullable": True,
                        "title": "Alias",
                    },
                    "address": {
                        "anyOf": [{"$ref": "#/$defs/Address_pu7okpsgxmp32yqr_leaf"}, {"type": "null"}],
                        "default": None,
                        "nullable": True,
                        "title": "Address",
                    },
                },
                "required": ["event_id", "name", "tournament", "participants"],
                "title": "Event",
                "type": "object",
            },
            "M2mWithO2oPk_2mchlzz2uvulfdyq_leaf": {
                "additionalProperties": False,
                "properties": {
                    "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
                    "name": {"maxLength": 64, "title": "Name", "type": "string"},
                },
                "required": ["id", "name"],
                "title": "M2mWithO2oPk",
                "type": "object",
            },
            "Reporter_e6s2ho4kgvyzgddv_leaf": {
                "additionalProperties": False,
                "description": "Whom is assigned as the reporter",
                "properties": {
                    "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
                    "name": {"title": "Name", "type": "string"},
                },
                "required": ["id", "name"],
                "title": "Reporter",
                "type": "object",
            },
            "Team_km6q3hpwepuhgvbj_leaf": {
                "additionalProperties": False,
                "description": "Team that is a playing",
                "properties": {
                    "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
                    "name": {"title": "Name", "type": "string"},
                    "alias": {
                        "anyOf": [
                            {"maximum": 2147483647, "minimum": -2147483648, "type": "integer"},
                            {"type": "null"},
                        ],
                        "default": None,
                        "nullable": True,
                        "title": "Alias",
                    },
                },
                "required": ["id", "name"],
                "title": "Team",
                "type": "object",
            },
            "Tournament_ln4hy2ux7h6l3w43_leaf": {
                "additionalProperties": False,
                "properties": {
                    "id": {"maximum": 32767, "minimum": -32768, "title": "Id", "type": "integer"},
                    "name": {"maxLength": 255, "title": "Name", "type": "string"},
                    "desc": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "default": None,
                        "nullable": True,
                        "title": "Desc",
                    },
                    "created": {
                        "default": None,
                        "format": "date-time",
                        "readOnly": True,
                        "title": "Created",
                        "type": "string",
                    },
                },
                "required": ["id", "name"],
                "title": "Tournament",
                "type": "object",
            },
        },
        "description": "Events on the calendar",
        "items": {"$ref": "#/$defs/Event"},
        "title": "Event_list",
        "type": "array",
    }


@pytest.mark.asyncio
async def test_address_schema(db, pydantic_setup):
    Address_Pydantic = pydantic_setup["Address_Pydantic"]
    assert Address_Pydantic.model_json_schema() == {
        "$defs": {
            "Event_7yj27jxzw6oy6quw_leaf": {
                "additionalProperties": False,
                "description": "Events on the calendar",
                "properties": {
                    "event_id": {
                        "maximum": 9223372036854775807,
                        "minimum": -9223372036854775808,
                        "title": "Event Id",
                        "type": "integer",
                    },
                    "name": {"description": "The name", "title": "Name", "type": "string"},
                    "tournament": {
                        "$ref": "#/$defs/Tournament_ln4hy2ux7h6l3w43_leaf",
                        "description": "What tournaments is a happenin'",
                    },
                    "reporter": {
                        "anyOf": [{"$ref": "#/$defs/Reporter_e6s2ho4kgvyzgddv_leaf"}, {"type": "null"}],
                        "default": None,
                        "nullable": True,
                        "title": "Reporter",
                    },
                    "participants": {
                        "items": {"$ref": "#/$defs/Team_km6q3hpwepuhgvbj_leaf"},
                        "title": "Participants",
                        "type": "array",
                    },
                    "modified": {
                        "default": None,
                        "format": "date-time",
                        "readOnly": True,
                        "title": "Modified",
                        "type": "string",
                    },
                    "token": {"title": "Token", "type": "string"},
                    "alias": {
                        "anyOf": [
                            {"maximum": 2147483647, "minimum": -2147483648, "type": "integer"},
                            {"type": "null"},
                        ],
                        "default": None,
                        "nullable": True,
                        "title": "Alias",
                    },
                },
                "required": ["event_id", "name", "tournament", "participants"],
                "title": "Event",
                "type": "object",
            },
            "M2mWithO2oPk_2mchlzz2uvulfdyq_leaf": {
                "additionalProperties": False,
                "properties": {
                    "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
                    "name": {"maxLength": 64, "title": "Name", "type": "string"},
                },
                "required": ["id", "name"],
                "title": "M2mWithO2oPk",
                "type": "object",
            },
            "Reporter_e6s2ho4kgvyzgddv_leaf": {
                "additionalProperties": False,
                "description": "Whom is assigned as the reporter",
                "properties": {
                    "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
                    "name": {"title": "Name", "type": "string"},
                },
                "required": ["id", "name"],
                "title": "Reporter",
                "type": "object",
            },
            "Team_km6q3hpwepuhgvbj_leaf": {
                "additionalProperties": False,
                "description": "Team that is a playing",
                "properties": {
                    "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
                    "name": {"title": "Name", "type": "string"},
                    "alias": {
                        "anyOf": [
                            {"maximum": 2147483647, "minimum": -2147483648, "type": "integer"},
                            {"type": "null"},
                        ],
                        "default": None,
                        "nullable": True,
                        "title": "Alias",
                    },
                },
                "required": ["id", "name"],
                "title": "Team",
                "type": "object",
            },
            "Tournament_ln4hy2ux7h6l3w43_leaf": {
                "additionalProperties": False,
                "properties": {
                    "id": {"maximum": 32767, "minimum": -32768, "title": "Id", "type": "integer"},
                    "name": {"maxLength": 255, "title": "Name", "type": "string"},
                    "desc": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "default": None,
                        "nullable": True,
                        "title": "Desc",
                    },
                    "created": {
                        "default": None,
                        "format": "date-time",
                        "readOnly": True,
                        "title": "Created",
                        "type": "string",
                    },
                },
                "required": ["id", "name"],
                "title": "Tournament",
                "type": "object",
            },
        },
        "additionalProperties": False,
        "properties": {
            "city": {"maxLength": 64, "title": "City", "type": "string"},
            "street": {"maxLength": 128, "title": "Street", "type": "string"},
            "event": {"$ref": "#/$defs/Event_7yj27jxzw6oy6quw_leaf"},
            "event_id": {
                "maximum": 9223372036854775807,
                "minimum": -9223372036854775808,
                "title": "Event Id",
                "type": "integer",
            },
            "m2mwitho2opks": {
                "items": {"$ref": "#/$defs/M2mWithO2oPk_2mchlzz2uvulfdyq_leaf"},
                "title": "M2Mwitho2Opks",
                "type": "array",
            },
        },
        "required": ["city", "street", "event", "event_id", "m2mwitho2opks"],
        "title": "Address",
        "type": "object",
    }


@pytest.mark.asyncio
async def test_tournament_schema(db, pydantic_setup):
    Tournament_Pydantic = pydantic_setup["Tournament_Pydantic"]
    assert Tournament_Pydantic.model_json_schema() == {
        "$defs": {
            "Address_pu7okpsgxmp32yqr_leaf": {
                "additionalProperties": False,
                "properties": {
                    "city": {"maxLength": 64, "title": "City", "type": "string"},
                    "street": {"maxLength": 128, "title": "Street", "type": "string"},
                    "event_id": {
                        "maximum": 9223372036854775807,
                        "minimum": -9223372036854775808,
                        "title": "Event Id",
                        "type": "integer",
                    },
                    "m2mwitho2opks": {
                        "items": {"$ref": "#/$defs/M2mWithO2oPk_2mchlzz2uvulfdyq_leaf"},
                        "title": "M2Mwitho2Opks",
                        "type": "array",
                    },
                },
                "required": ["city", "street", "event_id", "m2mwitho2opks"],
                "title": "Address",
                "type": "object",
            },
            "Event_czm5a53hsvvseysl_leaf": {
                "additionalProperties": False,
                "description": "Events on the calendar",
                "properties": {
                    "event_id": {
                        "maximum": 9223372036854775807,
                        "minimum": -9223372036854775808,
                        "title": "Event Id",
                        "type": "integer",
                    },
                    "name": {"description": "The name", "title": "Name", "type": "string"},
                    "reporter": {
                        "anyOf": [{"$ref": "#/$defs/Reporter_e6s2ho4kgvyzgddv_leaf"}, {"type": "null"}],
                        "default": None,
                        "nullable": True,
                        "title": "Reporter",
                    },
                    "participants": {
                        "items": {"$ref": "#/$defs/Team_km6q3hpwepuhgvbj_leaf"},
                        "title": "Participants",
                        "type": "array",
                    },
                    "modified": {
                        "default": None,
                        "format": "date-time",
                        "readOnly": True,
                        "title": "Modified",
                        "type": "string",
                    },
                    "token": {"title": "Token", "type": "string"},
                    "alias": {
                        "anyOf": [
                            {"maximum": 2147483647, "minimum": -2147483648, "type": "integer"},
                            {"type": "null"},
                        ],
                        "default": None,
                        "nullable": True,
                        "title": "Alias",
                    },
                    "address": {
                        "anyOf": [{"$ref": "#/$defs/Address_pu7okpsgxmp32yqr_leaf"}, {"type": "null"}],
                        "default": None,
                        "nullable": True,
                        "title": "Address",
                    },
                },
                "required": ["event_id", "name", "participants"],
                "title": "Event",
                "type": "object",
            },
            "M2mWithO2oPk_2mchlzz2uvulfdyq_leaf": {
                "additionalProperties": False,
                "properties": {
                    "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
                    "name": {"maxLength": 64, "title": "Name", "type": "string"},
                },
                "required": ["id", "name"],
                "title": "M2mWithO2oPk",
                "type": "object",
            },
            "Reporter_e6s2ho4kgvyzgddv_leaf": {
                "additionalProperties": False,
                "description": "Whom is assigned as the reporter",
                "properties": {
                    "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
                    "name": {"title": "Name", "type": "string"},
                },
                "required": ["id", "name"],
                "title": "Reporter",
                "type": "object",
            },
            "Team_km6q3hpwepuhgvbj_leaf": {
                "additionalProperties": False,
                "description": "Team that is a playing",
                "properties": {
                    "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
                    "name": {"title": "Name", "type": "string"},
                    "alias": {
                        "anyOf": [
                            {"maximum": 2147483647, "minimum": -2147483648, "type": "integer"},
                            {"type": "null"},
                        ],
                        "default": None,
                        "nullable": True,
                        "title": "Alias",
                    },
                },
                "required": ["id", "name"],
                "title": "Team",
                "type": "object",
            },
        },
        "additionalProperties": False,
        "properties": {
            "id": {"maximum": 32767, "minimum": -32768, "title": "Id", "type": "integer"},
            "name": {"maxLength": 255, "title": "Name", "type": "string"},
            "desc": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
                "nullable": True,
                "title": "Desc",
            },
            "created": {
                "default": None,
                "format": "date-time",
                "readOnly": True,
                "title": "Created",
                "type": "string",
            },
            "events": {
                "description": "What tournaments is a happenin'",
                "items": {"$ref": "#/$defs/Event_czm5a53hsvvseysl_leaf"},
                "title": "Events",
                "type": "array",
            },
        },
        "required": ["id", "name", "events"],
        "title": "Tournament",
        "type": "object",
    }


@pytest.mark.asyncio
async def test_team_schema(db, pydantic_setup):
    Team_Pydantic = pydantic_setup["Team_Pydantic"]
    assert Team_Pydantic.model_json_schema() == {
        "$defs": {
            "Address_pu7okpsgxmp32yqr_leaf": {
                "additionalProperties": False,
                "properties": {
                    "city": {"maxLength": 64, "title": "City", "type": "string"},
                    "street": {"maxLength": 128, "title": "Street", "type": "string"},
                    "event_id": {
                        "maximum": 9223372036854775807,
                        "minimum": -9223372036854775808,
                        "title": "Event Id",
                        "type": "integer",
                    },
                    "m2mwitho2opks": {
                        "items": {"$ref": "#/$defs/M2mWithO2oPk_2mchlzz2uvulfdyq_leaf"},
                        "title": "M2Mwitho2Opks",
                        "type": "array",
                    },
                },
                "required": ["city", "street", "event_id", "m2mwitho2opks"],
                "title": "Address",
                "type": "object",
            },
            "Event_aqkhmrddzzhfba5u_leaf": {
                "additionalProperties": False,
                "description": "Events on the calendar",
                "properties": {
                    "event_id": {
                        "maximum": 9223372036854775807,
                        "minimum": -9223372036854775808,
                        "title": "Event Id",
                        "type": "integer",
                    },
                    "name": {"description": "The name", "title": "Name", "type": "string"},
                    "tournament": {
                        "$ref": "#/$defs/Tournament_ln4hy2ux7h6l3w43_leaf",
                        "description": "What tournaments is a happenin'",
                    },
                    "reporter": {
                        "anyOf": [{"$ref": "#/$defs/Reporter_e6s2ho4kgvyzgddv_leaf"}, {"type": "null"}],
                        "default": None,
                        "nullable": True,
                        "title": "Reporter",
                    },
                    "modified": {
                        "default": None,
                        "format": "date-time",
                        "readOnly": True,
                        "title": "Modified",
                        "type": "string",
                    },
                    "token": {"title": "Token", "type": "string"},
                    "alias": {
                        "anyOf": [
                            {"maximum": 2147483647, "minimum": -2147483648, "type": "integer"},
                            {"type": "null"},
                        ],
                        "default": None,
                        "nullable": True,
                        "title": "Alias",
                    },
                    "address": {
                        "anyOf": [{"$ref": "#/$defs/Address_pu7okpsgxmp32yqr_leaf"}, {"type": "null"}],
                        "default": None,
                        "nullable": True,
                        "title": "Address",
                    },
                },
                "required": ["event_id", "name", "tournament"],
                "title": "Event",
                "type": "object",
            },
            "M2mWithO2oPk_2mchlzz2uvulfdyq_leaf": {
                "additionalProperties": False,
                "properties": {
                    "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
                    "name": {"maxLength": 64, "title": "Name", "type": "string"},
                },
                "required": ["id", "name"],
                "title": "M2mWithO2oPk",
                "type": "object",
            },
            "Reporter_e6s2ho4kgvyzgddv_leaf": {
                "additionalProperties": False,
                "description": "Whom is assigned as the reporter",
                "properties": {
                    "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
                    "name": {"title": "Name", "type": "string"},
                },
                "required": ["id", "name"],
                "title": "Reporter",
                "type": "object",
            },
            "Tournament_ln4hy2ux7h6l3w43_leaf": {
                "additionalProperties": False,
                "properties": {
                    "id": {"maximum": 32767, "minimum": -32768, "title": "Id", "type": "integer"},
                    "name": {"maxLength": 255, "title": "Name", "type": "string"},
                    "desc": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "default": None,
                        "nullable": True,
                        "title": "Desc",
                    },
                    "created": {
                        "default": None,
                        "format": "date-time",
                        "readOnly": True,
                        "title": "Created",
                        "type": "string",
                    },
                },
                "required": ["id", "name"],
                "title": "Tournament",
                "type": "object",
            },
        },
        "additionalProperties": False,
        "description": "Team that is a playing",
        "properties": {
            "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
            "name": {"title": "Name", "type": "string"},
            "alias": {
                "anyOf": [{"maximum": 2147483647, "minimum": -2147483648, "type": "integer"}, {"type": "null"}],
                "default": None,
                "nullable": True,
                "title": "Alias",
            },
            "events": {"items": {"$ref": "#/$defs/Event_aqkhmrddzzhfba5u_leaf"}, "title": "Events", "type": "array"},
        },
        "required": ["id", "name", "events"],
        "title": "Team",
        "type": "object",
    }


@pytest.mark.asyncio
async def test_eventlist(db, pydantic_setup):
    Event_Pydantic_List = pydantic_setup["Event_Pydantic_List"]
    event = pydantic_setup["event"]
    event2 = pydantic_setup["event2"]
    tournament = pydantic_setup["tournament"]
    reporter = pydantic_setup["reporter"]
    team1 = pydantic_setup["team1"]
    team2 = pydantic_setup["team2"]
    address = pydantic_setup["address"]

    eventlp = await Event_Pydantic_List.from_queryset(Event.objects.all())
    eventldict = eventlp.model_dump()

    # Remove timestamps
    del eventldict[0]["modified"]
    del eventldict[0]["tournament"]["created"]
    del eventldict[1]["modified"]
    del eventldict[1]["tournament"]["created"]

    assert eventldict == [
        {
            "event_id": event.event_id,
            "name": "Test",
            # "modified": "2020-01-28T10:43:50.901562",
            "token": event.token,
            "alias": None,
            "tournament": {
                "id": tournament.id,
                "name": "New Tournament",
                "desc": None,
                # "created": "2020-01-28T10:43:50.900664"
            },
            "reporter": {"id": reporter.id, "name": "The Reporter"},
            "participants": [
                {"id": team1.id, "name": "Onesies", "alias": None},
                {"id": team2.id, "name": "T-Shirts", "alias": None},
            ],
            "address": {
                "event_id": address.pk,
                "city": "Santa Monica",
                "m2mwitho2opks": [],
                "street": "Ocean",
            },
        },
        {
            "event_id": event2.event_id,
            "name": "Test2",
            # "modified": "2020-01-28T10:43:50.901562",
            "token": event2.token,
            "alias": None,
            "tournament": {
                "id": tournament.id,
                "name": "New Tournament",
                "desc": None,
                # "created": "2020-01-28T10:43:50.900664"
            },
            "reporter": None,
            "participants": [
                {"id": team1.id, "name": "Onesies", "alias": None},
                {"id": team2.id, "name": "T-Shirts", "alias": None},
            ],
            "address": None,
        },
    ]


@pytest.mark.asyncio
async def test_event(db, pydantic_setup):
    Event_Pydantic = pydantic_setup["Event_Pydantic"]
    event = pydantic_setup["event"]
    tournament = pydantic_setup["tournament"]
    reporter = pydantic_setup["reporter"]
    team1 = pydantic_setup["team1"]
    team2 = pydantic_setup["team2"]
    address = pydantic_setup["address"]

    eventp = await Event_Pydantic.from_hare_orm(await Event.objects.get(name="Test"))
    eventdict = eventp.model_dump()

    # Remove timestamps
    del eventdict["modified"]
    del eventdict["tournament"]["created"]

    assert eventdict == {
        "event_id": event.event_id,
        "name": "Test",
        # "modified": "2020-01-28T10:43:50.901562",
        "token": event.token,
        "alias": None,
        "tournament": {
            "id": tournament.id,
            "name": "New Tournament",
            "desc": None,
            # "created": "2020-01-28T10:43:50.900664"
        },
        "reporter": {"id": reporter.id, "name": "The Reporter"},
        "participants": [
            {"id": team1.id, "name": "Onesies", "alias": None},
            {"id": team2.id, "name": "T-Shirts", "alias": None},
        ],
        "address": {
            "event_id": address.pk,
            "city": "Santa Monica",
            "m2mwitho2opks": [],
            "street": "Ocean",
        },
    }


@pytest.mark.asyncio
async def test_address(db, pydantic_setup):
    Address_Pydantic = pydantic_setup["Address_Pydantic"]
    event = pydantic_setup["event"]
    tournament = pydantic_setup["tournament"]
    reporter = pydantic_setup["reporter"]
    team1 = pydantic_setup["team1"]
    team2 = pydantic_setup["team2"]
    address = pydantic_setup["address"]

    addressp = await Address_Pydantic.from_hare_orm(await Address.objects.get(street="Ocean"))
    addressdict = addressp.model_dump()

    # Remove timestamps
    del addressdict["event"]["tournament"]["created"]
    del addressdict["event"]["modified"]

    assert addressdict == {
        "city": "Santa Monica",
        "street": "Ocean",
        "event": {
            "event_id": event.event_id,
            "name": "Test",
            "tournament": {
                "id": tournament.id,
                "name": "New Tournament",
                "desc": None,
            },
            "reporter": {"id": reporter.id, "name": "The Reporter"},
            "participants": [
                {"id": team1.id, "name": "Onesies", "alias": None},
                {"id": team2.id, "name": "T-Shirts", "alias": None},
            ],
            "token": event.token,
            "alias": None,
        },
        "event_id": address.event_id,
        "m2mwitho2opks": [],
    }


@pytest.mark.asyncio
async def test_tournament(db, pydantic_setup):
    Tournament_Pydantic = pydantic_setup["Tournament_Pydantic"]
    event = pydantic_setup["event"]
    event2 = pydantic_setup["event2"]
    tournament = pydantic_setup["tournament"]
    reporter = pydantic_setup["reporter"]
    team1 = pydantic_setup["team1"]
    team2 = pydantic_setup["team2"]
    address = pydantic_setup["address"]

    tournamentp = await Tournament_Pydantic.from_hare_orm(await Tournament.objects.all().first())
    tournamentdict = tournamentp.model_dump()

    # Remove timestamps
    del tournamentdict["events"][0]["modified"]
    del tournamentdict["events"][1]["modified"]
    del tournamentdict["created"]

    assert tournamentdict == {
        "id": tournament.id,
        "name": "New Tournament",
        "desc": None,
        # "created": "2020-01-28T19:41:38.059617",
        "events": [
            {
                "event_id": event.event_id,
                "name": "Test",
                # "modified": "2020-01-28T19:41:38.060070",
                "token": event.token,
                "alias": None,
                "reporter": {"id": reporter.id, "name": "The Reporter"},
                "participants": [
                    {"id": team1.id, "name": "Onesies", "alias": None},
                    {"id": team2.id, "name": "T-Shirts", "alias": None},
                ],
                "address": {
                    "event_id": address.pk,
                    "city": "Santa Monica",
                    "m2mwitho2opks": [],
                    "street": "Ocean",
                },
            },
            {
                "event_id": event2.event_id,
                "name": "Test2",
                # "modified": "2020-01-28T19:41:38.060070",
                "token": event2.token,
                "alias": None,
                "reporter": None,
                "participants": [
                    {"id": team1.id, "name": "Onesies", "alias": None},
                    {"id": team2.id, "name": "T-Shirts", "alias": None},
                ],
                "address": None,
            },
        ],
    }


@pytest.mark.asyncio
async def test_team(db, pydantic_setup):
    Team_Pydantic = pydantic_setup["Team_Pydantic"]
    event = pydantic_setup["event"]
    event2 = pydantic_setup["event2"]
    tournament = pydantic_setup["tournament"]
    reporter = pydantic_setup["reporter"]
    team1 = pydantic_setup["team1"]
    address = pydantic_setup["address"]

    teamp = await Team_Pydantic.from_hare_orm(await Team.objects.get(id=team1.id))
    teamdict = teamp.model_dump()

    # Remove timestamps
    del teamdict["events"][0]["modified"]
    del teamdict["events"][0]["tournament"]["created"]
    del teamdict["events"][1]["modified"]
    del teamdict["events"][1]["tournament"]["created"]

    assert teamdict == {
        "id": team1.id,
        "name": "Onesies",
        "alias": None,
        "events": [
            {
                "event_id": event.event_id,
                "name": "Test",
                # "modified": "2020-01-28T19:47:03.334077",
                "token": event.token,
                "alias": None,
                "tournament": {
                    "id": tournament.id,
                    "name": "New Tournament",
                    "desc": None,
                    # "created": "2020-01-28T19:41:38.059617",
                },
                "reporter": {"id": reporter.id, "name": "The Reporter"},
                "address": {
                    "event_id": address.pk,
                    "city": "Santa Monica",
                    "m2mwitho2opks": [],
                    "street": "Ocean",
                },
            },
            {
                "event_id": event2.event_id,
                "name": "Test2",
                # "modified": "2020-01-28T19:47:03.334077",
                "token": event2.token,
                "alias": None,
                "tournament": {
                    "id": tournament.id,
                    "name": "New Tournament",
                    "desc": None,
                    # "created": "2020-01-28T19:41:38.059617",
                },
                "reporter": None,
                "address": None,
            },
        ],
    }


@pytest.mark.asyncio
async def test_event_named(db, pydantic_setup):
    Event_Named = pydantic_model_creator(Event, name="Foo")
    schema = Event_Named.model_json_schema()
    assert schema["title"] == "Foo"
    assert set(schema["properties"].keys()) == {
        "address",
        "alias",
        "event_id",
        "modified",
        "name",
        "participants",
        "reporter",
        "token",
        "tournament",
    }


@pytest.mark.asyncio
async def test_event_sorted(db, pydantic_setup):
    Event_Named = pydantic_model_creator(Event, sort_alphabetically=True)
    schema = Event_Named.model_json_schema()
    assert list(schema["properties"].keys()) == [
        "address",
        "alias",
        "event_id",
        "modified",
        "name",
        "participants",
        "reporter",
        "token",
        "tournament",
    ]


@pytest.mark.asyncio
async def test_event_unsorted(db, pydantic_setup):
    Event_Named = pydantic_model_creator(Event, sort_alphabetically=False)
    schema = Event_Named.model_json_schema()
    assert list(schema["properties"].keys()) == [
        "event_id",
        "name",
        "tournament",
        "reporter",
        "participants",
        "modified",
        "token",
        "alias",
        "address",
    ]


@pytest.mark.asyncio
async def test_json_field(db):
    json_field_0 = await JSONFields.objects.create(data={"a": 1})
    json_field_1 = await JSONFields.objects.create(data=[{"a": 1, "b": 2}])
    json_field_0_get = await JSONFields.objects.get(pk=json_field_0.pk)
    json_field_1_get = await JSONFields.objects.get(pk=json_field_1.pk)

    creator = pydantic_model_creator(JSONFields)
    ret0 = creator.model_validate(json_field_0_get).model_dump()
    assert ret0 == {
        "id": json_field_0.pk,
        "data": {"a": 1},
        "data_null": None,
        "data_default": {"a": 1},
        "data_validate": None,
        "data_pydantic": json_pydantic_default.model_dump(),
        "data_decimal": None,
        "data_index": None,
    }
    ret1 = creator.model_validate(json_field_1_get).model_dump()
    assert ret1 == {
        "id": json_field_1.pk,
        "data": [{"a": 1, "b": 2}],
        "data_null": None,
        "data_default": {"a": 1},
        "data_validate": None,
        "data_pydantic": json_pydantic_default.model_dump(),
        "data_decimal": None,
        "data_index": None,
    }


def test_json_field_without_generics_is_typed_any():
    """`data = fields.JSONField()` (no generic param - accepts either a dict or a list at
    runtime) used to fall through to _process_data_field()'s generic get_python_type() path
    instead of being typed Any - the dedicated `isinstance(field, JSONField)` branch meant to
    catch exactly this case compared field.field_type (the field's own PYTHON value type, e.g.
    dict/list - never the JSONField class itself) against the JSONField class object, which can
    never be true."""
    creator = pydantic_model_creator(JSONFields)
    assert creator.model_fields["data"].annotation is Any


def test_json_field_nullable_without_default_is_optional():
    """JSONField(null=True) with no explicit default (data_null/data_decimal/data_index) used to
    stay required - the JSONField short-circuit in _process_normal_field returns `Any` straight
    away, never reaching the json_schema_extra["nullable"] flag _process_orm_field's own
    required-ness check relies on, unlike every other nullable field type."""
    creator = pydantic_model_creator(JSONFields)

    for field_name in ("data_null", "data_decimal", "data_index"):
        assert creator.model_fields[field_name].is_required() is False
        assert creator.model_fields[field_name].annotation is Any


def test_override_default_model_config_by_config_class(db):
    """Pydantic meta's config_class should be able to override default config."""
    # Save original value to restore after test
    original_value = CamelCaseAliasPerson.PydanticMeta.model_config.get("from_attributes")
    try:
        # Set class pydantic config's from_attributes to False
        CamelCaseAliasPerson.PydanticMeta.model_config["from_attributes"] = False

        ModelPydantic = pydantic_model_creator(CamelCaseAliasPerson, name="AutoAliasPersonOverriddenORMMode")

        assert ModelPydantic.model_config["from_attributes"] is False
    finally:
        # Restore original value to avoid polluting other tests
        if original_value is None:
            CamelCaseAliasPerson.PydanticMeta.model_config.pop("from_attributes", None)
        else:
            CamelCaseAliasPerson.PydanticMeta.model_config["from_attributes"] = original_value


def test_override_meta_pydantic_config_by_model_creator(db):
    model_config = ConfigDict(title="Another title!")

    ModelPydantic = pydantic_model_creator(
        CamelCaseAliasPerson,
        model_config=model_config,
        name="AutoAliasPersonModelCreatorConfig",
    )

    assert model_config["title"] == ModelPydantic.model_config["title"]


def test_config_classes_merge_all_configs(db):
    """Model creator should merge all 3 configs.

    - It merges (Default, Meta's config_class and creator's config_class) together.
    """
    model_config = ConfigDict(str_min_length=3)

    ModelPydantic = pydantic_model_creator(
        CamelCaseAliasPerson, name="AutoAliasPersonMinLength", model_config=model_config
    )

    # Should set min_anystr_length from pydantic_model_creator's config
    assert ModelPydantic.model_config["str_min_length"] == model_config["str_min_length"]
    # Should set title from model PydanticMeta's config
    assert ModelPydantic.model_config["title"] == CamelCaseAliasPerson.PydanticMeta.model_config["title"]
    # Should set orm_mode from base pydantic model configuration
    assert ModelPydantic.model_config["from_attributes"] == PydanticModel.model_config["from_attributes"]


def test_model_config_included_in_cache_key(db):
    """PydanticModelCreator._hash didn't account for model_config - two default-named calls for
    the same base Model, differing only in model_config, hashed identically and silently reused
    the FIRST call's cached class (with the first call's model_config), ignoring the second
    call's model_config entirely."""
    config_a = pydantic_model_creator(Address, model_config=ConfigDict(title="CacheKeyConfigA"))
    config_b = pydantic_model_creator(Address, model_config=ConfigDict(title="CacheKeyConfigB"))

    assert config_a.model_config["title"] == "CacheKeyConfigA"
    assert config_b.model_config["title"] == "CacheKeyConfigB"
    assert config_a is not config_b


def test_model_config_only_diff_gets_a_distinct_class_name(db):
    """PydanticModelCreator._is_default didn't account for model_config/validators/module (unlike
    _hash, which already folded all three in) - two calls differing ONLY in one of these produced
    two genuinely distinct classes (correctly, per _hash/MODEL_INDEX) but get_name() saw both as
    "default" and returned the bare fqname for both, with no disambiguating hash suffix - a
    silent __name__ collision between two non-identical schemas (the type of thing an OpenAPI/
    FastAPI schema registry would render as one clobbering the other)."""
    PydanticModelCreator.MODEL_INDEX.clear()
    base = pydantic_model_creator(Address)
    with_config = pydantic_model_creator(Address, model_config=ConfigDict(str_min_length=5))

    assert base is not with_config
    assert base.__name__ != with_config.__name__

    PydanticModelCreator.MODEL_INDEX.clear()
    base_again = pydantic_model_creator(Address)
    with_validators = pydantic_model_creator(Address, validators={"street": lambda v: v})

    assert base_again is not with_validators
    assert base_again.__name__ != with_validators.__name__


def test_computed_field_order_does_not_affect_cache_key(db):
    """A computed=(...) tuple is the same logical config regardless of element order - two calls
    differing only in that order should hit the MODEL_INDEX cache and return the same class,
    not silently build (and hash-cache) two distinct model classes for one config."""
    a = pydantic_model_creator(Employee, computed=("name_length", "team_size"))
    b = pydantic_model_creator(Employee, computed=("team_size", "name_length"))

    assert a is b

    # A genuinely different config (sort_alphabetically) must still miss the cache - the fix
    # must not broaden order-insensitivity beyond the computed tuple specifically.
    c = pydantic_model_creator(Employee, computed=("name_length", "team_size"), sort_alphabetically=True)
    assert a is not c


def test_two_distinct_classes_from_the_same_factory_get_distinct_schemas(db):
    """PydanticModelCreator._hash used to be built from __module__/__qualname__ + field/relation
    NAMES only, never the source class's own identity - two DIFFERENT classes built by the same
    factory function (the shape a register_live_model()-style dynamic model builder produces,
    e.g. one shared class-building function called once per tenant/table) share the exact same
    __module__/__qualname__, so they hashed identically and the SECOND call silently reused the
    FIRST call's cached schema, including its orig_model pointing at the WRONG source class. The
    get_name() guard (hashed_model.__name__ == self._name) never catches this, since _name is
    itself built from this same hash when no explicit name= is given."""

    def build_widget(table_name: str) -> type[Model]:
        class Widget(Model):
            id = fields.IntField(primary_key=True)
            name = fields.CharField(max_length=32)

            class Meta:
                table = table_name

        return Widget

    WidgetA = build_widget("pydantic_hash_collision_widget_a")
    WidgetB = build_widget("pydantic_hash_collision_widget_b")
    assert WidgetA.__module__ == WidgetB.__module__
    assert WidgetA.__qualname__ == WidgetB.__qualname__

    SchemaA = pydantic_model_creator(WidgetA)
    SchemaB = pydantic_model_creator(WidgetB)

    assert SchemaA is not SchemaB
    assert SchemaB.model_config["orig_model"] is WidgetB


def test_model_index_evicts_oldest_entry_past_max_size(monkeypatch):
    """MODEL_INDEX used to be a plain dict - every distinct pydantic_model_creator() config ever
    called added a permanent entry, growing unbounded for the life of the process. Now a bounded
    LRU: shrink the limit to 2 for this test and confirm the oldest of 3 distinct configs gets
    evicted, while a config touched again (move_to_end on hit) survives past ones that would
    otherwise have been older."""
    monkeypatch.setattr(PydanticModelCreator.MODEL_INDEX, "max_size", 2)
    PydanticModelCreator.MODEL_INDEX.clear()

    # IntFields (no relational fields) - pydantic_model_creator() recurses into related models'
    # own configurations too (Address has a relation, which would add its own extra MODEL_INDEX
    # entries and make the eviction order unpredictable against a manually shrunk max size).
    # `exclude=` (not `name=`, which the hash deliberately ignores - see MODEL_INDEX's own
    # docstring) is what actually varies the hash here, so each of these 3 calls gets a genuinely
    # distinct MODEL_INDEX entry - `name=` just keeps each one identifiable in the assertions.
    first = pydantic_model_creator(IntFields, name="EvictFirst", exclude=("intnum",))
    second = pydantic_model_creator(IntFields, name="EvictSecond", exclude=("intnum_null",))
    # Touching `first` again moves it to the end - it must survive the next insertion instead of
    # "EvictSecond" (the real least-recently-used entry at that point).
    pydantic_model_creator(IntFields, name="EvictFirst", exclude=("intnum",))
    pydantic_model_creator(IntFields, name="EvictThird", exclude=("intnum", "intnum_null"))

    assert len(PydanticModelCreator.MODEL_INDEX) == 2
    cached_names = {model.__name__ for model in PydanticModelCreator.MODEL_INDEX.values()}
    assert cached_names == {"EvictFirst", "EvictThird"}
    # EvictFirst survived (it was touched more recently) - still the same cached class. Checked
    # BEFORE touching EvictSecond below - re-requesting an evicted entry re-populates the cache
    # and would itself evict EvictFirst again (2-slot cache), so order matters here.
    assert first is pydantic_model_creator(IntFields, name="EvictFirst", exclude=("intnum",))
    # EvictSecond was evicted - re-requesting it builds a genuinely new class, not the old one.
    assert second is not pydantic_model_creator(IntFields, name="EvictSecond", exclude=("intnum_null",))


def test_pydantic_queryset_creator_caches_identical_calls(db):
    """pydantic_queryset_creator used to build a brand new list-wrapper class via create_model()
    on every call, never caching it - unlike pydantic_model_creator, whose submodel it wraps IS
    cached via MODEL_INDEX. Two calls with identical arguments must now return the exact same
    class instead of two structurally-identical but distinct ones."""
    PydanticModelCreator.QUERYSET_MODEL_INDEX.clear()
    first = pydantic_queryset_creator(Address)
    second = pydantic_queryset_creator(Address)

    assert first is second
    assert len(PydanticModelCreator.QUERYSET_MODEL_INDEX) == 1


def test_pydantic_queryset_creator_distinct_configs_get_distinct_classes(db):
    """A different name=/exclude=/include= must still miss the cache - the fix must not make
    pydantic_queryset_creator() collapse genuinely different configurations into one class."""
    PydanticModelCreator.QUERYSET_MODEL_INDEX.clear()
    base = pydantic_queryset_creator(Address)
    with_name = pydantic_queryset_creator(Address, name="AddressCustomList")
    with_exclude = pydantic_queryset_creator(Address, name="AddressExcludeList", exclude=("street",))
    with_include = pydantic_queryset_creator(Address, name="AddressIncludeList", include=("street",))

    assert len({id(base), id(with_name), id(with_exclude), id(with_include)}) == 4


def test_queryset_model_index_evicts_oldest_entry_past_max_size(monkeypatch):
    """QUERYSET_MODEL_INDEX is a bounded LRU cache, matching MODEL_INDEX's own pattern - shrink
    the limit to 2 and confirm the oldest of 3 distinct configs gets evicted, while a config
    touched again (move_to_end on hit) survives past ones that would otherwise have been older."""
    monkeypatch.setattr(PydanticModelCreator.QUERYSET_MODEL_INDEX, "max_size", 2)
    PydanticModelCreator.QUERYSET_MODEL_INDEX.clear()

    first = pydantic_queryset_creator(IntFields, name="EvictFirstList", exclude=("intnum",))
    second = pydantic_queryset_creator(IntFields, name="EvictSecondList", exclude=("intnum_null",))
    # Touching `first` again moves it to the end - it must survive the next insertion instead of
    # "EvictSecondList" (the real least-recently-used entry at that point).
    pydantic_queryset_creator(IntFields, name="EvictFirstList", exclude=("intnum",))
    pydantic_queryset_creator(IntFields, name="EvictThirdList", exclude=("intnum", "intnum_null"))

    assert len(PydanticModelCreator.QUERYSET_MODEL_INDEX) == 2
    cached_names = {model.__name__ for model in PydanticModelCreator.QUERYSET_MODEL_INDEX.values()}
    assert cached_names == {"EvictFirstList", "EvictThirdList"}
    assert first is pydantic_queryset_creator(IntFields, name="EvictFirstList", exclude=("intnum",))
    assert second is not pydantic_queryset_creator(IntFields, name="EvictSecondList", exclude=("intnum_null",))


def test_exclude_readonly(db):
    ModelPydantic = pydantic_model_creator(Event, exclude_readonly=True)

    assert "modified" not in ModelPydantic.model_json_schema()["properties"]


def test_exclude_readonly_excludes_a_non_pk_generated_field():
    """exclude_readonly=True only ever excluded the PK (via _initialize_field_map never adding
    it) and a DateTimeField with auto_now/auto_now_add (via the readOnly json_schema_extra
    constraint DateTimeField alone sets) - any OTHER field explicitly marked generated=True
    (e.g. a DB computed column) leaked straight through into the "create"-shaped schema
    exclude_readonly is meant to produce."""

    class GeneratedFieldModel(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=50)
        computed_total = fields.IntField(generated=True)

    ModelPydantic = pydantic_model_creator(GeneratedFieldModel, exclude_readonly=True, name="GeneratedFieldCreate")

    assert "computed_total" not in ModelPydantic.model_fields
    assert "name" in ModelPydantic.model_fields


def test_exclude_readonly_keeps_required_forward_relations(db):
    """exclude_readonly=True builds a "create" schema - it used to drop every forward relation
    category-wide (FK/O2O/M2M), including a REQUIRED FK with no null=True/default
    (Event.tournament), making it impossible to build a valid create payload for it at all. A
    forward relation is never DB-assigned, so it must stay in the schema like any other field the
    caller has to supply - a nullable FK (Event.reporter) is still included too, just optional."""
    EventCreate = pydantic_model_creator(Event, exclude_readonly=True, name="EventCreateWithRelations")

    assert "tournament" in EventCreate.model_fields
    assert EventCreate.model_fields["tournament"].is_required() is True
    assert "reporter" in EventCreate.model_fields
    assert EventCreate.model_fields["reporter"].is_required() is False
    assert "participants" in EventCreate.model_fields


def test_array_field_annotation_keeps_element_type():
    """ArrayField.field_type is plain, unparameterized `list` (set by FieldMeta from the
    second base class of ArrayField(Field, list)) with no information about what's actually
    inside - without an explicit Python type annotation on the model attribute (which real
    ArrayField usage in this codebase never adds), the generated pydantic field collapsed to
    bare `list` (JSON schema `items: {}`, i.e. Any) instead of list[str]."""
    from hare.fields import ArrayField

    class ArrayFieldPydanticModel(Model):
        id = fields.IntField(primary_key=True)
        tags = ArrayField(base_field=fields.CharField(max_length=50))

    ModelPydantic = pydantic_model_creator(ArrayFieldPydanticModel, name="ArrayFieldPydanticModel")

    assert ModelPydantic.model_fields["tags"].annotation == list[str]
    assert ModelPydantic.model_json_schema()["properties"]["tags"]["items"] == {"type": "string"}


def test_tenant_field_is_not_required_in_generated_schemas():
    """Meta.tenant_field is silently filled in from Tenancy.current.get() by save()/create()/
    bulk_create() when the caller leaves it unset (see Model.save()'s own tenant auto-fill) - a
    typical tenant_field has none of the signals (default=/db_default/generated) required-ness
    is otherwise computed from, so it used to be marked required in both the read schema and an
    exclude_readonly=True "create" schema, forcing every caller to supply the tenant id
    explicitly and defeating the whole point of the auto-fill design."""

    class TenantFieldPydanticModel(Model):
        id = fields.IntField(primary_key=True)
        company_id = fields.IntField()

        class Meta:
            tenant_field = "company_id"

    ReadSchema = pydantic_model_creator(TenantFieldPydanticModel, name="TenantFieldPydanticModelRead")
    CreateSchema = pydantic_model_creator(
        TenantFieldPydanticModel, exclude_readonly=True, name="TenantFieldPydanticModelCreate"
    )

    assert ReadSchema.model_fields["company_id"].is_required() is False
    assert CreateSchema.model_fields["company_id"].is_required() is False


def test_vector_field_annotation_keeps_element_type():
    """VectorField.field_type is plain, unparameterized `list` (same root cause as ArrayField
    above) - the generated pydantic field collapsed to bare `list` (accepting a list of anything -
    ints, strings, nested lists) instead of list[float] for an embedding column."""
    from hare.vectors import VectorField

    class VectorFieldPydanticModel(Model):
        id = fields.IntField(primary_key=True)
        embedding = VectorField(dimensions=3)

    ModelPydantic = pydantic_model_creator(VectorFieldPydanticModel, name="VectorFieldPydanticModel")

    assert ModelPydantic.model_fields["embedding"].annotation == list[float]
    assert ModelPydantic.model_json_schema()["properties"]["embedding"]["items"] == {"type": "number"}


def test_postgis_field_annotation_keeps_element_type():
    """PostGISField.field_type is plain, unparameterized `tuple` (same root cause as VectorField/
    ArrayField above) - the generated pydantic field collapsed to bare `tuple` (accepting any
    array of anything) instead of tuple[float, float] for a (latitude, longitude) column."""
    from hare.dialects.postgresql.fields.postgis_field import PostGISField

    class PostGISFieldPydanticModel(Model):
        id = fields.IntField(primary_key=True)
        location = PostGISField()

    ModelPydantic = pydantic_model_creator(PostGISFieldPydanticModel, name="PostGISFieldPydanticModel")

    assert ModelPydantic.model_fields["location"].annotation == tuple[float, float]


def test_geometry_field_is_a_geo_json_geometry():
    from hare.gis import Point, PointField, Polygon

    class GeometryFieldPydanticModel(Model):
        id = fields.IntField(primary_key=True)
        location = PointField()

    ModelPydantic = pydantic_model_creator(GeometryFieldPydanticModel, name="GeometryFieldPydanticModel")

    model = ModelPydantic.model_validate({"id": 1, "location": {"type": "Point", "coordinates": [1, 2]}})
    assert model.location == Point(1, 2)
    assert ModelPydantic.model_validate({"id": 1, "location": "SRID=4326;POINT(1 2)"}).location == Point(
        1, 2, srid=4326
    )
    assert model.model_dump(mode="json")["location"] == {"type": "Point", "coordinates": [1.0, 2.0]}
    assert ModelPydantic.model_json_schema()["properties"]["location"]["description"] == "A GeoJSON geometry"
    with pytest.raises(ValidationError):
        ModelPydantic.model_validate({"id": 1, "location": "POINT(1)"})
    assert isinstance(Polygon.get_validated_geometry("POLYGON((0 0,1 0,1 1,0 0))"), Polygon)


# Fixtures for TestPydanticCycle
@pytest_asyncio.fixture
async def pydantic_cycle_setup(db):
    """Setup for pydantic cycle tests with employee hierarchy."""
    Employee_Pydantic = pydantic_model_creator(Employee)

    root = await Employee.objects.create(name="Root")
    loose = await Employee.objects.create(name="Loose")
    _1 = await Employee.objects.create(name="1. First H1", manager=root)
    _2 = await Employee.objects.create(name="2. Second H1", manager=root)
    _1_1 = await Employee.objects.create(name="1.1. First H2", manager=_1)
    _1_1_1 = await Employee.objects.create(name="1.1.1. First H3", manager=_1_1)
    _2_1 = await Employee.objects.create(name="2.1. Second H2", manager=_2)
    _2_2 = await Employee.objects.create(name="2.2. Third H2", manager=_2)

    await _1.talks_to.add(_2, _1_1_1, loose)
    await _2_1.gets_talked_to.add(_2_2, _1_1, loose)

    return {
        "Employee_Pydantic": Employee_Pydantic,
        "root": root,
        "loose": loose,
        "_1": _1,
        "_2": _2,
        "_1_1": _1_1,
        "_1_1_1": _1_1_1,
        "_2_1": _2_1,
        "_2_2": _2_2,
    }


@pytest.mark.asyncio
async def test_cycle_schema(db, pydantic_cycle_setup):
    Employee_Pydantic = pydantic_cycle_setup["Employee_Pydantic"]
    assert Employee_Pydantic.model_json_schema() == {
        "$defs": {
            "Employee_6fp7dmib3ccgfhcl_leaf": {
                "additionalProperties": False,
                "properties": {
                    "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
                    "name": {"maxLength": 50, "title": "Name", "type": "string"},
                    "talks_to": {
                        "items": {"$ref": "#/$defs/Employee_pvleucxo45yvrlxg_leaf"},
                        "title": "Talks To",
                        "type": "array",
                    },
                    "manager_id": {
                        "anyOf": [
                            {"maximum": 2147483647, "minimum": -2147483648, "type": "integer"},
                            {"type": "null"},
                        ],
                        "default": None,
                        "nullable": True,
                        "title": "Manager Id",
                    },
                    "team_members": {
                        "items": {"$ref": "#/$defs/Employee_pvleucxo45yvrlxg_leaf"},
                        "title": "Team Members",
                        "type": "array",
                    },
                },
                "required": ["id", "name", "talks_to", "team_members"],
                "title": "Employee",
                "type": "object",
            },
            "Employee_pvleucxo45yvrlxg_leaf": {
                "additionalProperties": False,
                "properties": {
                    "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
                    "name": {"maxLength": 50, "title": "Name", "type": "string"},
                    "manager_id": {
                        "anyOf": [
                            {"maximum": 2147483647, "minimum": -2147483648, "type": "integer"},
                            {"type": "null"},
                        ],
                        "default": None,
                        "nullable": True,
                        "title": "Manager Id",
                    },
                },
                "required": ["id", "name"],
                "title": "Employee",
                "type": "object",
            },
        },
        "additionalProperties": False,
        "properties": {
            "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
            "name": {"maxLength": 50, "title": "Name", "type": "string"},
            "talks_to": {
                "items": {"$ref": "#/$defs/Employee_6fp7dmib3ccgfhcl_leaf"},
                "title": "Talks To",
                "type": "array",
            },
            "manager_id": {
                "anyOf": [{"maximum": 2147483647, "minimum": -2147483648, "type": "integer"}, {"type": "null"}],
                "default": None,
                "nullable": True,
                "title": "Manager Id",
            },
            "team_members": {
                "items": {"$ref": "#/$defs/Employee_6fp7dmib3ccgfhcl_leaf"},
                "title": "Team Members",
                "type": "array",
            },
        },
        "required": ["id", "name", "talks_to", "team_members"],
        "title": "Employee",
        "type": "object",
    }


@pytest.mark.asyncio
async def test_cycle_serialisation(db, pydantic_cycle_setup):
    Employee_Pydantic = pydantic_cycle_setup["Employee_Pydantic"]
    root = pydantic_cycle_setup["root"]
    loose = pydantic_cycle_setup["loose"]
    _1 = pydantic_cycle_setup["_1"]
    _2 = pydantic_cycle_setup["_2"]
    _1_1 = pydantic_cycle_setup["_1_1"]
    _1_1_1 = pydantic_cycle_setup["_1_1_1"]
    _2_1 = pydantic_cycle_setup["_2_1"]
    _2_2 = pydantic_cycle_setup["_2_2"]

    empp = await Employee_Pydantic.from_hare_orm(await Employee.objects.get(name="Root"))
    empdict = empp.model_dump()

    assert empdict == {
        "id": root.id,
        "manager_id": None,
        "name": "Root",
        "talks_to": [],
        "team_members": [
            {
                "id": _1.id,
                "manager_id": root.id,
                "name": "1. First H1",
                "talks_to": [
                    {
                        "id": loose.id,
                        "manager_id": None,
                        "name": "Loose",
                        "name_length": 5,
                        "team_size": 0,
                    },
                    {
                        "id": _2.id,
                        "manager_id": root.id,
                        "name": "2. Second H1",
                        "name_length": 12,
                        "team_size": 0,
                    },
                    {
                        "id": _1_1_1.id,
                        "manager_id": _1_1.id,
                        "name": "1.1.1. First H3",
                        "name_length": 15,
                        "team_size": 0,
                    },
                ],
                "team_members": [
                    {
                        "id": _1_1.id,
                        "manager_id": _1.id,
                        "name": "1.1. First H2",
                        "name_length": 13,
                        "team_size": 0,
                    }
                ],
                "name_length": 11,
                "team_size": 1,
            },
            {
                "id": _2.id,
                "manager_id": root.id,
                "name": "2. Second H1",
                "talks_to": [],
                "team_members": [
                    {
                        "id": _2_1.id,
                        "manager_id": _2.id,
                        "name": "2.1. Second H2",
                        "name_length": 14,
                        "team_size": 0,
                    },
                    {
                        "id": _2_2.id,
                        "manager_id": _2.id,
                        "name": "2.2. Third H2",
                        "name_length": 13,
                        "team_size": 0,
                    },
                ],
                "name_length": 12,
                "team_size": 2,
            },
        ],
        "name_length": 4,
        "team_size": 2,
    }


@pytest.mark.asyncio
async def test_max_recursion_actually_limits_nesting_depth(db):
    """max_recursion must have a real, distinct effect at different values - it previously did
    nothing at all (every value behaved like the class's own hardcoded default), because the
    configured value was never threaded down into recursively-built submodels, and the
    stack-depth check itself always skipped the first recursion level.

    Builds a real 4-deep manager chain (root -> c1 -> c2 -> c3 -> c4) and confirms that
    max_recursion=1 cuts serialization off after exactly one level of nested team_members,
    while max_recursion=4 - on the very same data - reaches all four."""

    class OverrideOneLevel:
        max_recursion = 1

    class OverrideFourLevels:
        max_recursion = 4

    root = await Employee.objects.create(name="Root")
    c1 = await Employee.objects.create(name="C1", manager=root)
    c2 = await Employee.objects.create(name="C2", manager=c1)
    c3 = await Employee.objects.create(name="C3", manager=c2)
    await Employee.objects.create(name="C4", manager=c3)

    OneLevel_Pydantic = pydantic_model_creator(Employee, name="Employee_OneLevel", meta_override=OverrideOneLevel)
    FourLevels_Pydantic = pydantic_model_creator(
        Employee, name="Employee_FourLevels", meta_override=OverrideFourLevels
    )

    def team_members_nesting_depth(employee_dict: dict) -> int:
        depth = 0
        node = employee_dict
        while node.get("team_members"):
            depth += 1
            node = node["team_members"][0]
        return depth

    one_level_dict = (await OneLevel_Pydantic.from_hare_orm(await Employee.objects.get(name="Root"))).model_dump()
    four_levels_dict = (await FourLevels_Pydantic.from_hare_orm(await Employee.objects.get(name="Root"))).model_dump()

    assert team_members_nesting_depth(one_level_dict) == 1
    assert team_members_nesting_depth(four_levels_dict) == 4


# Fixtures for TestPydanticComputed
@pytest_asyncio.fixture
async def pydantic_computed_setup(db):
    """Setup for pydantic computed field tests."""
    Employee_Pydantic = pydantic_model_creator(Employee)
    employee = await Employee.objects.create(name="Some Employee")

    return {
        "Employee_Pydantic": Employee_Pydantic,
        "employee": employee,
    }


@pytest.mark.asyncio
async def test_computed_field(db, pydantic_computed_setup):
    Employee_Pydantic = pydantic_computed_setup["Employee_Pydantic"]
    employee = pydantic_computed_setup["employee"]

    employee_pyd = await Employee_Pydantic.from_hare_orm(await Employee.objects.get(name="Some Employee"))
    employee_serialised = employee_pyd.model_dump()
    assert employee_serialised.get("name_length") == employee.name_length()


@pytest.mark.asyncio
async def test_computed_field_schema(db, pydantic_computed_setup):
    Employee_Pydantic = pydantic_computed_setup["Employee_Pydantic"]
    assert Employee_Pydantic.model_json_schema(mode="serialization") == {
        "$defs": {
            "Employee_6fp7dmib3ccgfhcl_leaf": {
                "additionalProperties": False,
                "properties": {
                    "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
                    "name": {"maxLength": 50, "title": "Name", "type": "string"},
                    "talks_to": {
                        "items": {"$ref": "#/$defs/Employee_pvleucxo45yvrlxg_leaf"},
                        "title": "Talks To",
                        "type": "array",
                    },
                    "manager_id": {
                        "anyOf": [
                            {"maximum": 2147483647, "minimum": -2147483648, "type": "integer"},
                            {"type": "null"},
                        ],
                        "default": None,
                        "nullable": True,
                        "title": "Manager Id",
                    },
                    "team_members": {
                        "items": {"$ref": "#/$defs/Employee_pvleucxo45yvrlxg_leaf"},
                        "title": "Team Members",
                        "type": "array",
                    },
                    "name_length": {"description": "", "readOnly": True, "title": "Name Length", "type": "integer"},
                    "team_size": {
                        "description": (
                            "Computes team size.<br/><br/>Note that this function needs to be annotated with "
                            "a return type so that pydantic can<br/> generate a valid schema.<br/><br/>Note "
                            "that the pydantic serializer can't call async methods, but the hare helpers<br/> "
                            "pre-fetch relational data, so that it is available before serialization. So we "
                            "don't<br/> need to await the relation. We do however have to protect against the "
                            "case where no<br/> prefetching was done, hence catching and handling the<br/> "
                            "``hare.exceptions.NoValuesFetched`` exception."
                        ),
                        "readOnly": True,
                        "title": "Team Size",
                        "type": "integer",
                    },
                },
                "required": ["id", "name", "talks_to", "team_members", "name_length", "team_size"],
                "title": "Employee",
                "type": "object",
            },
            "Employee_pvleucxo45yvrlxg_leaf": {
                "additionalProperties": False,
                "properties": {
                    "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
                    "name": {"maxLength": 50, "title": "Name", "type": "string"},
                    "manager_id": {
                        "anyOf": [
                            {"maximum": 2147483647, "minimum": -2147483648, "type": "integer"},
                            {"type": "null"},
                        ],
                        "default": None,
                        "nullable": True,
                        "title": "Manager Id",
                    },
                    "name_length": {"description": "", "readOnly": True, "title": "Name Length", "type": "integer"},
                    "team_size": {
                        "description": (
                            "Computes team size.<br/><br/>Note that this function needs to be annotated with "
                            "a return type so that pydantic can<br/> generate a valid schema.<br/><br/>Note "
                            "that the pydantic serializer can't call async methods, but the hare helpers<br/> "
                            "pre-fetch relational data, so that it is available before serialization. So we "
                            "don't<br/> need to await the relation. We do however have to protect against the "
                            "case where no<br/> prefetching was done, hence catching and handling the<br/> "
                            "``hare.exceptions.NoValuesFetched`` exception."
                        ),
                        "readOnly": True,
                        "title": "Team Size",
                        "type": "integer",
                    },
                },
                "required": ["id", "name", "name_length", "team_size"],
                "title": "Employee",
                "type": "object",
            },
        },
        "additionalProperties": False,
        "properties": {
            "id": {"maximum": 2147483647, "minimum": -2147483648, "title": "Id", "type": "integer"},
            "name": {"maxLength": 50, "title": "Name", "type": "string"},
            "talks_to": {
                "items": {"$ref": "#/$defs/Employee_6fp7dmib3ccgfhcl_leaf"},
                "title": "Talks To",
                "type": "array",
            },
            "manager_id": {
                "anyOf": [{"maximum": 2147483647, "minimum": -2147483648, "type": "integer"}, {"type": "null"}],
                "default": None,
                "nullable": True,
                "title": "Manager Id",
            },
            "team_members": {
                "items": {"$ref": "#/$defs/Employee_6fp7dmib3ccgfhcl_leaf"},
                "title": "Team Members",
                "type": "array",
            },
            "name_length": {"description": "", "readOnly": True, "title": "Name Length", "type": "integer"},
            "team_size": {
                "description": (
                    "Computes team size.<br/><br/>Note that this function needs to be annotated with "
                    "a return type so that pydantic can<br/> generate a valid schema.<br/><br/>Note "
                    "that the pydantic serializer can't call async methods, but the hare helpers<br/> "
                    "pre-fetch relational data, so that it is available before serialization. So we "
                    "don't<br/> need to await the relation. We do however have to protect against the "
                    "case where no<br/> prefetching was done, hence catching and handling the<br/> "
                    "``hare.exceptions.NoValuesFetched`` exception."
                ),
                "readOnly": True,
                "title": "Team Size",
                "type": "integer",
            },
        },
        "required": ["id", "name", "talks_to", "team_members", "name_length", "team_size"],
        "title": "Employee",
        "type": "object",
    }


# Tests for TestPydanticUpdate
def test_create_schema(db):
    UserCreate_Pydantic = pydantic_model_creator(
        User,
        name="UserCreate",
        exclude_readonly=True,
    )
    assert UserCreate_Pydantic.model_json_schema() == {
        "title": "UserCreate",
        "type": "object",
        "properties": {
            "username": {
                "title": "Username",
                "maxLength": 32,
                "type": "string",
            },
            "mail": {
                "title": "Mail",
                "maxLength": 64,
                "type": "string",
            },
            "bio": {
                "title": "Bio",
                "type": "string",
            },
        },
        "required": [
            "username",
            "mail",
            "bio",
        ],
        "additionalProperties": False,
    }


def test_update_schema(db):
    """All fields of this schema should be optional.
    This demonstrates an example PATCH endpoint in an API, where a client may want
    to update a single field of a model without modifying the rest.
    """
    UserUpdate_Pydantic = pydantic_model_creator(
        User,
        name="UserUpdate",
        exclude_readonly=True,
        optional=("username", "mail", "bio"),
    )
    assert UserUpdate_Pydantic.model_json_schema() == {
        "additionalProperties": False,
        "properties": {
            "bio": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
                "title": "Bio",
            },
            "mail": {
                "anyOf": [{"maxLength": 64, "type": "string"}, {"type": "null"}],
                "default": None,
                "title": "Mail",
            },
            "username": {
                "anyOf": [{"maxLength": 32, "type": "string"}, {"type": "null"}],
                "default": None,
                "title": "Username",
            },
        },
        "title": "UserUpdate",
        "type": "object",
    }


# Tests for TestPydanticOptionalUpdate
def test_optional_update(db):
    UserUpdateAllOptional_Pydantic = pydantic_model_creator(
        User,
        name="UserUpdateAllOptional",
        exclude_readonly=True,
        optional=("username", "mail", "bio"),
    )
    UserUpdatePartialOptional_Pydantic = pydantic_model_creator(
        User,
        name="UserUpdatePartialOptional",
        exclude_readonly=True,
        optional=("username", "mail"),
    )
    UserUpdateWithoutOptional_Pydantic = pydantic_model_creator(
        User,
        name="UserUpdateWithoutOptional",
        exclude_readonly=True,
    )

    # All fields are optional
    assert UserUpdateAllOptional_Pydantic().model_dump(exclude_unset=True) == {}
    assert UserUpdateAllOptional_Pydantic(bio="foo").model_dump(exclude_unset=True) == {"bio": "foo"}
    assert UserUpdateAllOptional_Pydantic(username="name", mail="a@example.com").model_dump(exclude_unset=True) == {
        "username": "name",
        "mail": "a@example.com",
    }
    assert UserUpdateAllOptional_Pydantic(username="name", mail="a@example.com").model_dump() == {
        "username": "name",
        "mail": "a@example.com",
        "bio": None,
    }
    # Some fields are optional
    with pytest.raises(ValidationError):
        UserUpdatePartialOptional_Pydantic()
    with pytest.raises(ValidationError):
        UserUpdatePartialOptional_Pydantic(username="name")
    assert UserUpdatePartialOptional_Pydantic(bio="foo").model_dump(exclude_unset=True) == {"bio": "foo"}
    assert UserUpdatePartialOptional_Pydantic(username="name", mail="a@example.com", bio="").model_dump(
        exclude_unset=True
    ) == {"username": "name", "mail": "a@example.com", "bio": ""}
    assert UserUpdatePartialOptional_Pydantic(mail="a@example.com", bio="").model_dump() == {
        "username": None,
        "mail": "a@example.com",
        "bio": "",
    }
    # None of the fields is optional
    with pytest.raises(ValidationError):
        UserUpdateWithoutOptional_Pydantic()
    with pytest.raises(ValidationError):
        UserUpdateWithoutOptional_Pydantic(username="name")
    with pytest.raises(ValidationError):
        UserUpdateWithoutOptional_Pydantic(username="name", email="")
    assert UserUpdateWithoutOptional_Pydantic(username="name", mail="a@example.com", bio="").model_dump() == {
        "username": "name",
        "mail": "a@example.com",
        "bio": "",
    }


# Tests for TestPydanticMutlipleModelUses
def test_no_relations_model_reused(db):
    NoRelationsModel = IntFields
    Pydantic1 = pydantic_model_creator(NoRelationsModel)
    Pydantic2 = pydantic_model_creator(NoRelationsModel)

    assert Pydantic1 is Pydantic2


def test_no_relations_model_one_exclude(db):
    NoRelationsModel = IntFields
    Pydantic1 = pydantic_model_creator(NoRelationsModel)
    Pydantic2 = pydantic_model_creator(NoRelationsModel, exclude=("id",))

    assert Pydantic1 is not Pydantic2
    assert "id" in Pydantic1.model_json_schema()["required"]
    assert "id" not in Pydantic2.model_json_schema()["required"]


def test_no_relations_model_both_exclude(db):
    NoRelationsModel = IntFields
    Pydantic1 = pydantic_model_creator(NoRelationsModel, exclude=("id",))
    Pydantic2 = pydantic_model_creator(NoRelationsModel, exclude=("id",))

    assert Pydantic1 is Pydantic2
    assert "id" not in Pydantic1.model_json_schema()["required"]
    assert "id" not in Pydantic2.model_json_schema()["required"]


def test_no_relations_model_exclude_diff(db):
    NoRelationsModel = IntFields
    Pydantic1 = pydantic_model_creator(NoRelationsModel, exclude=("id",))
    Pydantic2 = pydantic_model_creator(NoRelationsModel, exclude=("name",))

    assert Pydantic1 is not Pydantic2


def test_no_relations_model_exclude_readonly(db):
    NoRelationsModel = IntFields
    Pydantic1 = pydantic_model_creator(NoRelationsModel)
    Pydantic2 = pydantic_model_creator(NoRelationsModel, exclude_readonly=True)

    assert Pydantic1 is not Pydantic2
    assert "id" in Pydantic1.model_json_schema()["properties"]
    assert "id" not in Pydantic2.model_json_schema()["properties"]


def test_model_with_relations_reused(db):
    ModelWithRelations = Event
    Pydantic1 = pydantic_model_creator(ModelWithRelations)
    Pydantic2 = pydantic_model_creator(ModelWithRelations)

    assert Pydantic1 is Pydantic2


def test_model_with_relations_exclude(db):
    ModelWithRelations = Event
    Pydantic1 = pydantic_model_creator(ModelWithRelations)
    Pydantic2 = pydantic_model_creator(ModelWithRelations, exclude=("event_id",))

    assert Pydantic1 is not Pydantic2
    assert "event_id" in Pydantic1.model_json_schema()["properties"]
    assert "event_id" not in Pydantic2.model_json_schema()["properties"]


def test_model_with_relations_exclude_readonly(db):
    ModelWithRelations = Event
    Pydantic1 = pydantic_model_creator(ModelWithRelations)
    Pydantic2 = pydantic_model_creator(ModelWithRelations, exclude_readonly=True)

    assert Pydantic1 is not Pydantic2
    assert "event_id" in Pydantic1.model_json_schema()["properties"]
    assert "event_id" not in Pydantic2.model_json_schema()["properties"]


def test_named_no_relations_model(db):
    NoRelationsModel = IntFields
    Pydantic1 = pydantic_model_creator(NoRelationsModel, name="Foo")
    Pydantic2 = pydantic_model_creator(NoRelationsModel, name="Foo")

    assert Pydantic1 is Pydantic2


def test_named_model_with_relations(db):
    ModelWithRelations = Event
    Pydantic1 = pydantic_model_creator(ModelWithRelations, name="Foo")
    Pydantic2 = pydantic_model_creator(ModelWithRelations, name="Foo")

    assert Pydantic1 is Pydantic2


# Tests for TestPydanticEnum
def test_int_enum(db):
    EnumFields_Pydantic = pydantic_model_creator(EnumFields)
    with pytest.raises(ValidationError) as exc_info:
        EnumFields_Pydantic.model_validate({"id": 1, "service": 4, "currency": "HUF"})
    assert [
        {
            "type": "enum",
            "loc": ("service",),
            "msg": "Input should be 1, 2 or 3",
            "input": 4,
            "ctx": {"expected": "1, 2 or 3"},
        }
    ] == exc_info.value.errors(include_url=False)
    with pytest.raises(ValidationError) as exc_info:
        EnumFields_Pydantic.model_validate({"id": 1, "service": "a string, not int", "currency": "HUF"})
    assert [
        {
            "type": "enum",
            "loc": ("service",),
            "msg": "Input should be 1, 2 or 3",
            "input": "a string, not int",
            "ctx": {"expected": "1, 2 or 3"},
        }
    ] == exc_info.value.errors(include_url=False)


def test_str_enum(db):
    EnumFields_Pydantic = pydantic_model_creator(EnumFields)
    with pytest.raises(ValidationError) as exc_info:
        EnumFields_Pydantic.model_validate({"id": 1, "service": 3, "currency": "GoofyGooberDollar"})
    assert [
        {
            "type": "enum",
            "loc": ("currency",),
            "msg": "Input should be 'HUF', 'EUR' or 'USD'",
            "input": "GoofyGooberDollar",
            "ctx": {"expected": "'HUF', 'EUR' or 'USD'"},
        }
    ] == exc_info.value.errors(include_url=False)
    with pytest.raises(ValidationError) as exc_info:
        EnumFields_Pydantic.model_validate({"id": 1, "service": 3, "currency": 1})
    assert [
        {
            "type": "enum",
            "loc": ("currency",),
            "msg": "Input should be 'HUF', 'EUR' or 'USD'",
            "input": 1,
            "ctx": {"expected": "'HUF', 'EUR' or 'USD'"},
        }
    ] == exc_info.value.errors(include_url=False)


def test_enum(db):
    EnumFields_Pydantic = pydantic_model_creator(EnumFields)
    with pytest.raises(ValidationError) as exc_info:
        EnumFields_Pydantic.model_validate({"id": 1, "service": 4, "currency": 1})
    assert [
        {
            "type": "enum",
            "loc": ("service",),
            "msg": "Input should be 1, 2 or 3",
            "input": 4,
            "ctx": {"expected": "1, 2 or 3"},
        },
        {
            "type": "enum",
            "loc": ("currency",),
            "msg": "Input should be 'HUF', 'EUR' or 'USD'",
            "input": 1,
            "ctx": {"expected": "'HUF', 'EUR' or 'USD'"},
        },
    ] == exc_info.value.errors(include_url=False)

    # should simply not raise any error:
    EnumFields_Pydantic.model_validate({"id": 1, "service": 3, "currency": "HUF"})
    assert {
        "$defs": {
            "Currency": {
                "enum": ["HUF", "EUR", "USD"],
                "title": "Currency",
                "type": "string",
            },
            "Service": {"enum": [1, 2, 3], "title": "Service", "type": "integer"},
        },
        "additionalProperties": False,
        "properties": {
            "id": {
                "maximum": 2147483647,
                "minimum": -2147483648,
                "title": "Id",
                "type": "integer",
            },
            "service": {
                "$ref": "#/$defs/Service",
                "description": "python_programming: 1<br/>database_design: 2<br/>system_administration: 3",
            },
            "currency": {
                "$ref": "#/$defs/Currency",
                "default": "HUF",
                "description": "HUF: HUF<br/>EUR: EUR<br/>USD: USD",
            },
        },
        "required": ["id", "service"],
        "title": "EnumFields",
        "type": "object",
    } == EnumFields_Pydantic.model_json_schema()


def test_nullable_fk_not_required(db):
    """Nullable FK/O2O relation fields should be optional (default=None) in the schema,
    not marked as required. This is the fix for issue #1481."""
    Event_Pydantic = pydantic_model_creator(Event, name="EventNullableTest")
    schema = Event_Pydantic.model_json_schema()

    # 'reporter' is a nullable FK (null=True) so it must NOT be required
    assert "reporter" not in schema["required"]
    reporter_prop = schema["properties"]["reporter"]
    assert reporter_prop.get("nullable") is True
    assert reporter_prop.get("default") is None

    # 'tournament' is a non-nullable FK so it MUST be required
    assert "tournament" in schema["required"]

    # 'address' is a nullable O2O backward relation so it must NOT be required
    assert "address" not in schema["required"]
    address_prop = schema["properties"]["address"]
    assert address_prop.get("nullable") is True
    assert address_prop.get("default") is None


def test_optional_relation_field_is_optional(db):
    """optional=(...) on a non-nullable FK/O2O relation field must widen its annotation to
    Optional[...], the same way it already does for plain data fields - not just give it a
    default=None while keeping the plain (non-Optional) type, which produces an internally
    inconsistent schema (not required, but no null branch in the type) and a real instance built
    by omitting the field violates its own declared type.
    """
    Event_Pydantic = pydantic_model_creator(Event, name="EventTournamentOptional", optional=("tournament",))
    schema = Event_Pydantic.model_json_schema()

    assert "tournament" not in schema["required"]
    tournament_prop = schema["properties"]["tournament"]
    assert tournament_prop.get("default") is None
    assert "anyOf" in tournament_prop, (
        f"expected an Optional (anyOf [..., null]) schema for 'tournament', got {tournament_prop}"
    )
    assert {"type": "null"} in tournament_prop["anyOf"]

    instance = Event_Pydantic.model_construct()
    assert instance.tournament is None


def test_field_with_default_not_optional(db):
    """Fields with a default value but null=False should not be marked as Optional."""
    Event_Pydantic = pydantic_model_creator(Event, name="EventDefaultNotOptional")
    schema = Event_Pydantic.model_json_schema()

    # 'token' has default=generate_token but null is not set (defaults to False),
    # so it must NOT allow None values
    token_prop = schema["properties"]["token"]
    assert token_prop == {"title": "Token", "type": "string"}
    assert "anyOf" not in token_prop

    # Validation should reject None for a non-nullable field with a default
    with pytest.raises(ValidationError):
        Event_Pydantic(event_id=1, name="test", tournament=1, token=None, modified="2024-01-01T00:00:00")


@pytest.mark.asyncio
async def test_computed_field_respects_exclude(db, pydantic_computed_setup):
    """exclude= must filter computed fields the same as ordinary fields.

    Computed fields live in ``model_computed_fields``, not ``model_fields`` - checking the
    wrong attribute would make this assertion pass regardless of the bug.
    """
    employee = pydantic_computed_setup["employee"]

    Employee_Pydantic = pydantic_model_creator(Employee, name="EmployeeComputedExclude", exclude=("name_length",))
    assert "name_length" not in Employee_Pydantic.model_computed_fields
    assert "team_size" in Employee_Pydantic.model_computed_fields

    employee_pyd = await Employee_Pydantic.from_hare_orm(await Employee.objects.get(pk=employee.pk))
    assert "name_length" not in employee_pyd.model_dump()


@pytest.mark.asyncio
async def test_computed_field_respects_include(db, pydantic_computed_setup):
    """include= must limit computed fields to only the named ones, like ordinary fields."""
    employee = pydantic_computed_setup["employee"]

    Employee_Pydantic = pydantic_model_creator(Employee, name="EmployeeComputedInclude", include=("name", "team_size"))
    assert "team_size" in Employee_Pydantic.model_computed_fields
    assert "name_length" not in Employee_Pydantic.model_computed_fields
    assert "not_annotated" not in Employee_Pydantic.model_computed_fields

    employee_pyd = await Employee_Pydantic.from_hare_orm(await Employee.objects.get(pk=employee.pk))
    assert "name_length" not in employee_pyd.model_dump()


@pytest.mark.asyncio
async def test_computed_field_respects_exclude_in_queryset_creator(db, pydantic_computed_setup):
    """pydantic_queryset_creator is built on pydantic_model_creator and must inherit the fix."""
    Employee_Pydantic_List = pydantic_queryset_creator(
        Employee, name="EmployeeComputedExcludeList", exclude=("name_length",)
    )
    inner_model = Employee_Pydantic_List.model_fields["root"].annotation.__args__[0]
    assert "name_length" not in inner_model.model_computed_fields
    assert "team_size" in inner_model.model_computed_fields


# Tests for computed fields accessing relations (#1440)
@pytest.mark.asyncio
async def test_computed_field_excluded_relation_not_prefetched(db):
    """Computed field accessing an excluded, non-prefetched relation.

    When team_members is excluded from the Pydantic model AND not manually prefetched,
    the wrapped function dispatches to the ORM object which raises NoValuesFetched.
    If the user's function handles the error gracefully (like team_size does),
    it returns a default. If it doesn't, NoValuesFetched propagates with a clear message.
    """
    Employee_Pydantic_NoTeam = pydantic_model_creator(
        Employee,
        name="Employee_NoTeam",
        exclude=("team_members", "manager", "gets_talked_to"),
        computed=("name_length", "team_size"),
        allow_cycles=True,
    )

    root = await Employee.objects.create(name="Root")
    await Employee.objects.create(name="Member1", manager=root)
    await Employee.objects.create(name="Member2", manager=root)

    empp = await Employee_Pydantic_NoTeam.from_hare_orm(await Employee.objects.get(name="Root"))
    empdict = empp.model_dump()

    assert "team_members" not in empdict
    # team_size returns 0 because team_members was not prefetched and the
    # team_size function gracefully handles NoValuesFetched
    assert empdict["team_size"] == 0
    assert empdict["name_length"] == 4


@pytest.mark.asyncio
async def test_computed_field_excluded_relation_works_with_manual_prefetch(db):
    """Computed field accessing an excluded relation works when manually prefetched.

    If the user prefetches the relation before calling from_hare_orm, the computed
    field can access it on the ORM object even though it's excluded from the schema.
    """
    Employee_Pydantic_NoTeam = pydantic_model_creator(
        Employee,
        name="Employee_NoTeam2",
        exclude=("team_members", "manager", "gets_talked_to"),
        computed=("name_length", "team_size"),
        allow_cycles=True,
    )

    root = await Employee.objects.create(name="Root")
    await Employee.objects.create(name="Member1", manager=root)
    await Employee.objects.create(name="Member2", manager=root)

    obj = await Employee.objects.get(name="Root")
    await prefetch_related_objects([obj], "team_members")
    empp = await Employee_Pydantic_NoTeam.from_hare_orm(obj)
    empdict = empp.model_dump()

    assert "team_members" not in empdict
    assert empdict["team_size"] == 2
    assert empdict["name_length"] == 4


@pytest.mark.asyncio
async def test_computed_field_relation_in_model(db, pydantic_cycle_setup):
    """Computed field accessing a reverse relation that IS in the Pydantic model.

    This tests the happy path where team_members is a Pydantic field AND team_size
    accesses it via the ORM object.
    """
    Employee_Pydantic = pydantic_cycle_setup["Employee_Pydantic"]

    empp = await Employee_Pydantic.from_hare_orm(await Employee.objects.get(name="Root"))
    empdict = empp.model_dump()

    # team_members is present in the schema
    assert "team_members" in empdict
    # team_size correctly reports the count
    assert empdict["team_size"] == 2
    assert empdict["name_length"] == 4


# ============================================================================
# Composite primary key support
# ============================================================================


def test_pydantic_model_creator_composite_pk_exposes_each_component(db):
    """A composite PK (CompositePrimaryKey) has no single Field to represent it - each
    component becomes its own top-level schema field, named after the model's own attribute,
    the same way the model itself already exposes them (id/version, not a nested pk object)."""
    VersionedDocument_Pydantic = pydantic_model_creator(VersionedDocument)
    schema = VersionedDocument_Pydantic.model_json_schema()

    assert "id" in schema["properties"]
    assert "version" in schema["properties"]
    assert "title" in schema["properties"]


@pytest.mark.asyncio
async def test_pydantic_roundtrip_composite_pk_model(db):
    doc = await VersionedDocument.objects.create(title="first")
    # revision_notes is a backward relation - excluded here since it isn't prefetched and this
    # test validates straight from the ORM object rather than going through from_hare_orm().
    VersionedDocument_Pydantic = pydantic_model_creator(VersionedDocument, exclude=("revision_notes",))

    validated = VersionedDocument_Pydantic.model_validate(doc)

    assert validated.id == doc.id
    assert validated.version == doc.version
    assert validated.title == "first"


@pytest.mark.asyncio
async def test_pydantic_from_hare_orm_composite_pk_model(db):
    doc = await VersionedDocument.objects.create(title="first")
    VersionedDocument_Pydantic = pydantic_model_creator(VersionedDocument)

    validated = await VersionedDocument_Pydantic.from_hare_orm(doc)

    assert validated.id == doc.id
    assert validated.version == doc.version


def test_pydantic_validates_composite_pk_model_from_plain_dict(db):
    """Validation (not just generation) works for a composite-PK model - a plain dict payload
    (not an ORM instance) with explicit values for every PK component validates cleanly."""
    from uuid import uuid4

    VersionedDocument_Pydantic = pydantic_model_creator(VersionedDocument, exclude=("revision_notes",))

    validated = VersionedDocument_Pydantic.model_validate({"id": uuid4(), "version": 3, "title": "from dict"})

    assert validated.version == 3
    assert validated.title == "from dict"


def test_pydantic_composite_pk_component_missing_is_required(db):
    """A composite PK component with no default (CompositePkThing.thing_id/revision) is
    required in the generated schema, same as a required single-column PK."""
    CompositePkThing_Pydantic = pydantic_model_creator(CompositePkThing)

    with pytest.raises(ValidationError):
        CompositePkThing_Pydantic.model_validate({"name": "no pk given"})


def test_pydantic_exclude_readonly_excludes_every_composite_pk_component(db):
    """exclude_readonly=True builds a "create" schema that never includes the PK - for a
    composite PK that means every component, not just the first, mirroring how a single-column
    PK is already excluded from this schema variant."""
    VersionedDocument_Create = pydantic_model_creator(VersionedDocument, exclude_readonly=True)
    schema = VersionedDocument_Create.model_json_schema()

    assert "id" not in schema["properties"]
    assert "version" not in schema["properties"]
    assert "title" in schema["properties"]


def test_pydantic_exclude_raw_fields_strips_every_shadow_column_of_a_composite_target_fk(db):
    """exclude_raw_fields=True (the default) pops a plain FK's single raw shadow column
    ("document_id") once the relation field ("document") is present - for a FK targeting a
    composite-PK model (DocumentRevisionNote.document -> VersionedDocument's id+version), there
    are TWO shadow columns ("document_id"/"document_version"), and only the first used to get
    popped, leaking the second as a plain top-level field in the generated schema."""
    DocumentRevisionNote_Pydantic = pydantic_model_creator(DocumentRevisionNote)
    schema = DocumentRevisionNote_Pydantic.model_json_schema()

    assert "document" in schema["properties"]
    assert "document_id" not in schema["properties"]
    assert "document_version" not in schema["properties"]


# Tests for model_validate() on an unfetched relation raising NoValuesFetched
@pytest.mark.asyncio
async def test_model_validate_unfetched_fk_raises_no_values_fetched(db):
    """model_validate() on an unfetched forward FK must raise a clear NoValuesFetched naming
    the field, instead of letting pydantic's own type validation reject the raw relation-getter
    value (an unresolved QuerySet) with an unrelated, confusing 'Field required' error."""
    Event_Pydantic = pydantic_model_creator(Event, name="EventUnfetchedFK")
    tournament = await Tournament.objects.create(name="Unfetched FK Tournament")
    event = await Event.objects.create(name="Unfetched FK Event", tournament=tournament)

    plain_event = await Event.objects.get(pk=event.event_id)
    with pytest.raises(NoValuesFetched, match="tournament"):
        Event_Pydantic.model_validate(plain_event)


@pytest.mark.asyncio
async def test_model_validate_unfetched_nullable_fk_raises_no_values_fetched(db):
    """Even a nullable FK whose shadow column is actually NULL (so the lazy getter returns the
    NoneAwaitable sentinel, not a real None) must raise NoValuesFetched - that sentinel is only
    ever produced by an un-fetched access, never by a real select_related()/prefetch_related()
    call, so it can't be told apart from "genuinely unfetched" any other way."""
    Event_Pydantic = pydantic_model_creator(Event, name="EventUnfetchedNullableFK")
    tournament = await Tournament.objects.create(name="No Reporter Tournament")
    event = await Event.objects.create(name="No Reporter Event", tournament=tournament)

    plain_event = await Event.objects.get(pk=event.event_id)
    await prefetch_related_objects([plain_event], "tournament")
    with pytest.raises(NoValuesFetched, match="reporter"):
        Event_Pydantic.model_validate(plain_event)


@pytest.mark.asyncio
async def test_model_validate_unfetched_backward_o2o_raises_no_values_fetched(db):
    Event_Pydantic = pydantic_model_creator(Event, name="EventUnfetchedO2O")
    tournament = await Tournament.objects.create(name="O2O Tournament")
    event = await Event.objects.create(name="O2O Event", tournament=tournament)
    await Address.objects.create(city="Santa Monica", street="Ocean", event=event)

    plain_event = await Event.objects.get(pk=event.event_id)
    await prefetch_related_objects([plain_event], "tournament", "reporter", "participants")
    with pytest.raises(NoValuesFetched, match="address"):
        Event_Pydantic.model_validate(plain_event)


@pytest.mark.asyncio
async def test_model_validate_unfetched_m2m_raises_no_values_fetched(db):
    Event_Pydantic = pydantic_model_creator(Event, name="EventUnfetchedM2M")
    tournament = await Tournament.objects.create(name="M2M Tournament")
    event = await Event.objects.create(name="M2M Event", tournament=tournament)
    team = await Team.objects.create(name="M2M Team")
    await event.participants.add(team)

    plain_event = await Event.objects.get(pk=event.event_id)
    await prefetch_related_objects([plain_event], "tournament", "reporter", "address")
    with pytest.raises(NoValuesFetched, match="participants"):
        Event_Pydantic.model_validate(plain_event)


@pytest.mark.asyncio
async def test_model_validate_unfetched_backward_fk_raises_no_values_fetched(db):
    """A backward FK reverse relation (Tournament.events) surfaces the same clear error."""
    Tournament_Pydantic = pydantic_model_creator(Tournament, name="TournamentUnfetchedBackwardFK")
    tournament = await Tournament.objects.create(name="Backward FK Tournament")

    plain_tournament = await Tournament.objects.get(pk=tournament.id)
    with pytest.raises(NoValuesFetched, match="events"):
        Tournament_Pydantic.model_validate(plain_tournament)


@pytest.mark.asyncio
async def test_model_validate_fully_fetched_relations_still_works(db):
    """Once every relation the schema references is actually fetched, plain model_validate()
    (not just from_hare_orm()/from_queryset()) works exactly like the documentation says."""
    Event_Pydantic = pydantic_model_creator(Event, name="EventFullyFetched")
    tournament = await Tournament.objects.create(name="Fully Fetched Tournament")
    reporter = await Reporter.objects.create(name="Fully Fetched Reporter")
    event = await Event.objects.create(name="Fully Fetched Event", tournament=tournament, reporter=reporter)
    team = await Team.objects.create(name="Fully Fetched Team")
    await event.participants.add(team)
    await Address.objects.create(city="Santa Monica", street="Ocean", event=event)

    plain_event = await Event.objects.get(pk=event.event_id)
    await prefetch_related_objects([plain_event], "tournament", "reporter", "participants", "address")
    await prefetch_related_objects([plain_event.address], "m2mwitho2opks")

    schema = Event_Pydantic.model_validate(plain_event)
    assert schema.tournament.name == "Fully Fetched Tournament"
    assert schema.reporter.name == "Fully Fetched Reporter"
    assert [team_schema.name for team_schema in schema.participants] == ["Fully Fetched Team"]


# Tests for GeneratedField not being marked required in the ordinary (non-exclude_readonly) schema
def test_generated_field_not_required_in_read_schema():
    """A non-PK GeneratedField (a DB-computed column) must be optional/read-only in the ordinary
    read schema, matching Field.required's own definition - it's filled in by the database, the
    caller never provides it. Separate from exclude_readonly=True, which drops the field
    entirely instead (see test_exclude_readonly_excludes_a_non_pk_generated_field)."""
    from tests.fields.models_generated_field import PricedItem

    ReadSchema = pydantic_model_creator(PricedItem, name="PricedItemRead")

    assert ReadSchema.model_fields["total"].is_required() is False
    assert ReadSchema.model_json_schema()["properties"]["total"]["readOnly"] is True

    validated = ReadSchema.model_validate({"id": 1, "price": "10.00", "quantity": 2})
    assert validated.total is None


def test_generated_pk_still_required_in_read_schema():
    """An auto-increment integer PK is ALSO generated=True (IntField.__init__ sets this
    automatically for primary_key=True) - unlike a non-PK GeneratedField, its required-ness in
    the ordinary read schema is a separate, pre-existing design (every other schema test in this
    file encodes it), not something the GeneratedField fix should touch."""
    IntFields_Pydantic = pydantic_model_creator(IntFields, name="IntFieldsGeneratedPkStillRequired")

    assert IntFields_Pydantic.model_fields["id"].is_required() is True
    assert "readOnly" not in IntFields_Pydantic.model_json_schema()["properties"]["id"]


# Tests for auto_now/auto_now_add not being marked required in the ordinary read schema
def test_auto_now_field_not_required_in_read_schema():
    """A DatetimeField(auto_now=True) is the sibling mechanism to generated=True - the ORM fills
    it in on every save, so the caller should never have to provide it either. Before this fix,
    only field.generated was considered when computing required-ness, so this field stayed
    required despite already being marked readOnly (via DatetimeField.constraints)."""
    ReadSchema = pydantic_model_creator(DatetimeFields, name="DatetimeFieldsAutoNowRead")

    assert ReadSchema.model_fields["datetime_auto"].is_required() is False
    assert ReadSchema.model_json_schema()["properties"]["datetime_auto"]["readOnly"] is True

    validated = ReadSchema.model_validate({"id": 1, "datetime": "2026-01-01T00:00:00"})
    assert validated.datetime_auto is None


def test_auto_now_add_field_not_required_in_read_schema():
    """Same as test_auto_now_field_not_required_in_read_schema, for auto_now_add - a distinct
    field on DatetimeField from auto_now, set only on first save, but sharing the same
    ORM-fills-it-in reasoning and the same readOnly constraint."""
    ReadSchema = pydantic_model_creator(DatetimeFields, name="DatetimeFieldsAutoNowAddRead")

    assert ReadSchema.model_fields["datetime_add"].is_required() is False
    assert ReadSchema.model_json_schema()["properties"]["datetime_add"]["readOnly"] is True

    validated = ReadSchema.model_validate({"id": 1, "datetime": "2026-01-01T00:00:00"})
    assert validated.datetime_add is None


def test_auto_now_time_field_not_required_in_read_schema():
    """TimeField shares its auto_now handling with DatetimeField (both use the same
    auto_now_value_for_db()/constraints machinery) - covered separately since TimeField is a
    distinct Field subclass from DatetimeField in creator.py's field processing."""
    from tests.testmodels import TimeFields

    ReadSchema = pydantic_model_creator(TimeFields, name="TimeFieldsAutoNowRead")

    assert ReadSchema.model_fields["time_auto"].is_required() is False
    assert ReadSchema.model_json_schema()["properties"]["time_auto"]["readOnly"] is True

    validated = ReadSchema.model_validate({"id": 1, "time": "12:00:00"})
    assert validated.time_auto is None


# Tests for a callable default not being marked required in the generated schema, matching
# Field.required's own "self.default is None" check - creator.py used to explicitly exclude a
# callable default from its required-ness logic, so e.g. UUIDField(primary_key=True)'s implicit
# default=uuid4 (and any other Field(default=some_callable)) stayed required=True even though
# Model.__init__ calls the callable to fill the field in whenever the caller omits it.
def test_uuid_pk_callable_default_not_required_in_read_schema():
    """UUIDField(primary_key=True, default=uuid.uuid1) is a PK with an explicit callable
    default. The generated field must use default_factory=uuid.uuid1 (not default=uuid.uuid1,
    which would hand Pydantic the function object itself as a literal default) so an omitted
    value is filled in with an actual generated UUID, the same as the ORM's own behavior."""
    UUIDFields_Pydantic = pydantic_model_creator(UUIDFields, name="UUIDFieldsPkCallableDefaultRead")

    id_field_info = UUIDFields_Pydantic.model_fields["id"]
    assert id_field_info.is_required() is False
    assert id_field_info.default_factory is uuid.uuid1
    assert isinstance(id_field_info.get_default(call_default_factory=True, validated_data={}), uuid.UUID)

    validated = UUIDFields_Pydantic.model_validate({"data": uuid.uuid4()})
    assert isinstance(validated.id, uuid.UUID)


def test_uuid_non_pk_callable_default_not_required_in_read_schema():
    """Same as test_uuid_pk_callable_default_not_required_in_read_schema, for a non-PK field
    (UUIDField(default=uuid.uuid4))."""
    UUIDFields_Pydantic = pydantic_model_creator(UUIDFields, name="UUIDFieldsNonPkCallableDefaultRead")

    data_auto_field_info = UUIDFields_Pydantic.model_fields["data_auto"]
    assert data_auto_field_info.is_required() is False
    assert data_auto_field_info.default_factory is uuid.uuid4
    assert isinstance(data_auto_field_info.get_default(call_default_factory=True, validated_data={}), uuid.UUID)

    validated = UUIDFields_Pydantic.model_validate({"id": uuid.uuid1(), "data": uuid.uuid4()})
    assert isinstance(validated.data_auto, uuid.UUID)


def test_json_field_callable_default_not_required_in_read_schema():
    """JSONField(default=dict) is a non-PK field with a callable default - same fix as the UUID
    cases above, verified end to end with model_validate() actually producing a real dict (via
    default_factory) for the omitted field, not None or the dict class object itself."""
    DirtyTrackedThing_Pydantic = pydantic_model_creator(DirtyTrackedThing, name="DirtyTrackedThingCallableDefaultRead")

    data_field_info = DirtyTrackedThing_Pydantic.model_fields["data"]
    assert data_field_info.is_required() is False
    assert data_field_info.default_factory is dict

    validated = DirtyTrackedThing_Pydantic.model_validate({"id": 1, "name": "thing"})
    assert validated.data == {}


def test_async_callable_defaults_are_none_not_an_unawaited_coroutine_in_read_schema():
    """An async default (async def, an object with an async __call__, or a functools.partial
    over an async function) used to be handed straight to default_factory= like any sync
    callable - Pydantic calls default_factory synchronously, so the field's "value" became an
    unawaited coroutine object that made model_dump_json() fail outright. Reported as None
    instead, matching Model.__iter__()'s own contract for a pending async default. The sync
    callable default alongside them must still be called normally."""
    CallableDefault_Pydantic = pydantic_model_creator(CallableDefault, name="CallableDefaultAsyncRead")

    validated = CallableDefault_Pydantic.model_validate({"id": 1})

    assert validated.callable_default == "callable_default"
    assert validated.async_default is None
    assert validated.async_callable_object_default is None
    assert validated.async_partial_default is None
    assert "coroutine" not in validated.model_dump_json()


def test_generated_field_json_output_field_accepts_a_json_scalar():
    """GeneratedField wraps its real type via composition (output_field=), not inheritance -
    isinstance(field, JSONField) was False for GeneratedField(output_field=JSONField()) even
    though the generated column IS one at the SQL level, so it fell through to the ordinary
    data-field path instead of being typed Any - `field.get_python_type()` (delegated to
    output_field, `dict | list` for a bare JSONField) then rejected any legitimate JSON scalar
    a DB-generated JSON expression could produce (a number, string, bool, or null), even though
    an ordinary (non-generated) JSONField on the same model already correctly accepts one."""
    from hare.fields.generated_field import GeneratedField

    class GenJsonScalarModel(Model):
        id = fields.IntField(primary_key=True)
        payload = fields.JSONField[dict](default=dict)
        computed = GeneratedField(expression=RawSQLTerm("payload"), output_field=fields.JSONField())

    ModelPydantic = pydantic_model_creator(GenJsonScalarModel, name="GenJsonScalarModelRead")

    validated = ModelPydantic.model_validate({"id": 1, "payload": {}, "computed": 42})
    assert validated.computed == 42
    validated = ModelPydantic.model_validate({"id": 1, "payload": {}, "computed": "just a string"})
    assert validated.computed == "just a string"


def test_generated_field_enum_output_field_keeps_enum_validation():
    """Same composition-not-inheritance gap as the JSONField case above, for
    GeneratedField(output_field=CharEnumField(...)) - isinstance(field, (IntEnumFieldInstance,
    CharEnumFieldInstance)) was False, so python_type fell back to field.get_python_type()
    (delegated to output_field's own get_python_type(), the raw `str`/`int` field_type, not the
    enum), silently losing enum validation entirely - any string under max_length passed, not
    just real enum members."""
    from enum import Enum

    from hare.fields.generated_field import GeneratedField

    class Status(str, Enum):
        ACTIVE = "active"
        INACTIVE = "inactive"

    class GenEnumOutputFieldModel(Model):
        id = fields.IntField(primary_key=True)
        flag = fields.CharField(max_length=10)
        # fields.CharEnumField() is declared to return the enum type itself (CharEnumType), so
        # a caller's model attribute type-checks naturally - not the real Field[Any] instance it
        # actually is at runtime, which is what GeneratedField.__init__ expects here.
        computed_status = GeneratedField(
            expression=RawSQLTerm("flag"),
            output_field=fields.CharEnumField(Status, max_length=10),  # type: ignore[arg-type]
        )

    ModelPydantic = pydantic_model_creator(GenEnumOutputFieldModel, name="GenEnumOutputFieldModelRead")

    assert ModelPydantic.model_fields["computed_status"].annotation is Status
    validated = ModelPydantic.model_validate({"id": 1, "flag": "x", "computed_status": "active"})
    assert validated.computed_status is Status.ACTIVE
    with pytest.raises(ValidationError):
        ModelPydantic.model_validate({"id": 1, "flag": "x", "computed_status": "not_a_real_member"})


def test_multiple_inheritance_from_two_unrelated_abstract_bases_merges_both_pydantic_meta():
    """getattr(cls, "PydanticMeta", None) in _get_meta() only ever sees the FIRST PydanticMeta
    Python's normal MRO attribute lookup finds - for `class Child(AbstractA, AbstractB)` where
    BOTH abstract bases declare their own, independent PydanticMeta, the other one's settings
    silently vanished, and WHICH one survived flipped purely based on base-class order (the same
    class of gap already fixed for ORM Meta.unique_together/constraints/indexes/triggers, see
    ModelMeta.__new__/test_multiple_inheritance_from_two_unrelated_abstract_bases_merges_both_
    meta_collections). Confirmed live before this fix: swapping the two base classes' order (a
    refactor that changes no visible ORM behavior at all) flipped which PydanticMeta won - once
    even leaking a field its own PydanticMeta.exclude explicitly asked to hide."""

    class PydanticMetaAbstractA(Model):
        secret_a = fields.CharField(max_length=20, default="a")

        class Meta:
            abstract = True

        class PydanticMeta:
            exclude = ("secret_a",)

    class PydanticMetaAbstractB(Model):
        name = fields.CharField(max_length=20, null=True)

        class Meta:
            abstract = True

        def double_name(self) -> str:
            return (self.name or "") * 2

        class PydanticMeta:
            computed = ("double_name",)

    # mypy flags the two bases' independently-declared Meta/PydanticMeta classes as
    # incompatible - correct at the type level (neither is a subtype of the other), but both
    # ModelMeta.__new__ and _collect_pydantic_meta() merge them at runtime rather than requiring
    # one to win, exactly what this model exists to exercise.
    class PydanticMetaChildAFirst(PydanticMetaAbstractA, PydanticMetaAbstractB):  # type: ignore[misc]
        id = fields.IntField(primary_key=True)

    class PydanticMetaChildBFirst(PydanticMetaAbstractB, PydanticMetaAbstractA):  # type: ignore[misc]
        id = fields.IntField(primary_key=True)

    for model in (PydanticMetaChildAFirst, PydanticMetaChildBFirst):
        schema = pydantic_model_creator(model, name=f"{model.__name__}Read")
        assert "secret_a" not in schema.model_fields
        assert "double_name" in schema.model_computed_fields


def test_orm_field_validators_enforced_in_generated_schema():
    """Field(validators=[...]) is an ORM-level rule Model.save() enforces via Field.validate() -
    the generated pydantic schema never consulted it at all, so it was systematically more
    permissive than the ORM: it happily accepted a value Field.validate() would reject at save()
    time. A per-field pydantic validator must now run Field.validate() and translate a failure
    into something pydantic recognizes."""
    ValidatorModel_Create = pydantic_model_creator(
        ValidatorModel, exclude_readonly=True, name="ValidatorModelCreateCheck"
    )

    with pytest.raises(ValidationError):
        ValidatorModel_Create.model_validate({"min_value": 5})

    validated = ValidatorModel_Create.model_validate({"min_value": 15})
    assert validated.min_value == 15

    # A field with no validators at all (max_length has none) is unaffected.
    assert ValidatorModel_Create.model_validate({"max_length": "abc"}).max_length == "abc"


def test_orm_field_validators_combine_with_caller_supplied_validators():
    """The auto-generated Field.validate()-backed validator and a caller-supplied validators=
    entry for the SAME field must both actually run, not silently override each other -
    they're injected into __validators__ under distinct keys precisely so both survive."""

    def reject_odd_min_value(cls, value):
        if value is not None and value % 2 != 0:
            raise ValueError("must be even")
        return value

    ValidatorModel_Combined = pydantic_model_creator(
        ValidatorModel,
        exclude_readonly=True,
        name="ValidatorModelCombinedCheck",
        validators={"check_min_value_even": field_validator("min_value")(reject_odd_min_value)},
    )

    # Rejected by the ORM's own MinValueValidator(10.0) alone (6 is even, so the
    # caller-supplied validator would accept it).
    with pytest.raises(ValidationError):
        ValidatorModel_Combined.model_validate({"min_value": 6})
    # Within the ORM's bounds, rejected by the caller-supplied validator alone.
    with pytest.raises(ValidationError):
        ValidatorModel_Combined.model_validate({"min_value": 15})
    # Passes both.
    assert ValidatorModel_Combined.model_validate({"min_value": 16}).min_value == 16


@pytest.mark.asyncio
async def test_binary_field_round_trips_through_json_as_base64(db):
    """Non-UTF-8 bytes used to break model_dump_json() - JSON carries them as base64, and the
    schema says so."""
    BinaryFields_Pydantic = pydantic_model_creator(BinaryFields, name="BinaryFieldsBase64")
    row = await BinaryFields.objects.create(binary=b"\x00\xff", binary_null=None)

    dumped_json = (await BinaryFields_Pydantic.from_hare_orm(row)).model_dump_json()

    assert '"binary":"AP8="' in dumped_json
    assert BinaryFields_Pydantic.model_validate_json(dumped_json).binary == b"\x00\xff"
    assert BinaryFields_Pydantic.model_json_schema()["properties"]["binary"]["format"] == "base64url"


def test_optional_field_accepts_explicit_null_despite_orm_validators(db):
    """optional=(...) widens the schema to accept null ("not provided") - the ORM field's own
    validators (here the automatic max_length one) must not reject that null."""
    UserUpdate_Pydantic = pydantic_model_creator(
        User, name="UserUpdateExplicitNull", exclude_readonly=True, optional=("username", "mail", "bio")
    )

    validated = UserUpdate_Pydantic.model_validate({"username": None, "mail": "a@example.com"})

    assert validated.username is None
    with pytest.raises(ValidationError):
        UserUpdate_Pydantic.model_validate({"username": "x" * 33})


def test_json_field_declared_type_goes_into_the_schema(db):
    JSONFields_Pydantic = pydantic_model_creator(JSONFields, name="JSONFieldsDeclaredType")
    schema = JSONFields_Pydantic.model_json_schema()

    assert schema["properties"]["data_pydantic"]["$ref"] == "#/$defs/TestSchemaForJSONField"
    with pytest.raises(ValidationError):
        JSONFields_Pydantic.model_validate({"id": 1, "data": {}, "data_pydantic": {"foo": "not an int"}})


@pytest.mark.asyncio
async def test_computed_property_and_cached_property(db):
    Tag_Pydantic = pydantic_model_creator(
        Tag,
        name="TagComputedProperties",
        include=("id", "name", "name_upper", "name_length"),
        computed=("name_upper", "name_length"),
    )
    schema_properties = Tag_Pydantic.model_json_schema(mode="serialization")["properties"]

    assert list(schema_properties) == ["id", "name", "name_upper", "name_length"]
    assert schema_properties["name_upper"]["type"] == "string"
    assert schema_properties["name_length"]["type"] == "integer"
    tag = await Tag.objects.create(name="abc")
    assert (await Tag_Pydantic.from_hare_orm(tag)).model_dump() == {
        "id": tag.id,
        "name": "abc",
        "name_upper": "ABC",
        "name_length": 3,
    }


def test_queryset_creator_name_only_names_the_list_model(db):
    Tag_Pydantic_List = pydantic_queryset_creator(Tag, name="TagNamedList")
    item_model = Tag_Pydantic_List.model_config["submodel"]

    assert Tag_Pydantic_List.__name__ == "TagNamedList"
    assert Tag_Pydantic_List.model_config["title"] == "TagNamedList"
    assert "TagNamedList" not in item_model.__name__
    assert item_model.model_config["title"] == "Tag"


def test_optional_pk_accepts_explicit_null(db):
    Tag_Pydantic = pydantic_model_creator(Tag, name="TagOptionalPk", include=("id", "name"), optional=("id", "name"))
    id_schema = Tag_Pydantic.model_json_schema()["properties"]["id"]

    assert {"type": "null"} in id_schema["anyOf"]
    assert Tag_Pydantic.model_validate({"id": None}).id is None
    assert Tag_Pydantic.model_validate_json('{"id": null}').id is None
    dumped_json = Tag_Pydantic.model_validate({"name": "n"}).model_dump_json()
    assert Tag_Pydantic.model_validate_json(dumped_json).name == "n"


def test_non_null_fk_to_a_scoped_target_is_an_optional_submodel(db):
    """A non-null FK to a model with soft delete/tenant/manager scope reads as None when that scope
    hides the target - the nested submodel required a value and from_hare() failed validation."""
    ChildSchema = pydantic_model_creator(SoftDeleteChildCascadeSoft, name="SoftDeleteChildCascadeSoftScoped")

    parent_field = ChildSchema.model_fields["parent"]
    assert type(None) in typing.get_args(parent_field.annotation)
    validated = ChildSchema.model_validate({"id": 1, "name": "C", "deleted_at": None, "parent": None})
    assert validated.parent is None


def test_tenant_foreign_key_is_not_required_in_a_create_schema(db):
    """exclude_readonly=True without relations_as_ids made a tenant FK a required nested relation -
    only its company_id column was recognized as the tenant field."""
    for model in (TenantFkOrder, TenantFkNamedOrder):
        CreateSchema = pydantic_model_creator(model, exclude_readonly=True, name=f"{model.__name__}TenantCreate")
        assert CreateSchema.model_fields["company"].is_required() is False


def test_a_path_into_a_relation_in_include_selects_the_relation(db):
    EventSchema = pydantic_model_creator(Event, name="EventWithTournamentName", include=("name", "tournament.name"))

    assert list(EventSchema.model_fields) == ["name", "tournament"]
    assert list(EventSchema.model_fields["tournament"].annotation.model_fields) == ["name"]


def test_a_computed_field_of_a_relation_without_include(db):
    EmployeeSchema = pydantic_model_creator(
        Employee, name="EmployeeWithTeamNameLengths", computed=("team_members.name_length",)
    )
    member_schema = typing.get_args(EmployeeSchema.model_fields["team_members"].annotation)[0]

    assert "team_members.name_length" not in EmployeeSchema.model_computed_fields
    assert "name_length" in member_schema.model_computed_fields


def test_meta_override_sets_the_model_config(db):
    class TitledMeta:
        model_config = ConfigDict(title="EmployeeCard")

    EmployeeSchema = pydantic_model_creator(Employee, include=("name",), meta_override=TitledMeta)

    assert EmployeeSchema.model_json_schema()["title"] == "EmployeeCard"


def test_the_list_schema_takes_every_option_of_its_item_schema(db):
    class TitledMeta:
        model_config = ConfigDict(title="TournamentRow")

    TournamentListSchema = pydantic_queryset_creator(
        Tournament, include=("id", "name"), optional=("name",), meta_override=TitledMeta
    )
    item_schema = TournamentListSchema.model_config["submodel"]

    assert item_schema.model_json_schema()["title"] == "TournamentRow"
    assert item_schema.model_fields["name"].is_required() is False
    assert item_schema is pydantic_model_creator(
        Tournament, include=("id", "name"), optional=("name",), meta_override=TitledMeta
    )


def test_the_default_list_schema_holds_the_default_item_schema(db):
    assert pydantic_queryset_creator(Event).model_config["submodel"] is pydantic_model_creator(Event)
