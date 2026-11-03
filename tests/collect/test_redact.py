from urllib.parse import quote

import pytest

from rebalance.collect import MASK, redact

SPECIAL_KEY = "ab+c/d=e&f"
ENCODED_SPECIAL_KEY = quote(SPECIAL_KEY, safe="")  # ab%2Bc%2Fd%3De%26f

KEY = "TESTKEY123"


# Redact tests


def test_redact_uses_mask_constant() -> None:
    assert redact(f"app_key={KEY}", KEY) == f"app_key={MASK}"


def test_redact_masks_key_inside_plain_text() -> None:
    text = f"Some text with a key: {KEY} inside it."

    result = redact(text, KEY)

    assert KEY not in result
    assert "Some text with a key: " in result


def test_redact_masks_key_inside_url() -> None:
    text = f"GET https://example.com/path?app_key={KEY} failed"

    result = redact(text, KEY)

    assert KEY not in result
    assert "GET https://example.com/path?app_key=" in result


def test_redact_masks_key_twice() -> None:
    text = f"Some text with a key: {KEY} inside it. Another key: {KEY}."

    result = redact(text, KEY)

    assert KEY not in result
    assert "Some text with a key: " in result
    assert "Another key: " in result


def test_redact_masks_plain_and_url_encoded_key() -> None:
    text = (
        f"plain {SPECIAL_KEY} and encoded "
        f"https://example.com/?app_key={ENCODED_SPECIAL_KEY}&x=1 failed"
    )

    result = redact(text, SPECIAL_KEY)

    assert SPECIAL_KEY not in result
    assert ENCODED_SPECIAL_KEY not in result
    assert "failed" in result
    assert "&x=1" in result


def test_redact_masks_incorrect_key_inside_url() -> None:
    incorrect_key = "WRONGKEY456"
    text = f"GET https://example.com/path?app_key={incorrect_key} failed"

    result = redact(text, KEY)

    assert incorrect_key not in result
    assert "GET https://example.com/path?app_key=" in result


def test_redact_does_nothing_if_key_not_present() -> None:
    text = "Some text without the key."

    result = redact(text, KEY)

    assert result == text
    assert KEY not in result


def test_redact_treats_key_literally() -> None:
    key = "a.b*c+d"
    text = f"key {key} and decoy aXbbcd"

    result = redact(text, key)

    assert key not in result
    assert "aXbbcd" in result


def test_redact_masks_keys_in_multi_line_traceback() -> None:
    text = f"""Traceback (most recent call last):
        File "example.py", line 10, in <module>
            raise ValueError("An error occurred with key: {KEY}")
        ValueError: An error occurred with key: {KEY}"""

    result = redact(text, KEY)

    assert KEY not in result
    assert "Traceback (most recent call last):" in result
    assert "ValueError: An error occurred with key: " in result


def test_redact_masks_key_in_url_when_parameter_name_upper_case() -> None:
    text = f"GET https://example.com/path?APP_KEY={KEY} failed"

    result = redact(text, KEY)

    assert KEY not in result
    assert "GET https://example.com/path?APP_KEY=" in result


def test_redact_masks_upper_case_parameter_with_wrong_value() -> None:
    text = "failed: APP_KEY=WRONGKEY456 after"

    result = redact(text, KEY)

    assert result == f"failed: APP_KEY={MASK} after"


def test_redact_masks_key_when_part_of_longer_token() -> None:
    text = f"Some text with a key: {KEY}XYZ inside it."

    result = redact(text, KEY)

    assert KEY not in result
    assert "Some text with a key: " in result


def test_redact_masks_key_in_exception_string() -> None:
    text = f"HTTPError: 403 Client Error: Forbidden for url: https://api.tfl.gov.uk/path?app_key={KEY}"

    result = redact(text, KEY)

    assert KEY not in result
    assert (
        "HTTPError: 403 Client Error: Forbidden for url: https://api.tfl.gov.uk/path?app_key="
        in result
    )


def test_redact_empty_key_leaves_ordinary_text_unchanged() -> None:
    text = "Some ordinary text with no secret in it."

    assert redact(text, "") == text


def test_redact_empty_key_still_masks_app_key_parameter() -> None:
    text = "GET https://example.com/path?app_key=whatever failed"

    result = redact(text, "")

    assert "whatever" not in result
    assert "failed" in result


def test_redact_is_idempotent() -> None:
    text = f"url ?app_key={KEY} and {KEY}"

    once = redact(text, KEY)

    assert redact(once, KEY) == once


def test_redact_masks_key_at_start_of_text() -> None:
    text = f"{KEY} is at the start of this text."

    result = redact(text, KEY)

    assert KEY not in result
    assert "is at the start of this text." in result


def test_redact_masks_key_at_end_of_text() -> None:
    text = f"This text ends with the key: {KEY}"

    result = redact(text, KEY)

    assert KEY not in result
    assert "This text ends with the key: " in result


@pytest.mark.parametrize("terminator", ["'", '"', ")", ",", "&", " ", "\n"])
def test_redact_masks_app_key_value_up_to_terminator(terminator: str) -> None:
    text = f"failed: app_key=WRONGKEY456{terminator}after"

    result = redact(text, KEY)

    assert result == f"failed: app_key={MASK}{terminator}after"
