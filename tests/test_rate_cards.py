from app.rate_cards import code_key, extract_codes_from_text, load_code_catalog


def test_code_key_treats_zero_padded_variants_as_same_code() -> None:
    assert code_key("UG-7") == ("UG", 7)
    assert code_key("UG-07") == ("UG", 7)
    assert code_key("PC01") == ("PC", 1)


def test_load_code_catalog_keeps_rate_card_display_code() -> None:
    catalog = load_code_catalog("UG-07, PC-01, Comp-13")
    assert catalog[("UG", 7)] == "UG-07"
    assert catalog[("PC", 1)] == "PC-01"
    assert catalog[("COMP", 13)] == "Comp-13"


def test_extract_codes_from_text_dedupes_variants() -> None:
    assert extract_codes_from_text("UG-7 UG-07 PC1 PC-01") == ["UG-7", "PC-1"]
