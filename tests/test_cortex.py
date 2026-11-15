"""Building Cortex calls and reading their replies. Reply shapes are the ones in Snowflake's function reference."""
import json

import pytest

from motherlode import cortex, schema


def test_statements_bind_document_text_instead_of_pasting_it():
    for statement in (cortex.PARSE_SQL, cortex.COMPLETE_SQL, cortex.EXTRACT_TEXT_SQL, cortex.EXTRACT_FILE_SQL,
                      cortex.FILTER_SQL, cortex.DEAL_TYPE_SQL, cortex.CONTINENT_SQL):
        assert "?" in statement and statement.startswith("SELECT AI_")


def test_classify_statement_lists_the_categories_and_escapes_quotes():
    assert "['Africa', 'Asia', 'Europe', 'North America', 'Oceania', 'South America']" in cortex.CONTINENT_SQL
    assert "{'label': 'option or earn-in', 'description':" in cortex.DEAL_TYPE_SQL
    assert "'it''s'" in cortex.classify_sql(("it's", "other"), "task")


def test_extraction_prompt_carries_the_production_prompt_the_project_and_the_feedback():
    prompt = cortex.extraction_prompt("Article body.", "Big Onion")
    assert prompt.startswith(cortex.EXTRACTOR_SYSTEM_PROMPT)
    assert "**Primary Project of Interest:** 'Big Onion'" in prompt and "Article body." in prompt
    assert "Correction required" not in prompt
    retry = cortex.extraction_prompt("Article body.", "Big Onion", "- shares_amount: 3.5e+06")
    assert "Correction required" in retry and "- shares_amount: 3.5e+06" in retry


def test_article_is_cut_at_the_same_limit_as_the_original_pipeline():
    prompt = cortex.extraction_prompt("x" * 20000, "Acme")
    assert "x" * 15000 in prompt and "x" * 15001 not in prompt


def test_completion_reply_with_details_gives_updates_and_usage():
    body = {"reasoning_and_acknowledgement": {}, "database_updates": {"project_name": "Acme", "nsr_percent": 2}}
    reply = json.dumps({"choices": [{"messages": "```json\n" + json.dumps(body) + "\n```"}],
                        "usage": {"prompt_tokens": 2164, "completion_tokens": 1283}})
    updates, usage = cortex.parse_completion(reply)
    assert updates == {"project_name": "Acme", "nsr_percent": 2} and usage["prompt_tokens"] == 2164


@pytest.mark.parametrize("reply", [None, "", "not json", "[]", json.dumps({"choices": [{"messages": "I cannot help."}]}),
                                   json.dumps({"choices": [{"messages": json.dumps({"database_updates": "none"})}]})])
def test_a_reply_of_the_wrong_shape_counts_as_nothing_extracted(reply):
    assert cortex.parse_completion(reply)[0] == {}


def test_text_around_the_json_object_is_ignored():
    reply = {"choices": [{"messages": 'Here it is: {"database_updates": {"nsr_percent": 2}} Done.'}]}
    assert cortex.parse_completion(reply)[0] == {"nsr_percent": 2}


@pytest.mark.parametrize("answer, expected", [
    ("$250,000", 250000.0), ("C$1.5 million", 1500000.0), ("US$250K", 250000.0), ("2%", 2.0), ("2.5 percent", 2.5),
    ("4,857 hectares", 4857.0), ("2,000,000 common shares", 2000000.0), ("0.25", 0.25), ("CAD 500,000", 500000.0),
    ("51 per cent", 51.0), ("120 km2", 120.0), ("4,000 m", 4000.0), ("$3M", 3000000.0), ("12 km", 12.0), ("3 million", 3000000.0),
    ("250,000 and 500,000", "250,000 and 500,000"),     # two numbers: left for the schema to reject
    ("two million", "two million"), ("approximately 2,000", "approximately 2,000"),
])
def test_number_answers_become_numbers_and_anything_unclear_is_left_alone(answer, expected):
    assert cortex.to_number(answer) == expected


def test_ai_extract_reply_is_typed_and_passes_the_schema():
    reply = json.dumps({"response": {"project_name": "Big Onion", "cash_payments_value": "$250,000", "nsr_percent": "2%",
                                     "share_price": "None", "owner_ticker": "not stated", "news_date": "2026-11-10",
                                     "cash_payments_currency": "CAD", "shares_amount": "two million"}})
    extraction = cortex.parse_extract(reply)
    assert extraction["cash_payments_value"] == 250000.0 and extraction["nsr_percent"] == 2.0
    assert extraction["share_price"] is None and extraction["owner_ticker"] is None
    assert extraction["project_name"] == "Big Onion"
    # the unclear number is not guessed: it reaches the schema, which reports it
    assert [issue.split(":")[0] for issue in schema.issues(extraction)] == ["shares_amount"]


def test_ai_extract_error_reply_gives_nothing():
    assert cortex.parse_extract(json.dumps({"error": "The provided file isn't in the expected format or is corrupted."})) == {}


def test_structured_output_schema_matches_the_pydantic_model_and_the_documented_rules():
    from motherlode import fields
    from motherlode.schema import DealExtraction

    fmt = cortex.response_format()
    updates = fmt["schema"]["properties"]["database_updates"]
    assert fmt["type"] == "json" and fmt["schema"]["required"] == ["database_updates"]
    assert set(updates["properties"]) == set(DealExtraction.model_fields) - set(fields.NOT_EXTRACTED)
    # Snowflake's rule for OpenAI models: every property required, additionalProperties false at every node
    assert updates["required"] == list(updates["properties"]) and updates["additionalProperties"] is False
    assert fmt["schema"]["additionalProperties"] is False
    assert updates["properties"]["cash_payments_value"]["type"] == ["number", "null"]
    assert updates["properties"]["news_date"]["type"] == ["string", "null"]
    json.dumps(fmt)                                    # must be serialisable: it is sent as a bind parameter


def test_structured_reply_is_read_and_still_goes_through_the_pydantic_gate():
    reply = json.dumps({"structured_output": [{"raw_message": {"database_updates": {"nsr_percent": 150, "project_name": "Acme"}},
                                               "type": "json"}], "usage": {"prompt_tokens": 100, "completion_tokens": 49}})
    updates, usage = cortex.parse_structured(reply)
    assert updates == {"nsr_percent": 150, "project_name": "Acme"} and usage["completion_tokens"] == 49
    # the JSON schema allows any number; the range is Pydantic's job
    assert [issue.split(":")[0] for issue in schema.issues(updates)] == ["nsr_percent"]
    assert cortex.parse_structured(None) == ({}, {}) and cortex.parse_structured('{"structured_output": []}')[0] == {}


def test_classify_and_parse_document_replies():
    assert cortex.parse_label('{"labels": ["North America"]}') == "North America"
    assert cortex.parse_label('{"labels": []}') is None and cortex.parse_label(None) is None
    assert cortex.parse_document('{"content": "Document text.", "metadata": {"pageCount": 3}}') == ("Document text.", 3)
    paged = {"metadata": {"pageCount": 2}, "pages": [{"content": "One.", "index": 0}, {"content": "Two.", "index": 1}]}
    assert cortex.parse_document(paged) == ("One.\n\nTwo.", 2)
    assert cortex.parse_document(None) == ("", 0)        # AI_PARSE_DOCUMENT returns NULL when parsing fails
