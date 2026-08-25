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

from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
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

def _phrase_xpath(phrase):
    """Case-insensitive match on a link or button's visible text."""
    upper, lower = "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
    return (
        f"//a[contains(translate(., '{upper}', '{lower}'), '{phrase}')]"
        f" | //button[contains(translate(., '{upper}', '{lower}'), '{phrase}')]"
    )


#: "Log in with Teachable" is Teachable's own single sign-on, not a third-party
#: social login: it leads to a password form on sso.teachable.com. A school that
#: uses it usually still renders its own email/password form on the same page,
#: and that form is not the one the account lives in -- filling it in submits
#: successfully and leaves the browser anonymous.
SSO_LOGIN_SELECTORS = [
    (By.CSS_SELECTOR, "a[href*='sso.teachable.com']"),
    (By.CSS_SELECTOR, "a[href*='/secure/'][href*='identity']"),
    (By.XPATH, _phrase_xpath("log in with teachable")),
    (By.XPATH, _phrase_xpath("sign in with teachable")),
    (By.XPATH, _phrase_xpath("continue with teachable")),
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

#: A live session shows a way out of it. Any of these means we are logged in.
LOGGED_IN_SELECTORS = [
    (By.CSS_SELECTOR, "a[href*='/sign_out']"),
    (By.CSS_SELECTOR, "a[href*='/logout']"),
    (By.CSS_SELECTOR, "form[action*='/sign_out']"),
    (By.CSS_SELECTOR, "[data-testid*='user-menu']"),
    (By.CSS_SELECTOR, ".user-avatar, .current-user, #current-user"),
]

#: Deliberately narrow. A signed-in Teachable page routinely links to sign-up
#: and enrolment for *other* courses, so treating those as proof of being
#: anonymous invents failures on a perfectly good session. Only an actual login
#: form counts.
LOGGED_OUT_SELECTORS = [
    (By.CSS_SELECTOR, "form[action*='/sign_in']"),
    (By.CSS_SELECTOR, "form[action*='/identity/login']"),
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
        """Log in ahead of downloading ``course_url``, and confirm it worked."""
        settings = self.settings

        if settings.cookies_file or settings.cookies_from_browser:
            self._authenticate_with_cookies(course_url)
        elif settings.man_login_url:
            self._manual_login(course_url, settings.man_login_url)
        else:
            if settings.login_url:
                self.browser.get(settings.login_url)
            else:
                self.find_login(course_url)

            self.browser.handle_cloudflare_if_present()
            self.follow_sso_if_offered()
            self.login(settings.email, settings.password)

        # Submitting a form and seeing no error is not proof of a session.
        # Without this check the run continued unauthenticated, scraped only the
        # free preview lectures, and reported success the whole way.
        self.verify_logged_in(course_url)

    def follow_sso_if_offered(self):
        """Take the "Log in with Teachable" route when the page offers it.

        Schools that use Teachable's single sign-on typically still render their
        own email/password form beside the SSO button. That local form belongs to
        a different account system, so filling it in appears to succeed and
        leaves the browser signed out -- which is exactly how a run ends up
        downloading only free preview lectures.
        """
        if "sso.teachable.com" in (self.browser.current_url or ""):
            logger.debug("Already on Teachable's single sign-on")
            return False

        button = self.browser.find_first(SSO_LOGIN_SELECTORS, timeout=3)
        if button is None:
            return False

        logger.info("Following the 'Log in with Teachable' route")
        try:
            button.click()
        except WebDriverException as exc:
            if is_dead_session_error(exc):
                raise SessionLostError(str(exc)) from exc
            try:
                self.browser.driver.execute_script("arguments[0].click();", button)
            except WebDriverException:
                logger.warning("Could not click the single sign-on button")
                return False

        self.browser.wait_for_body()
        self.browser.handle_cloudflare_if_present()
        logger.info("Now at %s", self.browser.current_url)
        return True

    def verify_logged_in(self, course_url):
        """Sanity-check the session without getting in the way.

        An earlier version navigated to the course URL to run this check, which
        threw away a perfectly good page, followed whatever redirect the school
        felt like serving, and then judged *that*. On a school whose course URL
        bounces to its sales page it reported a working session as broken.

        So: judge the page we are already on, and only refuse to continue when
        the browser is unambiguously sitting on a login form. Anything less
        certain is a warning -- a false failure here is worse than a late one,
        because the curriculum parse that follows will show the truth anyway.
        """
        current = self.browser.current_url or ""

        if looks_like_login_page(current):
            raise LoginError(
                f"The browser is sitting on a sign-in page: {current!r}\n"
                "  Whatever was tried did not produce a session, so only free "
                "preview lectures would be downloaded.\n"
                "  If your school signs you in through 'Log in with Teachable', "
                "a social login or emailed codes, the surest routes are:\n"
                "    --cookies FILE            reuse a session already in your browser\n"
                f"    --man_login_url '{course_url}'   sign in by hand"
            )

        if self.browser.find_first(LOGGED_IN_SELECTORS, timeout=2) is not None:
            logger.info("Confirmed an authenticated session")
            return True

        if self.browser.find_first(LOGGED_OUT_SELECTORS, timeout=1) is not None:
            raise LoginError(
                f"A sign-in form is showing on {current!r}, so the browser is "
                "not logged in.\n"
                "  Only free preview lectures would be downloaded."
            )

        logger.info(
            "Could not positively confirm the session from this page. Continuing; "
            "if the lecture count looks short, you are probably not logged in."
        )
        return False

    def _authenticate_with_cookies(self, course_url):
        """Adopt a session exported from a browser, rather than logging in."""
        from .cookies import CookieError, apply_to_browser, load_cookie_jar

        try:
            jar = load_cookie_jar(self.settings)
            apply_to_browser(self.browser, jar, course_url)
        except CookieError as exc:
            raise LoginError(str(exc)) from exc

    def _manual_login(self, start_url, man_login_url):
        """Wait for a human to sign in, however their school does it.

        This is the escape hatch for schools where the automated form does not
        apply at all: "Log in with Teachable", a social login, or any single
        sign-on. Nothing here touches credentials -- the person signs in in the
        browser window and we simply wait for a session to appear.
        """
        self.browser.get(start_url)

        logger.info(
            "Waiting for you to log in. Sign in however you normally do in the "
            "browser window that just opened."
        )
        if man_login_url != start_url:
            logger.info("Downloading starts once you reach: %s", man_login_url)

        deadline = time.time() + self.settings.manual_login_timeout
        while time.time() < deadline:
            time.sleep(3)
            self.browser.ensure_alive()
            current = self.browser.current_url

            if current == man_login_url or current.startswith(man_login_url):
                logger.info("Reached %s", man_login_url)
                return

            # An SSO round trip often lands somewhere slightly different from
            # the URL that was asked for, so a live session counts too.
            if self.browser.find_first(LOGGED_IN_SELECTORS, timeout=0) is not None:
                logger.info("Detected a signed-in session at %s", current)
                return

            logger.debug("Still waiting. Current url: %s", current)

        raise LoginError(
            f"Gave up waiting for a manual login after "
            f"{self.settings.manual_login_timeout:.0f}s. Raise the limit with "
            "--manual-login-timeout if you need longer."
        )

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

        has_email = self._field_present(EMAIL_SELECTORS)
        has_password = self._field_present(PASSWORD_SELECTORS)

        if not has_email and not has_password:
            raise LoginError(
                "Could not find a login form on "
                f"{self.browser.current_url!r}. Pass --login_url with the direct "
                "URL of the sign-in page, or use --man_login_url to log in by hand."
            )

        if has_email:
            self._fill_field(EMAIL_SELECTORS, email, "email")

        # Some SSO flows ask for the email first and reveal the password field
        # only after that form is submitted.
        if not self._field_present(PASSWORD_SELECTORS, timeout=2):
            logger.info("Password field not shown yet, submitting the email step")
            self._submit()
            if not self._field_present(PASSWORD_SELECTORS):
                raise LoginError("Password field never appeared after submitting the email")

        self._fill_field(PASSWORD_SELECTORS, password, "password")
        self._submit()

        if self._has_credential_error():
            raise LoginError("Login failed: incorrect email or password")

        self.handle_otp_challenge()

        logger.info("Logged in")
        time.sleep(2)

    def _field_present(self, selectors, timeout=None):
        wait = self.settings.timeout if timeout is None else timeout
        return self.browser.find_first(selectors, timeout=wait) is not None

    def _fill_field(self, selectors, value, label, attempts=4):
        """Locate the field and fill it, re-locating if the page re-renders.

        Holding an element across a re-render is what produced
        ``stale element reference`` here: the login page settles after the
        Cloudflare check clears and React re-mounts the form, invalidating any
        reference taken beforehand. So the element is looked up again on every
        attempt rather than reused.
        """
        last_error = None
        for attempt in range(1, attempts + 1):
            element = self.browser.find_first(selectors, timeout=self.settings.timeout)
            if element is None:
                raise LoginError(
                    f"Could not find the {label} field on "
                    f"{self.browser.current_url!r}"
                )
            try:
                self._fill(element, value)
                return
            except StaleElementReferenceException as exc:
                last_error = exc
                logger.debug(
                    "The %s field went stale, re-locating (attempt %s/%s)",
                    label, attempt, attempts,
                )
                time.sleep(0.5)

        raise LoginError(
            f"The {label} field kept being replaced while filling it in "
            f"({last_error})"
        )

    def _fill(self, element, value):
        """Type into a field the way a human would, so React/Stimulus notice.

        Raises ``StaleElementReferenceException`` rather than swallowing it:
        that exception is a ``WebDriverException`` subclass, so the previous
        blanket handling here hid the one failure the caller can actually
        recover from by looking the element up again.
        """
        try:
            element.click()
        except StaleElementReferenceException:
            raise
        except WebDriverException:
            pass
        try:
            element.clear()
        except StaleElementReferenceException:
            raise
        except WebDriverException:
            pass
        try:
            element.send_keys(value)
        except StaleElementReferenceException:
            raise
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

    def _submit(self, attempts=4):
        """Submit the form, re-locating the button if the page re-renders."""
        for attempt in range(1, attempts + 1):
            submit = self.browser.find_first(SUBMIT_SELECTORS, timeout=self.settings.timeout)
            if submit is None:
                # Plenty of login forms have no button at all and submit on
                # Enter, so try that before giving up.
                if self._submit_by_keyboard():
                    return
                raise LoginError(
                    "Could not find a submit button, and pressing Enter in the "
                    f"form did nothing, on {self.browser.current_url!r}"
                )
            try:
                submit.click()
                self.browser.wait_for_body()
                return
            except StaleElementReferenceException:
                logger.debug(
                    "The submit button went stale, re-locating (attempt %s/%s)",
                    attempt, attempts,
                )
                time.sleep(0.5)
            except WebDriverException as exc:
                if is_dead_session_error(exc):
                    raise SessionLostError(str(exc)) from exc
                try:
                    self.browser.driver.execute_script("arguments[0].click();", submit)
                    self.browser.wait_for_body()
                    return
                except StaleElementReferenceException:
                    time.sleep(0.5)

        raise LoginError("The submit button kept being replaced before it could be clicked")

    def _submit_by_keyboard(self):
        """Press Enter in the password (or email) field to submit the form."""
        from selenium.webdriver.common.keys import Keys

        for selectors in (PASSWORD_SELECTORS, EMAIL_SELECTORS):
            element = self.browser.find_first(selectors, timeout=2)
            if element is None:
                continue
            try:
                element.send_keys(Keys.RETURN)
                self.browser.wait_for_body()
                logger.debug("Submitted the form with the Enter key")
                return True
            except WebDriverException as exc:
                if is_dead_session_error(exc):
                    raise SessionLostError(str(exc)) from exc
        return False

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

    def _wait_for_otp_to_clear(self, timeout=None):
        """Wait until the OTP form goes away, meaning the code was accepted.

        The previous check slept two seconds and then asked whether the OTP
        input was still on screen. It always is until the form POST completes,
        so any verification round-trip slower than that reported a perfectly
        good code as rejected and killed the run.
        """
        deadline = time.time() + (timeout or max(self.settings.timeout * 3, 30))
        starting_url = self.browser.current_url

        while time.time() < deadline:
            time.sleep(1)
            try:
                self.browser.ensure_alive()
            except SessionLostError:
                raise
            if self.browser.current_url != starting_url:
                logger.debug("Navigated away from the OTP form")
                return True
            if self.browser.find_first(OTP_SELECTORS, timeout=0) is None:
                logger.debug("OTP form is gone")
                return True

        return False

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

            if not self._wait_for_otp_to_clear():
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
            if not self._wait_for_otp_to_clear():
                logger.warning(
                    "The one-time password form is still showing; the code may have "
                    "been wrong or the page may still be loading."
                )
