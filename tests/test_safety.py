import pytest

from app.safety import is_emergency


@pytest.mark.parametrize(
    "text",
    [
        "My father suddenly has chest pain and can't breathe",
        "Sakit dada!!",
        "I CAN’T BREATHE",  # curly apostrophe, capitals
        "ayah saya pengsan",
        "Mak saya tak boleh bernafas",
    ],
)
def test_catches_tier_a_phrases(text):
    assert is_emergency(text)


@pytest.mark.parametrize(
    "text",
    [
        "Is there parking?",
        "Saya sakit hati dengan servis ni",  # BM idiom for upset: Tier B, not Tier A
        "How much is a consultation?",
        "",
    ],
)
def test_ignores_non_emergencies(text):
    assert not is_emergency(text)
