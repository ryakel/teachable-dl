"""Filename handling -- upstream #37 (non-latin titles) and #22 (illegal characters)."""

import pytest

from teachable_dl.utils import (
    clean_string,
    lecture_basename,
    truncate_title_to_fit_file_name,
    truncate_to_byte_budget,
)


@pytest.mark.parametrize(
    "title",
    ["Привет мир", "日本語のレッスン", "한국어 강의", "Ελληνικά", "Español: acción"],
)
def test_non_latin_titles_survive(title):
    """#37: the old encode('ascii', 'ignore') destroyed these entirely."""
    cleaned = clean_string(title)
    assert cleaned != "untitled"
    # At least one character from the original alphabet must remain.
    assert any(char in cleaned for char in title if char.isalnum())


def test_illegal_characters_are_replaced():
    assert clean_string('a<b>c:d"e/f\\g|h?i*j') == "a-b-c-d-e-f-g-h-i-j"


def test_control_characters_are_dropped():
    assert "\x00" not in clean_string("bad\x00name")
    assert "\x1f" not in clean_string("bad\x1fname")


def test_whitespace_collapses_to_single_dash():
    assert clean_string("a   b\t\tc\n\nd") == "a-b-c-d"


def test_trailing_dots_and_spaces_are_stripped():
    """Windows silently drops these, which broke 'does this file exist' checks."""
    assert clean_string("lecture one.  ") == "lecture-one"


@pytest.mark.parametrize("reserved", ["CON", "PRN", "AUX", "NUL", "COM1", "LPT9"])
def test_windows_reserved_device_names_are_escaped(reserved):
    assert clean_string(reserved) == "_" + reserved
    assert clean_string(reserved.lower() + ".txt").startswith("_")


def test_empty_input_gets_a_placeholder():
    assert clean_string("") == "untitled"
    assert clean_string("   ") == "untitled"
    assert clean_string(None) == "untitled"


def test_ascii_mode_transliterates_when_it_can():
    assert clean_string("Café Münster", ascii_only=True) == "Cafe-Munster"


def test_ascii_mode_keeps_the_original_when_nothing_survives():
    """Folding pure CJK to ASCII yields nothing, so keep something usable."""
    assert clean_string("日本語", ascii_only=True) == "日本語"


def test_truncation_is_byte_aware_and_does_not_split_characters():
    long_title = "я" * 300
    truncated = truncate_to_byte_budget(long_title, budget=50)
    assert len(truncated.encode("utf-8")) <= 50
    # Decoding must round-trip -- a naive slice would leave a dangling byte.
    truncated.encode("utf-8").decode("utf-8")


def test_short_titles_are_left_alone():
    assert truncate_to_byte_budget("short") == "short"


def test_truncation_leaves_room_for_the_yt_dlp_suffix():
    result = truncate_title_to_fit_file_name("x" * 400)
    full_name = f"01-{result}.mp4.part-Frag0000.part"
    assert len(full_name.encode("utf-8")) <= 255


def test_lecture_basename_is_zero_padded():
    assert lecture_basename(3, "Intro") == "03-Intro"
    assert lecture_basename(12, "Intro") == "12-Intro"
