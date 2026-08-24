"""Login handling -- upstream #56 (accounts that require an OTP)."""

import pytest
from selenium.webdriver.remote.webdriver import By

from tests.conftest import FakeElement
from teachable_dl.auth import (
    EMAIL_SELECTORS,
    OTP_SELECTORS,
    PASSWORD_SELECTORS,
    LoginError,
    construct_sign_in_url,
    generate_totp,
    looks_like_login_page,
)


def test_sign_in_url_is_built_from_the_course_host():
    assert (
        construct_sign_in_url("https://school.teachable.com/courses/enrolled/123")
        == "https://school.teachable.com/sign_in"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://sso.teachable.com/secure/156164/identity/login/password?force=true",
        "https://school.teachable.com/sign_in",
        "https://school.com/login",
    ],
)
def test_login_pages_are_detected_so_we_do_not_hunt_for_a_login_link(url):
    """#56: on an SSO page there is no 'Login' link, and the old code gave up."""
    assert looks_like_login_page(url)


def test_a_course_page_is_not_mistaken_for_a_login_page():
    assert not looks_like_login_page("https://school.teachable.com/courses/enrolled/123")
    assert not looks_like_login_page("")
    assert not looks_like_login_page(None)


def test_an_sso_login_form_is_actually_found(browser_factory):
    """#56: SSO pages carry none of id=email / id=password / name=commit.

    This drives the real lookup against a fake DOM rather than asserting that a
    string appears in the selector list, so it fails if the lookup logic breaks
    even when the constants are untouched.
    """
    email = FakeElement()
    password = FakeElement()
    browser = browser_factory(
        {
            (By.CSS_SELECTOR, "input[type='email']"): [email],
            (By.CSS_SELECTOR, "input[type='password']"): [password],
        }
    )
    assert browser.find_first(EMAIL_SELECTORS, timeout=0) is email
    assert browser.find_first(PASSWORD_SELECTORS, timeout=0) is password


def test_a_legacy_teachable_login_form_is_still_found(browser_factory):
    email = FakeElement()
    browser = browser_factory({(By.ID, "email"): [email]})
    assert browser.find_first(EMAIL_SELECTORS, timeout=0) is email


def test_a_page_with_no_login_form_yields_nothing(browser_factory):
    assert browser_factory({}).find_first(EMAIL_SELECTORS, timeout=0) is None


def test_a_hidden_field_is_not_treated_as_the_login_form(browser_factory):
    """Teachable renders an off-screen form on some pages; typing into it does nothing."""
    hidden = FakeElement(displayed=False)
    browser = browser_factory({(By.ID, "email"): [hidden]})
    assert browser.find_first(EMAIL_SELECTORS, timeout=0) is None


def test_a_modern_one_time_code_field_is_found(browser_factory):
    """The old code only knew name=otp_code."""
    otp = FakeElement()
    browser = browser_factory(
        {(By.CSS_SELECTOR, "input[autocomplete='one-time-code']"): [otp]}
    )
    assert browser.find_first(OTP_SELECTORS, timeout=0) is otp


def test_the_legacy_otp_field_is_still_found(browser_factory):
    otp = FakeElement()
    browser = browser_factory({(By.NAME, "otp_code"): [otp]})
    assert browser.find_first(OTP_SELECTORS, timeout=0) is otp


def test_totp_codes_are_six_digits():
    code = generate_totp("JBSWY3DPEHPK3PXP")
    assert len(code) == 6 and code.isdigit()


def test_totp_secrets_may_contain_the_spaces_sites_display():
    assert generate_totp("JBSW Y3DP EHPK 3PXP") == generate_totp("JBSWY3DPEHPK3PXP")


def test_an_invalid_totp_secret_reports_a_clear_error():
    with pytest.raises(LoginError):
        generate_totp("not-a-valid-base32-secret!!")
