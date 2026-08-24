"""Login, including SSO pages and one-time-password challenges.

Upstream issue #56 reports ``Login button not found, navigating to fallback URL``
followed by ``Could not login: Message: invalid session id`` on an account that
requires an OTP.  Three separate problems fed into that:

* The login form was located purely by ``id=email`` / ``id=password`` /
  ``name=commit``.  Teachable's SSO pages
  (``sso.teachable.com/secure/<id>/identity/login/password``) use different
  markup, so nothing was found and the driver was left in a broken state.
* Credentials were injected with
  ``execute_script("document.getElementById('email').value='" + email + "'")``.
  Any quote or backslash in the password produced a JavaScript syntax error, and
  assigning ``.value`` directly does not fire the events React listens for, so
  the form often submitted empty.
* The OTP prompt only ever blocked on ``input()``; there was no way to automate
  it, and no detection for the other field names Teachable uses.
"""

import logging
import re
import time
from urllib.parse import urlparse, urlunparse

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.remote.webdriver import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from .browser import SessionLostError, is_dead_session_error

logger = logging.getLogger(__name__)

EMAIL_SELECTORS = [
    (By.ID, "email"),
    (By.NAME, "email"),
    (By.NAME, "user[email]"),
    (By.CSS_SELECTOR, "input[type='email']"),
    (By.CSS_SELECTOR, "input[autocomplete='username']"),
    (By.CSS_SELECTOR, "input[name*='email' i]"),
]

PASSWORD_SELECTORS = [
    (By.ID, "password"),
    (By.NAME, "password"),
    (By.NAME, "user[password]"),
    (By.CSS_SELECTOR, "input[type='password']"),
    (By.CSS_SELECTOR, "input[autocomplete='current-password']"),
]

SUBMIT_SELECTORS = [
    (By.NAME, "commit"),
    (By.CSS_SELECTOR, "button[type='submit']"),
    (By.CSS_SELECTOR, "input[type='submit']"),
    (By.CSS_SELECTOR, "form button"),
]

OTP_SELECTORS = [
    (By.NAME, "otp_code"),
    (By.ID, "otp_code"),
    (By.CSS_SELECTOR, "input[autocomplete='one-time-code']"),
    (By.CSS_SELECTOR, "input[name*='otp' i]"),
    (By.CSS_SELECTOR, "input[name*='two_factor' i]"),
    (By.CSS_SELECTOR, "input[id*='verification' i]"),
]

LOGIN_LINK_SELECTORS = [
    (By.LINK_TEXT, "Login"),
    (By.LINK_TEXT, "Log In"),
    (By.LINK_TEXT, "Sign In"),
    (By.PARTIAL_LINK_TEXT, "Log in"),
    (By.PARTIAL_LINK_TEXT, "Sign in"),
    (By.CSS_SELECTOR, "a[href*='/sign_in']"),
    (By.CSS_SELECTOR, "a[href*='/login']"),
]

BAD_CREDENTIALS_MARKERS = (
    "your email or password is incorrect",
    "invalid email or password",
    "incorrect email or password",
)


class LoginError(RuntimeError):
    """Authentication failed for a reason retrying will not fix."""


def construct_sign_in_url(course_url):
    """Best-effort guess at the sign-in page for a course URL."""
    parsed = urlparse(course_url)
    return urlunparse((parsed.scheme, parsed.netloc, "/sign_in", "", "", ""))


def looks_like_login_page(url):
    """Is this URL already a login form? Then don't hunt for a 'Login' link."""
    lowered = (url or "").lower()
    return any(
        marker in lowered
        for marker in ("/sign_in", "/login", "/identity/login", "sso.teachable.com")
    )


def generate_totp(secret):
    """Turn a base32 TOTP secret into the current 6-digit code."""
    try:
        import pyotp
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise LoginError(
            "--totp-secret needs the 'pyotp' package: pip install pyotp"
        ) from exc
    normalised = re.sub(r"\s+", "", secret).upper()
    try:
        return pyotp.TOTP(normalised).now()
    except Exception as exc:
        raise LoginError(f"Could not derive a TOTP code from the given secret: {exc}") from exc


