"""The outline must lose no text, number sections stably and recognise a heading without markup."""
from motherlode import outline

FILL = " The parties expect to close the transaction in the third quarter, subject to customary conditions." * 5
RELEASE = f"""Acme Gold Options the Big Onion Property

Vancouver, May 31, 2023. Acme Gold Corp. has entered into an option agreement to acquire a 100% interest in the Big Onion property.{FILL}

Terms of the Option

The Company will pay $250,000 in cash on closing. The Company will issue 2,000,000 common shares.{FILL}
The vendor retains a 2% NSR royalty.

About Acme Gold

Acme is a Canadian exploration company.{FILL}
- Listed on the TSX Venture Exchange

Contact

Jane Doe, CEO
"""


def test_headings_start_sections_and_text_stays_with_its_heading():
    parts = outline.sections(RELEASE)
    assert [s.heading for s in parts] == ["Acme Gold Options the Big Onion Property", "Terms of the Option", "About Acme Gold"]
    assert [s.number for s in parts] == [1, 2, 3]
    assert "2% NSR royalty" in parts[1].text and "2% NSR royalty" not in parts[0].text
    assert parts[1].first_sentence == "The Company will pay $250,000 in cash on closing."


def test_a_section_too_small_to_read_separately_joins_its_neighbour():
    parts = outline.sections(RELEASE)
    assert "Contact" in parts[-1].text and "Jane Doe, CEO" in parts[-1].text      # the small tail joined "About Acme Gold"
    small = outline.sections("Heading One\n\nShort text.\n\nHeading Two\n\nAlso short.")
    assert len(small) == 1 and small[0].heading == "Heading One" and "Heading Two" in small[0].text


def test_no_line_of_text_is_lost():
    kept = " ".join(s.heading + " " + s.text for s in outline.sections(RELEASE))
    for line in (line.strip() for line in RELEASE.splitlines()):
        assert line in kept


def test_long_text_without_headings_is_cut_at_paragraphs_and_labelled_by_its_first_words():
    text = "\n".join(f"Paragraph {i} " + "word " * 120 + "end." for i in range(6))
    parts = outline.sections(text)
    assert len(parts) > 1 and all(len(s.text) < 2 * outline.TARGET_CHARS for s in parts)
    assert parts[0].heading.startswith("Paragraph 0 word") and parts[0].heading.endswith("…")
    assert sum(s.text.count("end.") for s in parts) == 6


def test_one_very_long_paragraph_is_cut_at_sentence_ends():
    text = " ".join(f"Sentence number {i} states a fact about the property." for i in range(200))
    parts = outline.sections(text)
    assert len(parts) > 3 and max(len(s.text) for s in parts) < 2 * outline.TARGET_CHARS
    assert all(s.text.rstrip().endswith(".") for s in parts)                       # never cut inside a sentence
    assert sum(s.text.count("Sentence number") for s in parts) == 200


def test_sentences_table_rows_and_list_items_are_not_headings():
    assert outline.is_heading("Terms of the Option") and outline.is_heading("About Acme Gold")
    for line in ("The Company will pay $250,000 in cash.", "- Listed on the TSX Venture Exchange", "Cash | Shares | NSR",
                 "terms of the option continued", "", "A" * 120):
        assert not outline.is_heading(line), line


def test_outline_text_lists_every_section_on_one_line_with_its_size():
    parts = outline.sections(RELEASE)
    lines = outline.outline(parts).splitlines()
    assert len(lines) == 3 and lines[1].startswith(f"2. Terms of the Option ({len(parts[1].text)} chars): The Company will pay")


def test_empty_document_has_no_sections():
    assert outline.sections("") == [] and outline.sections("\n \n") == []
