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


def test_a_permission_error_is_not_blamed_on_a_missing_browser():
    """macOS refuses Safari's cookie store without Full Disk Access. Calling
    that "no profile" sends people to fix the wrong thing."""
    from teachable_dl.cookies import _explain_browser_failure

    message = _explain_browser_failure(
        "safari",
        PermissionError(1, "Operation not permitted", "/Users/x/.../Cookies.binarycookies"),
    )
    assert "Full Disk Access" in message
    assert "has never been signed in" not in message


def test_a_missing_store_is_not_blamed_on_permissions():
    from teachable_dl.cookies import _explain_browser_failure

    message = _explain_browser_failure(
        "chrome", Exception('could not find chrome cookies database in "/x"')
    )
    assert "never been signed in" in message
    assert "Full Disk Access" not in message


def test_every_failure_offers_the_permission_free_route():
    from teachable_dl.cookies import _explain_browser_failure

    for exc in (
        PermissionError(1, "Operation not permitted", "/x"),
        Exception("could not find chrome cookies database"),
        Exception("something else entirely"),
    ):
        assert "--cookies" in _explain_browser_failure("chrome", exc)


def test_a_directory_without_a_cookie_store_is_not_reported_as_available(tmp_path,
                                                                        monkeypatch):
    """A browser installed just for this tool has a directory and no cookies."""
    from teachable_dl import cookies as module

    empty = tmp_path / "Chrome"
    empty.mkdir()
    monkeypatch.setattr(module, "_browser_data_dirs", lambda: {"chrome": str(empty)})
    assert module.available_browsers() == []

    (empty / "Default").mkdir()
    (empty / "Default" / "Cookies").write_bytes(b"")
    assert module.available_browsers() == ["chrome"]


def test_the_browser_error_names_what_is_actually_installed():
    """A browser installed just for this tool has no profile, which is the
    likeliest cause and the least obvious from yt-dlp's own message."""
    from teachable_dl.cookies import describe_available_browsers, load_from_browser

    with pytest.raises(CookieError) as caught:
        load_from_browser("chrome")
    message = str(caught.value)

    assert "\\n" not in message, "escaped newline leaked into the message"
    assert "\n" in message, "the guidance should be on its own lines"
    assert "Browsers with a cookie store here" in message
    assert describe_available_browsers() in message
    assert "browser:profile" in message


def test_available_browsers_only_reports_real_profile_directories():
    from teachable_dl.cookies import available_browsers

    import os
    from teachable_dl.cookies import _browser_data_dirs

    for name in available_browsers():
        assert os.path.isdir(_browser_data_dirs()[name])


def test_setting_names_are_not_split_across_lines():
    """A wrapped 'Full Disk Access' is invisible to anyone searching for it."""
    from teachable_dl.cookies import _explain_browser_failure

    message = _explain_browser_failure("safari", PermissionError(1, "Operation not permitted", "/x"))
    for phrase in ("Full Disk Access", "Privacy & Security"):
        assert phrase in message, f"{phrase!r} should sit on one line"


# ------------------------------------------------- which hosts get seeded

def test_the_sso_host_is_seeded_alongside_the_school():
    """Seeding only the school's host left the browser anonymous: a school using
    'Log in with Teachable' keeps its session cookie on sso.teachable.com."""
    from teachable_dl.cookies import hosts_to_seed

    jar = [FakeCookie("a", ".flightinsight.teachable.com"),
           FakeCookie("b", "sso.teachable.com"),
           FakeCookie("c", ".teachable.com")]
    hosts = hosts_to_seed(jar, "https://flightinsight.teachable.com/courses/enrolled/1")
    assert hosts[0] == "flightinsight.teachable.com"      # school first
    assert "sso.teachable.com" in hosts


def test_a_custom_domain_school_still_gets_the_teachable_sso_host():
    from teachable_dl.cookies import hosts_to_seed

    jar = [FakeCookie("a", "courses.example.com"), FakeCookie("b", "sso.teachable.com")]
    hosts = hosts_to_seed(jar, "https://courses.example.com/courses/enrolled/1")
    assert hosts == ["courses.example.com", "sso.teachable.com"]


def test_unrelated_sites_in_the_jar_are_never_visited():
    """An exported jar covers every site you use; only the school's is relevant."""
    from teachable_dl.cookies import hosts_to_seed

    jar = [FakeCookie("a", "flightinsight.teachable.com"),
           FakeCookie("b", ".google.com"),
           FakeCookie("c", "bank.example.com")]
    hosts = hosts_to_seed(jar, "https://flightinsight.teachable.com/courses/enrolled/1")
    assert hosts == ["flightinsight.teachable.com"]


def test_registrable_domain_grouping():
    from teachable_dl.cookies import registrable_domain

    assert registrable_domain("flightinsight.teachable.com") == "teachable.com"
    assert registrable_domain("teachable.com") == "teachable.com"
    assert registrable_domain("localhost") == "localhost"
