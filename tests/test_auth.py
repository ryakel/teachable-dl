"""Login handling -- upstream #56 (accounts that require an OTP)."""

import pytest

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


def test_sso_login_forms_are_covered_by_the_selectors():
    """The old code only knew id=email / id=password, which SSO pages do not use."""
    email_selectors = [selector for _, selector in EMAIL_SELECTORS]
    password_selectors = [selector for _, selector in PASSWORD_SELECTORS]
    assert "input[type='email']" in email_selectors
    assert "input[type='password']" in password_selectors
    assert len(EMAIL_SELECTORS) > 1 and len(PASSWORD_SELECTORS) > 1


def test_otp_detection_covers_more_than_the_legacy_field_name():
    selectors = [selector for _, selector in OTP_SELECTORS]
    assert "otp_code" in selectors
    assert "input[autocomplete='one-time-code']" in selectors


def test_totp_codes_are_six_digits():
    code = generate_totp("JBSWY3DPEHPK3PXP")
    assert len(code) == 6 and code.isdigit()


def test_totp_secrets_may_contain_the_spaces_sites_display():
    assert generate_totp("JBSW Y3DP EHPK 3PXP") == generate_totp("JBSWY3DPEHPK3PXP")


def test_an_invalid_totp_secret_reports_a_clear_error():
    with pytest.raises(LoginError):
        generate_totp("not-a-valid-base32-secret!!")
