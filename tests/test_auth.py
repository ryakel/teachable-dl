"""Login handling -- upstream #56 (accounts that require an OTP)."""

import pytest
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.remote.webdriver import By

from tests.conftest import FakeElement, make_browser
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


# --------------------------------------------------- stale element handling

class StaleOnceElement(FakeElement):
    """Goes stale the first time it is touched, as a re-render would cause."""

    def __init__(self, stale_times=1):
        super().__init__()
        self.remaining_stale = stale_times

    def click(self):
        if self.remaining_stale:
            self.remaining_stale -= 1
            raise StaleElementReferenceException("stale element not found")
        super().click()


def _authenticator(mapping, **settings_kwargs):
    from teachable_dl.auth import Authenticator

    browser = make_browser(mapping, **settings_kwargs)
    return Authenticator(browser, browser.settings), browser


def test_a_field_that_goes_stale_is_relocated_and_filled():
    """Upstream #19: the login page re-renders once Cloudflare clears, which
    invalidates any element reference taken beforehand."""
    fresh = FakeElement()
    elements = [StaleOnceElement(), fresh]

    class Cycling(dict):
        def get(self, key, default=None):
            # Hand out the stale element first, then a fresh one.
            return [elements[0]] if elements[0].remaining_stale else [fresh]

    auth, browser = _authenticator({})
    browser.driver.mapping = Cycling()
    auth._fill_field(EMAIL_SELECTORS, "a@b.c", "email")
    assert fresh.value == "a@b.c"


def test_a_permanently_stale_field_reports_a_clear_error():
    always_stale = StaleOnceElement(stale_times=99)
    auth, browser = _authenticator(
        {(By.ID, "email"): [always_stale]}
    )
    with pytest.raises(LoginError) as caught:
        auth._fill_field(EMAIL_SELECTORS, "a@b.c", "email", attempts=2)
    assert "kept being replaced" in str(caught.value)


def test_fill_lets_stale_propagate_rather_than_swallowing_it():
    """StaleElementReferenceException is a WebDriverException, so blanket
    handling hid the one error the caller can recover from."""
    auth, _ = _authenticator({})
    with pytest.raises(StaleElementReferenceException):
        auth._fill(StaleOnceElement(), "value")


def test_a_form_with_no_button_is_submitted_with_the_enter_key():
    """Plenty of login forms carry no submit button and submit on Enter."""
    from selenium.webdriver.common.keys import Keys

    field = FakeElement()
    auth, _ = _authenticator({(By.CSS_SELECTOR, "input[type='password']"): [field]})
    auth._submit()
    assert field.value == Keys.RETURN


# ------------------------------------------------------ login verification

def test_an_actual_sign_in_form_means_no_session():
    """A real login form is the only page-content signal worth failing on."""
    from teachable_dl.auth import LoginError

    auth, _ = _authenticator({(By.CSS_SELECTOR, "form[action*='/sign_in']"): [FakeElement()]})
    with pytest.raises(LoginError) as caught:
        auth.verify_logged_in("https://school.teachable.com/courses/enrolled/1")
    assert "not logged in" in str(caught.value)


def test_links_to_sign_up_or_enrol_do_not_condemn_a_good_session():
    """A signed-in Teachable page links to enrolment for other courses. Reading
    that as proof of being anonymous failed a session that was working."""
    auth, browser = _authenticator({
        (By.CSS_SELECTOR, "a[href*='/sign_up']"): [FakeElement()],
        (By.CSS_SELECTOR, "a[href*='/enroll']"): [FakeElement()],
    })
    browser.driver.current_url = "https://school.teachable.com/courses/enrolled/1"
    assert auth.verify_logged_in("https://school.teachable.com/courses/enrolled/1") is False


def test_verification_does_not_navigate_away_from_a_working_page():
    """Re-navigating threw away the logged-in page and followed a redirect to
    the school's sales page, then judged that instead."""
    auth, browser = _authenticator({})
    browser.driver.current_url = "https://school.teachable.com/courses/enrolled/1"
    auth.verify_logged_in("https://school.teachable.com/courses/enrolled/1")
    assert browser.driver.current_url == "https://school.teachable.com/courses/enrolled/1"


def test_sitting_on_a_login_page_still_fails_loudly():
    from teachable_dl.auth import LoginError

    auth, browser = _authenticator({})
    browser.driver.current_url = "https://sso.teachable.com/secure/1/identity/login/password"
    with pytest.raises(LoginError) as caught:
        auth.verify_logged_in("https://school.teachable.com/courses/enrolled/1")
    assert "sign-in page" in str(caught.value)


def test_a_sign_out_link_confirms_the_session():
    auth, _ = _authenticator({(By.CSS_SELECTOR, "a[href*='/sign_out']"): [FakeElement()]})
    assert auth.verify_logged_in("https://school.teachable.com/courses/enrolled/1") is True


def test_an_ambiguous_page_warns_rather_than_guessing():
    """Neither marker present: continue, but say so, since a false failure here
    would be as unhelpful as a false success."""
    auth, _ = _authenticator({})
    assert auth.verify_logged_in("https://school.teachable.com/courses/enrolled/1") is False


def test_the_advice_names_the_routes_that_actually_work():
    from teachable_dl.auth import LoginError

    auth, browser = _authenticator({})
    browser.driver.current_url = "https://sso.teachable.com/secure/1/identity/login/password"
    with pytest.raises(LoginError) as caught:
        auth.verify_logged_in("https://school.teachable.com/courses/enrolled/1")
    message = str(caught.value)
    assert "--cookies" in message and "--man_login_url" in message
