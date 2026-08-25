"""Reusing an existing browser session instead of logging in."""

import pytest

from teachable_dl.config import Settings
from teachable_dl.cookies import (
    CookieError,
    cookie_matches_host,
    cookies_for_host,
    load_cookie_jar,
    to_selenium_cookie,
)


class FakeCookie:
    def __init__(self, name, domain, value="v", path="/", secure=True, expires=None):
        self.name = name
        self.value = value
        self.domain = domain
        self.path = path
        self.secure = secure
        self.expires = expires


# ---------------------------------------------------------- domain matching

@pytest.mark.parametrize(
    "domain,host,expected",
    [
        (".teachable.com", "flightinsight.teachable.com", True),
        ("teachable.com", "flightinsight.teachable.com", True),
        ("flightinsight.teachable.com", "flightinsight.teachable.com", True),
        ("sso.teachable.com", "flightinsight.teachable.com", False),
        ("evil.com", "flightinsight.teachable.com", False),
        ("", "flightinsight.teachable.com", False),
        (".teachable.com", "", False),
        # Must not match a lookalike registered elsewhere.
        ("teachable.com", "notteachable.com", False),
    ],
)
def test_cookie_domain_scoping(domain, host, expected):
    assert cookie_matches_host(domain, host) is expected


def test_only_applicable_cookies_are_selected():
    jar = [
        FakeCookie("session", ".teachable.com"),
        FakeCookie("school", "flightinsight.teachable.com"),
        FakeCookie("other", "example.com"),
    ]
    names = [c.name for c in cookies_for_host(jar, "flightinsight.teachable.com")]
    assert names == ["session", "school"]


# ------------------------------------------------------------- conversion

def test_a_cookie_converts_to_what_selenium_expects():
    payload = to_selenium_cookie(FakeCookie("s", ".teachable.com", expires=1893456000))
    assert payload["name"] == "s"
    assert payload["domain"] == ".teachable.com"
    assert payload["secure"] is True
    assert payload["expiry"] == 1893456000


def test_a_session_cookie_carries_no_expiry():
    """A cookie with no expiry is a session cookie; sending expiry=None is rejected."""
    assert "expiry" not in to_selenium_cookie(FakeCookie("s", ".teachable.com"))


# ------------------------------------------------------------------ loading

def test_no_cookie_source_configured_returns_nothing():
    assert load_cookie_jar(Settings()) is None


def test_a_missing_cookie_file_is_reported_clearly(tmp_path):
    settings = Settings(cookies_file=str(tmp_path / "nope.txt"))
    with pytest.raises(CookieError) as caught:
        load_cookie_jar(settings)
    assert "No cookie file" in str(caught.value)


def test_a_real_netscape_cookie_file_loads(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        ".teachable.com\tTRUE\t/\tTRUE\t1893456000\t_session\tabc123\n",
        encoding="utf-8",
    )
    jar = load_cookie_jar(Settings(cookies_file=str(path)))
    names = [c.name for c in jar]
    assert "_session" in names


def test_a_file_that_is_not_a_cookie_file_is_reported_clearly(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text("this is not a cookie file at all", encoding="utf-8")
    with pytest.raises(CookieError) as caught:
        load_cookie_jar(Settings(cookies_file=str(path)))
    assert "Netscape" in str(caught.value)


def test_an_unknown_browser_lists_the_supported_ones():
    with pytest.raises(CookieError) as caught:
        load_cookie_jar(Settings(cookies_from_browser="nyetscape"))
    message = str(caught.value)
    assert "chrome" in message and "firefox" in message
