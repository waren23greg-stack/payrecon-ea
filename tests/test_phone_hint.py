import pytest

from payrecon.normalize import (
    NormalizationError,
    hints_compatible,
    normalize_phone,
    parse_phone_hint,
    phone_hint,
)


def test_phone_hint_masks_like_a_statement():
    assert phone_hint("+254798123048") == "254798***048"
    assert phone_hint("+254798123048", prefix_digits=4) == "2547***048"
    assert phone_hint(normalize_phone("0798 123 048")) == "254798***048"


@pytest.mark.parametrize("prefix_digits", [3, 7])
def test_phone_hint_rejects_silly_prefix_lengths(prefix_digits):
    with pytest.raises(NormalizationError) as exc:
        phone_hint("+254798123048", prefix_digits)
    assert exc.value.code == "bad_phone_length"


def test_phone_hint_rejects_short_numbers():
    with pytest.raises(NormalizationError):
        phone_hint("+2547")


@pytest.mark.parametrize(
    ("raw", "hint"),
    [
        ("254798***048", "254798***048"),
        ("  2547***716 ", "2547***716"),
        ("254 798 *** 048", "254798***048"),
        ("", None),
        (None, None),
        ("-", None),
    ],
)
def test_parse_phone_hint(raw, hint):
    assert parse_phone_hint(raw) == hint


@pytest.mark.parametrize("raw", ["0798123048", "254***048", "2547981***048", "254798**048", "254798***04", "abc***123"])
def test_parse_phone_hint_rejects_other_shapes(raw):
    with pytest.raises(NormalizationError) as exc:
        parse_phone_hint(raw)
    assert exc.value.code == "bad_phone_hint"
    assert exc.value.value is None  # the text is never kept in the error


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("254798***048", "254798***048", True),
        ("254798***048", "2547***048", True),  # long vs short: compare the shared prefix
        ("2547***048", "254798***048", True),
        ("254798***048", "254711***048", False),
        ("254798***048", "254798***049", False),
        ("2547***048", "2541***048", False),
        ("254798***048", "not a hint", False),
    ],
)
def test_hints_compatible(a, b, expected):
    assert hints_compatible(a, b) is expected