class Authenticator:
    def __init__(self, browser, settings):
        self.browser = browser
        self.settings = settings

    # ------------------------------------------------------------------ flow

    def authenticate(self, course_url):
        """Log in ahead of downloading ``course_url``."""
        settings = self.settings

        if settings.man_login_url:
            return self._manual_login(course_url, settings.man_login_url)

        if settings.login_url:
            self.browser.get(settings.login_url)
        else:
            self.find_login(course_url)

        self.browser.handle_cloudflare_if_present()
        self.login(settings.email, settings.password)

    def _manual_login(self, start_url, man_login_url):
        self.browser.get(start_url)
        logger.info("Waiting for you to log in manually; target url: %s", man_login_url)
        while True:
            self.browser.ensure_alive()
            current = self.browser.current_url
            if current == man_login_url or current.startswith(man_login_url):
                logger.info("Reached %s, continuing", man_login_url)
                return
            logger.info("Still waiting. Current url: %s", current)
            time.sleep(3)

    def find_login(self, course_url):
        logger.info("Trying to find the login page")
        self.browser.get(course_url)

        if looks_like_login_page(self.browser.current_url):
            logger.info("Already on a login page")
            return

        login_element = self.browser.find_first(LOGIN_LINK_SELECTORS, timeout=self.settings.timeout)
        if login_element is None:
            fallback_url = construct_sign_in_url(course_url)
            logger.warning("Login link not found, navigating to fallback URL %s", fallback_url)
            self.browser.get(fallback_url)
            return

        try:
            login_element.click()
            self.browser.wait_for_body()
        except WebDriverException as exc:
            if is_dead_session_error(exc):
                raise SessionLostError(str(exc)) from exc
            fallback_url = construct_sign_in_url(course_url)
            logger.warning("Clicking the login link failed (%s), using %s", exc, fallback_url)
            self.browser.get(fallback_url)

    # ------------------------------------------------------------------ form

    def login(self, email, password):
        if not email or not password:
            raise LoginError("Email and password are required for automatic login")

        logger.info("Logging in")
        self.browser.ensure_alive()
        self.browser.wait_for_body()

        email_element = self.browser.find_first(EMAIL_SELECTORS, timeout=self.settings.timeout)
        password_element = self.browser.find_first(PASSWORD_SELECTORS, timeout=self.settings.timeout)

        if email_element is None and password_element is None:
            raise LoginError(
                "Could not find a login form on "
                f"{self.browser.current_url!r}. Pass --login_url with the direct "
                "URL of the sign-in page, or use --man_login_url to log in by hand."
            )

        if email_element is not None:
            self._fill(email_element, email)

        # Some SSO flows ask for the email first and reveal the password field
        # only after that form is submitted.
        if password_element is None:
            logger.info("Password field not shown yet, submitting the email step")
            self._submit()
            password_element = self.browser.find_first(
                PASSWORD_SELECTORS, timeout=self.settings.timeout
            )
            if password_element is None:
                raise LoginError("Password field never appeared after submitting the email")

        self._fill(password_element, password)
        self._submit()

        if self._has_credential_error():
            raise LoginError("Login failed: incorrect email or password")

        self.handle_otp_challenge()

        logger.info("Logged in")
        time.sleep(2)

    def _fill(self, element, value):
        """Type into a field the way a human would, so React/Stimulus notice."""
        try:
            element.click()
        except WebDriverException:
            pass
        try:
            element.clear()
        except WebDriverException:
            pass
        try:
            element.send_keys(value)
        except WebDriverException as exc:
            if is_dead_session_error(exc):
                raise SessionLostError(str(exc)) from exc
            # Last resort: set the value via the DOM, but pass the string as an
            # argument instead of splicing it into the script source.
            logger.debug("send_keys failed (%s), falling back to a DOM assignment", exc)
            self.browser.driver.execute_script(
                "arguments[0].value = arguments[1];"
                "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));"
                "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
                element,
                value,
            )

    def _submit(self):
        submit = self.browser.find_first(SUBMIT_SELECTORS, timeout=self.settings.timeout)
        if submit is None:
            raise LoginError("Could not find the submit button on the login form")
        try:
            submit.click()
        except WebDriverException as exc:
            if is_dead_session_error(exc):
                raise SessionLostError(str(exc)) from exc
            self.browser.driver.execute_script("arguments[0].click();", submit)
        self.browser.wait_for_body()

    def _has_credential_error(self):
        try:
            elements = WebDriverWait(self.browser.driver, 5).until(
                EC.presence_of_all_elements_located(
                    (By.CSS_SELECTOR, "div.toast, span.text-with-icon, .alert, .form-error")
                )
            )
        except TimeoutException:
            return False
        except WebDriverException as exc:
            if is_dead_session_error(exc):
                raise SessionLostError(str(exc)) from exc
            return False

        for element in elements:
            try:
                text = element.text.lower()
            except WebDriverException:
                continue
            if any(marker in text for marker in BAD_CREDENTIALS_MARKERS):
                return True
        return False

    # ------------------------------------------------------------------- OTP

    def handle_otp_challenge(self):
        """Fill in a one-time password, automatically when a TOTP secret is set."""
        otp_element = self.browser.find_first(OTP_SELECTORS, timeout=5)
        if otp_element is None:
            logger.debug("No OTP challenge")
            return

        logger.info("An one-time password is required")

        if self.settings.totp_secret:
            code = generate_totp(self.settings.totp_secret)
            logger.info("Filling in the generated TOTP code")
            self._fill(otp_element, code)
            try:
                self._submit()
            except LoginError:
                # Some OTP forms submit themselves once the last digit lands.
                logger.debug("No submit button on the OTP form; assuming auto-submit")
            time.sleep(2)
            if self.browser.find_first(OTP_SELECTORS, timeout=5) is not None:
                raise LoginError(
                    "The generated TOTP code was rejected. Check that --totp-secret "
                    "matches the account and that your clock is accurate."
                )
            return

        code = input(
            "\033[93mA one-time password is required.\n"
            "Enter the code (from your email or authenticator app) and press enter, "
            "or just press enter if you already submitted it in the browser: \033[0m"
        ).strip()

        if code:
            self._fill(otp_element, code)
            try:
                self._submit()
            except LoginError:
                logger.debug("No submit button on the OTP form; assuming auto-submit")
            time.sleep(2)
