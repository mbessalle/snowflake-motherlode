"""The prompt, the Pydantic model, the table and the AI_EXTRACT questions must list the same fields."""
from motherlode import cortex, ddl, fields
from motherlode.schema import DealExtraction


def test_fields_come_from_the_prompt_and_match_the_schema():
    names = [f.name for f in fields.all_fields()]
    assert len(names) == 36 and names == list(DealExtraction.model_fields)


def test_sql_types_follow_the_declared_kind():
    by_name = {f.name: f.sql_type for f in fields.all_fields()}
    assert by_name["cash_payments_value"] == "FLOAT" and by_name["news_date"] == "DATE"
    assert by_name["project_name"] == "VARCHAR" and by_name["cash_payments_currency"] == "VARCHAR(3)"
    assert {name for name, kind in by_name.items() if kind == "VARCHAR(3)"} == set(fields.CURRENCY_FIELDS)


def test_committed_table_is_the_generated_one():
    assert ddl.TARGET.read_text() == ddl.deals_table(), "run: python -m motherlode.ddl --write"


def test_table_has_every_field_as_a_column():
    statement = ddl.deals_table()
    for field in fields.all_fields():
        assert f"\n    {field.name.upper()} " in statement
    assert "DOC_ID" in statement and "PRIMARY KEY (DOC_ID)" in statement


def test_extract_questions_cover_every_extracted_field_within_the_function_limit():
    questions = cortex.extract_questions("Big Onion")
    assert set(questions) == set(DealExtraction.model_fields) - set(fields.NOT_EXTRACTED)
    assert len(questions) <= 100                      # AI_EXTRACT: at most 100 entity questions per call
    assert all("'Big Onion'" in q for q in questions.values())
    assert "YYYY-MM-DD" in questions["news_date"] and "one number" in questions["shares_amount"]
