"""Narrow claim-argument coercion for text fields a model writes as scalars."""

import pytest
from pydantic import ValidationError

from server.capabilities import ClaimsArgs, ResearchClaim


def claim(**changes):
    return {"kind": "fact", "statement": "合成公司 2025 年产量 120 万吨。", **changes}


def test_numeric_period_is_coerced_to_its_text_form():
    assert ResearchClaim.model_validate(claim(period=2024)).period == "2024"
    assert ResearchClaim.model_validate(claim(period=2024.5)).period == "2024.5"
    assert ResearchClaim.model_validate(claim(period="2024Q1")).period == "2024Q1"
    assert ResearchClaim.model_validate(claim(period=None)).period is None


def test_numeric_entity_scalars_become_text_and_dicts_stay_rejected():
    assert ResearchClaim.model_validate(claim(entities=[1, "x", True])).entities == ["1", "x", "True"]
    with pytest.raises(ValidationError) as rejected:
        ResearchClaim.model_validate(claim(entities=[1, "x", {"a": 1}]))
    assert [(error["loc"], error["type"]) for error in rejected.value.errors()] == [
        (("entities", 2), "string_type")
    ]


def test_claim_batch_keeps_the_nested_field_path_for_rejected_entities():
    with pytest.raises(ValidationError) as rejected:
        ClaimsArgs.model_validate({"claims": [claim(entities=[{"a": 1}])]})
    assert [(error["loc"], error["type"]) for error in rejected.value.errors()] == [
        (("claims", 0, "entities", 0), "string_type")
    ]


@pytest.mark.parametrize("period", [True, float("inf"), float("nan")])
def test_only_finite_numbers_are_coerced_for_period(period):
    with pytest.raises(ValidationError) as rejected:
        ResearchClaim.model_validate(claim(period=period))
    assert [(error["loc"], error["type"]) for error in rejected.value.errors()] == [
        (("period",), "string_type")
    ]


def test_claims_still_reject_empty_entity_text_and_keep_string_json_schema():
    with pytest.raises(ValidationError) as rejected:
        ResearchClaim.model_validate(claim(entities=[""]))
    assert rejected.value.errors()[0]["type"] == "string_too_short"

    schema = ClaimsArgs.model_json_schema()["$defs"]["ResearchClaim"]["properties"]
    assert schema["period"]["anyOf"] == [{"maxLength": 120, "type": "string"}, {"type": "null"}]
    assert schema["entities"]["items"]["type"] == "string"


def test_scalar_supports_become_a_single_element_list():
    assert ResearchClaim.model_validate(claim(supports="passage_a")).supports == ["passage_a"]
    assert ResearchClaim.model_validate(claim(supports=12)).supports == ["12"]
    assert ResearchClaim.model_validate(claim(supports=["passage_a", 7])).supports == ["passage_a", "7"]


def test_support_items_keep_rejecting_dicts_and_booleans():
    with pytest.raises(ValidationError) as rejected:
        ResearchClaim.model_validate(claim(supports=["passage_a", {"a": 1}]))
    assert [(error["loc"], error["type"]) for error in rejected.value.errors()] == [
        (("supports", 1), "string_type")
    ]
    # A passage id is an identifier, so a bare boolean must not become "True".
    with pytest.raises(ValidationError) as rejected:
        ResearchClaim.model_validate(claim(supports=True))
    assert [(error["loc"], error["type"]) for error in rejected.value.errors()] == [
        (("supports", 0), "string_type")
    ]


def test_scalar_entity_and_support_lists_stay_rejected_when_nested():
    # The coercion adds no list level: a nested list is still a shape error.
    with pytest.raises(ValidationError) as rejected:
        ResearchClaim.model_validate(claim(entities=[["x"]]))
    assert [(error["loc"], error["type"]) for error in rejected.value.errors()] == [
        (("entities", 0), "string_type")
    ]
    with pytest.raises(ValidationError) as rejected:
        ResearchClaim.model_validate(claim(supports=[["passage_a"]]))
    assert [(error["loc"], error["type"]) for error in rejected.value.errors()] == [
        (("supports", 0), "string_type")
    ]


def test_scalar_quote_values_become_text_but_objects_and_keys_stay_rejected():
    quotes = {"passage_a": 120, "passage_b": 2024.5}
    assert ResearchClaim.model_validate(claim(quotes=quotes)).quotes == {
        "passage_a": "120",
        "passage_b": "2024.5",
    }
    with pytest.raises(ValidationError) as rejected:
        ResearchClaim.model_validate(claim(quotes={"passage_a": {"text": "120"}}))
    assert [(error["loc"], error["type"]) for error in rejected.value.errors()] == [
        (("quotes", "passage_a"), "string_type")
    ]
    # A boolean quote is not textual source content; it stays rejected.
    with pytest.raises(ValidationError) as rejected:
        ResearchClaim.model_validate(claim(quotes={"passage_a": True}))
    assert [(error["loc"], error["type"]) for error in rejected.value.errors()] == [
        (("quotes", "passage_a"), "string_type")
    ]


def test_empty_quote_is_accepted_by_the_schema_for_the_ledger_to_ignore():
    # Production run a1310d01 sent quotes.passage_<id>: "" and the whole five-claim
    # batch was rejected as string_too_short. An empty quote means "no quote for
    # that passage": the ledger drops it and reports quote_empty_ignored in the
    # structured claim details instead of failing the batch.
    parsed = ResearchClaim.model_validate(claim(quotes={"passage_a": "", "passage_b": "   "}))
    assert parsed.quotes == {"passage_a": "", "passage_b": "   "}


def test_shape_coercion_leaves_the_json_schema_unchanged():
    properties = ClaimsArgs.model_json_schema()["$defs"]["ResearchClaim"]["properties"]
    assert properties["supports"]["items"]["type"] == "string"
    assert properties["entities"]["items"]["type"] == "string"
    assert properties["quotes"]["additionalProperties"]["type"] == "string"
    assert properties["quotes"]["additionalProperties"]["maxLength"] == 600


def test_bare_string_entities_become_a_single_element_list():
    assert ResearchClaim.model_validate(claim(entities="贵州茅台")).entities == ["贵州茅台"]
    assert ResearchClaim.model_validate(claim(entities=120)).entities == ["120"]


def test_calculation_formula_names_the_two_required_input_claims():
    with pytest.raises(ValidationError) as rejected:
        ClaimsArgs.model_validate(
            {
                "claims": [
                    claim(
                        kind="calculation",
                        formula={"operator": "add", "input_claim_ids": ["claim_a"]},
                    )
                ]
            }
        )
    error = rejected.value.errors()[0]
    assert error["loc"] == ("claims", 0, "formula", "input_claim_ids")
    assert error["type"] == "value_error"
    # The model must be able to retry from the message alone; the generic
    # pydantic "too_short" text did not say what the field needs.
    assert "两个不同结论ID" in error["msg"]

    with pytest.raises(ValidationError) as too_many:
        ClaimsArgs.model_validate(
            {
                "claims": [
                    claim(
                        kind="calculation",
                        formula={"operator": "add", "input_claim_ids": ["a", "b", "c"]},
                    )
                ]
            }
        )
    assert "两个不同结论ID" in too_many.value.errors()[0]["msg"]

    accepted = ClaimsArgs.model_validate(
        {
            "claims": [
                claim(
                    kind="calculation",
                    formula={"operator": "add", "input_claim_ids": ["a", "b"]},
                )
            ]
        }
    )
    assert accepted.claims[0].formula.input_claim_ids == ["a", "b"]
