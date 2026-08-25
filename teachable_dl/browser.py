"""Browser lifecycle management.

Upstream issues #51 and #53 both report ``no such window: target window already
closed`` / ``web view not found``.  Two things caused that:

* :meth:`Browser.bypass_cloudflare` opens a second tab and closes the first one.
  If anything went wrong in between, the driver was left pointing at a handle
  that no longer existed and *every* later call raised.
* Long downloads outlive the browser.  Once Chrome dies the driver raises
  ``invalid session id`` forever, and the old code just logged it per lecture.

So every navigation now goes through :meth:`Browser.ensure_alive`, and a dead
session is rebuilt (and re-authenticated by the caller) instead of poisoning the
rest of the run.
"""

import base64
import logging
import os
import sys
import time

from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchElementException,
    NoSuchWindowException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.remote.webdriver import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
from seleniumbase import Driver

logger = logging.getLogger(__name__)

#: How Cloudflare's interstitial shows up in the DOM. Matching only
#: ``#challenge-stage`` (the original check) misses the Turnstile widget and the
#: managed-challenge page, which is how a run ends up looping on "Performing
#: security verification" forever.
CHALLENGE_SELECTORS = [
    (By.ID, "challenge-stage"),
    (By.ID, "challenge-running"),
    (By.ID, "cf-chl-widget"),
    (By.CSS_SELECTOR, "iframe[src*='challenges.cloudflare.com']"),
    (By.CSS_SELECTOR, "[class*='cf-turnstile']"),
    (By.CSS_SELECTOR, "#cf-wrapper, #cf-error-details"),
]

#: Page titles Cloudflare serves while challenging.
CHALLENGE_TITLES = ("just a moment", "attention required", "security verification")

#: Substrings that mean "the browser is gone, stop trying to talk to it".
_DEAD_SESSION_MARKERS = (
    "invalid session id",
    "session deleted",
    "web view not found",
    "target window already closed",
    "no such window",
    "chrome not reachable",
    "disconnected: not connected to devtools",
    "unable to connect to renderer",
)


class SessionLostError(RuntimeError):
    """Raised when the underlying browser session can no longer be used."""


class BrowserNotFoundError(RuntimeError):
    """Google Chrome is not installed, or could not be located."""


class MissingRosettaError(RuntimeError):
    """Apple Silicon Mac without Rosetta 2, which UC Mode needs."""


class BrowserProfileInUseError(RuntimeError):
    """The requested Chrome profile is open in another process."""


def default_chrome_profile():
    """Where Chrome keeps its profiles on this platform."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "Google", "Chrome")
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
    return os.path.join(home, ".config", "google-chrome")


#: SeleniumBase's undetected mode uses the x86_64 chromedriver on macOS even on
#: Apple Silicon, so Rosetta 2 has to be present to execute it.
_ROSETTA_MARKERS = ("rosetta", "bad cpu type")


#: Substrings in a startup failure that mean "there is no browser to drive".
_MISSING_BROWSER_MARKERS = (
    "chrome not found",
    "cannot find chrome binary",
    "no chrome binary",
    "unable to locate",
    "browser not found",
)


def _install_hint():
    """Platform-appropriate instructions for installing Google Chrome."""
    if sys.platform == "darwin":
        return (
            "Install Google Chrome:\n"
            "    brew install --cask google-chrome\n"
            "  or download it from https://www.google.com/chrome/\n"
            "  On Apple Silicon, take the Apple Silicon build so the browser and "
            "its driver match."
        )
    if sys.platform.startswith("win"):
        return (
            "Install Google Chrome from https://www.google.com/chrome/ , or:\n"
            "    winget install Google.Chrome"
        )
    return (
        "Install Google Chrome, for example:\n"
        "    sudo apt install google-chrome-stable\n"
        "  or download it from https://www.google.com/chrome/\n"
        "  Chromium alone is not enough for undetected mode."
    )


def render_pdf(driver):
    """Return the current page as PDF bytes, or ``None`` if the browser cannot.

    There is no ``save_print_page`` on a Selenium or SeleniumBase driver -- the
    original code called one, which is why saving a page as PDF raised
    ``AttributeError`` the moment it was wired up.  Two real APIs exist and
    neither works everywhere, so try both:

    * ``Page.printToPDF`` over CDP, which Chrome supports whether or not it is
      headless, but which only exists on Chromium-based drivers.
    * The W3C ``print_page`` endpoint, which ChromeDriver serves only in
      headless mode.
    """
    try:
        result = driver.execute_cdp_cmd("Page.printToPDF", {"printBackground": True})
        return base64.b64decode(result["data"])
    except Exception as exc:
        logger.debug("Page.printToPDF unavailable: %s", exc)

    try:
        return base64.b64decode(driver.print_page())
    except Exception as exc:
        logger.debug("print_page unavailable: %s", exc)

    return None


def is_dead_session_error(exc):
    if isinstance(exc, (InvalidSessionIdException, NoSuchWindowException, SessionLostError)):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _DEAD_SESSION_MARKERS)


class Browser:
    """A thin, self-healing wrapper around the SeleniumBase undetected driver."""

    def __init__(self, settings):
        self.settings = settings
        self.driver = None
        self.restarts = 0
        #: Called after a restart so the caller can log back in.
        self.on_restart = None
        self.start()

    # ------------------------------------------------------------------ setup

    def start(self):
        logger.info(
            "Starting browser (headless=%s, stealth=%s)",
            self.settings.headless,
            self.settings.stealth,
        )
        kwargs = {"uc": bool(self.settings.stealth)}
        if self.settings.user_agent:
            kwargs["agent"] = self.settings.user_agent

        # Undetected mode otherwise starts a throwaway profile: no cookies, no
        # history, no login. Pointing it at a real profile is what makes the
        # automated browser the same browser the person is already signed in to.
        if self.settings.user_data_dir:
            kwargs["user_data_dir"] = os.path.expanduser(self.settings.user_data_dir)
            logger.info("Using the Chrome profile at %s", kwargs["user_data_dir"])
        if self.settings.profile_directory:
            kwargs["chromium_arg"] = f"--profile-directory={self.settings.profile_directory}"
        if self.settings.headless:
            # ``headless2`` keeps a real renderer, which undetected-chromedriver
            # and Cloudflare's challenge both need. Plain ``headless`` fails the
            # challenge outright.
            kwargs["headless2"] = True
        else:
            kwargs["headed"] = True
        try:
            self.driver = Driver(**kwargs)
        except Exception as exc:
            # SeleniumBase happily downloads a matching chromedriver and only
            # then reports "Chrome not found!", which reads like a driver
            # problem when it is a missing browser. Say what to actually do.
            lowered = str(exc).lower()
            if any(marker in lowered for marker in _ROSETTA_MARKERS):
                raise MissingRosettaError(
                    "Undetected mode needs Rosetta 2 on this Mac: SeleniumBase "
                    "runs the x86_64 chromedriver even on Apple Silicon.\n"
                    "  Either install the translation layer (one-time):\n"
                    "    softwareupdate --install-rosetta --agree-to-license\n"
                    "  or skip undetected mode entirely and use a plain driver:\n"
                    "    --no-stealth\n"
                    "  Undetected mode is only needed to get past a Cloudflare "
                    "challenge. Many schools have none, so try --no-stealth "
                    "first if you would rather not install Rosetta.\n"
                    f"  (original error: {exc})"
                ) from exc
            if self.settings.user_data_dir and (
                "user data directory" in lowered
                or "already in use" in lowered
                or "cannot create default profile" in lowered
                or "profile appears to be in use" in lowered
            ):
                raise BrowserProfileInUseError(
                    "That Chrome profile is already open, and two processes "
                    "cannot share one.\n"
                    "  Quit Chrome completely (Cmd-Q, not just closing the "
                    "window) and run this again.\n"
                    "  To keep browsing while it downloads, copy the profile "
                    "and point at the copy instead.\n"
                    f"  (original error: {exc})"
                ) from exc
            if any(marker in lowered for marker in _MISSING_BROWSER_MARKERS):
                raise BrowserNotFoundError(
                    f"Google Chrome could not be found, so there is no browser "
                    f"to drive.\n  {_install_hint()}\n"
                    f"  (original error: {exc})"
                ) from exc
            raise
        self.driver.set_page_load_timeout(max(self.settings.timeout * 6, 60))
        self._adopt_real_user_agent()
        return self.driver

    def _adopt_real_user_agent(self):
        """Use the browser's own user agent for every request we make.

        Requests made outside the browser -- attachments, video fragments -- have
        to look like they came from it. Ask the browser what it actually is
        rather than asserting a version we hardcoded, which drifts out of date
        and breaks any Cloudflare clearance tied to the real one.
        """
        if self.settings.user_agent:
            return
        try:
            agent = self.driver.execute_script("return navigator.userAgent;")
        except Exception as exc:
            logger.debug("Could not read the browser's user agent: %s", exc)
            agent = None

        from .config import DEFAULT_USER_AGENT

        self.settings.user_agent = agent or DEFAULT_USER_AGENT
        logger.debug("Using user agent: %s", self.settings.user_agent)

    def quit(self):
        if self.driver is None:
            return
        try:
            self.driver.quit()
        except Exception as exc:  # pragma: no cover - teardown must never raise
            logger.debug("Ignoring error while quitting driver: %s", exc)
        finally:
            self.driver = None

    # -------------------------------------------------------------- liveness

    def is_alive(self):
        if self.driver is None:
            return False
        try:
            return bool(self.driver.window_handles)
        except Exception as exc:
            logger.debug("Driver liveness probe failed: %s", exc)
            return False

    def ensure_window(self):
        """Make sure the driver is focused on a window that actually exists."""
        if self.driver is None:
            raise SessionLostError("No driver")
        try:
            handles = self.driver.window_handles
        except Exception as exc:
            raise SessionLostError(str(exc)) from exc

        if not handles:
            raise SessionLostError("Browser has no open windows")

        try:
            # Touching current_window_handle raises if the active tab was closed.
            if self.driver.current_window_handle in handles:
                return
        except Exception:
            pass
        logger.warning("Active tab disappeared, switching to a surviving window")
        self.driver.switch_to.window(handles[-1])

    def ensure_alive(self):
        """Recover from a dead browser, restarting it up to ``max_session_restarts``."""
        try:
            self.ensure_window()
            return
        except SessionLostError as exc:
            logger.warning("Browser session lost: %s", exc)

        if self.restarts >= self.settings.max_session_restarts:
            raise SessionLostError(
                f"Browser session lost and already restarted "
                f"{self.restarts} time(s); giving up"
            )

        self.restarts += 1
        logger.warning(
            "Restarting browser (attempt %s/%s)",
            self.restarts,
            self.settings.max_session_restarts,
        )
        self.quit()
        self.start()
        if self.on_restart is not None:
            self.on_restart()

    # ------------------------------------------------------------ navigation

    def get(self, url, retries=2):
        """Navigate, transparently recovering from a lost session."""
        last_error = None
        for attempt in range(retries + 1):
            try:
                self.ensure_alive()
                self._navigate(url)
                self.wait_for_body()
                return True
            except Exception as exc:
                last_error = exc
                if not is_dead_session_error(exc):
                    raise
                logger.warning(
                    "Navigation to %s failed (%s), attempt %s/%s",
                    url,
                    exc,
                    attempt + 1,
                    retries + 1,
                )
                time.sleep(2)
        raise SessionLostError(f"Could not navigate to {url}: {last_error}")

    def _navigate(self, url):
        """Load a URL, hiding the automation from Cloudflare where we can.

        ``driver.get`` leaves chromedriver attached for the whole load, and that
        connection is exactly what Cloudflare's bot check notices -- the
        challenge then never clears and the run loops on "Performing security
        verification". SeleniumBase's UC mode can drop the connection across the
        navigation and reattach afterwards, which is what gets through.
        """
        if self.settings.stealth and hasattr(self.driver, "uc_open_with_reconnect"):
            try:
                self.driver.uc_open_with_reconnect(url, self.settings.uc_reconnect_time)
                return
            except Exception as exc:
                if is_dead_session_error(exc):
                    raise
                logger.debug("uc_open_with_reconnect failed (%s), using a plain load", exc)
        self.driver.get(url)

    @property
    def current_url(self):
        try:
            return self.driver.current_url
        except Exception:
            return ""

    @property
    def page_source(self):
        return self.driver.page_source

    def wait_for_body(self):
        try:
            WebDriverWait(self.driver, self.settings.timeout).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except TimeoutException:
            logger.debug("Timed out waiting for <body>")

    def check_elem_exists(self, by, selector, timeout=None):
        """Does an element exist? ``timeout`` is now actually honoured (it was ignored)."""
        wait = self.settings.timeout if timeout is None else timeout
        try:
            WebDriverWait(self.driver, timeout=wait).until(
                EC.presence_of_element_located((by, selector))
            )
        except (NoSuchElementException, TimeoutException):
            return False
        except WebDriverException as exc:
            if is_dead_session_error(exc):
                raise SessionLostError(str(exc)) from exc
            return False
        return True

    def find_first(self, selectors, timeout=None):
        """Return the first element matching any of ``(by, selector)`` pairs."""
        wait = self.settings.timeout if timeout is None else timeout
        deadline = time.time() + wait
        while True:
            for by, selector in selectors:
                try:
                    elements = self.driver.find_elements(by, selector)
                except WebDriverException as exc:
                    if is_dead_session_error(exc):
                        raise SessionLostError(str(exc)) from exc
                    continue
                for element in elements:
                    try:
                        if element.is_displayed():
                            return element
                    except WebDriverException:
                        continue
            if time.time() >= deadline:
                return None
            time.sleep(0.3)

    # ------------------------------------------------------------ cloudflare

    def browser_major_version(self):
        try:
            raw = self.driver.capabilities.get("browserVersion", "0")
            return int(str(raw).split(".")[0])
        except (TypeError, ValueError):
            return 0

    def on_challenge_page(self):
        """Is Cloudflare currently challenging us?"""
        try:
            title = (self.driver.title or "").lower()
        except WebDriverException:
            title = ""
        if any(marker in title for marker in CHALLENGE_TITLES):
            return True
        for by, selector in CHALLENGE_SELECTORS:
            try:
                if self.driver.find_elements(by, selector):
                    return True
            except WebDriverException:
                continue
        return False

    def bypass_cloudflare(self, attempts=3):
        """Clear a Cloudflare interstitial.

        The original approach opened a duplicate tab, asked the user to click
        the checkbox, then closed the first tab. That misses the common case:
        the challenge loops because chromedriver is attached, so no amount of
        clicking helps. UC mode's own handlers disconnect the driver around the
        click, which is what actually clears it.
        """
        if not self.on_challenge_page():
            logger.debug("No Cloudflare challenge present")
            return True

        logger.info("Cloudflare challenge detected, attempting to clear it")

        for attempt in range(1, attempts + 1):
            if self.settings.stealth and self._try_uc_captcha_click():
                if not self.on_challenge_page():
                    logger.info("Cloudflare challenge cleared")
                    return True

            # Reloading through the reconnecting path often clears a managed
            # challenge on its own.
            try:
                self._navigate(self.driver.current_url)
            except Exception as exc:
                logger.debug("Reload during the challenge failed: %s", exc)

            time.sleep(2)
            if not self.on_challenge_page():
                logger.info("Cloudflare challenge cleared")
                return True

            logger.warning("Still on the challenge page (attempt %s/%s)", attempt, attempts)

        return self._ask_user_to_solve_challenge()

    def _try_uc_captcha_click(self):
        """Let SeleniumBase click the Turnstile checkbox with the driver detached."""
        for method in ("uc_gui_click_captcha", "uc_gui_handle_captcha"):
            handler = getattr(self.driver, method, None)
            if handler is None:
                continue
            try:
                logger.info("Trying %s", method)
                handler()
                return True
            except Exception as exc:
                message = str(exc).lower()
                if "permission" in message or "accessibility" in message:
                    logger.warning(
                        "%s needs permission to control the mouse. On macOS grant "
                        "your terminal Accessibility access in System Settings > "
                        "Privacy & Security > Accessibility, then retry.",
                        method,
                    )
                else:
                    logger.debug("%s failed: %s", method, exc)
        return False

    def _ask_user_to_solve_challenge(self):
        """Last resort: let a human click it, since we are already headed."""
        if self.settings.headless:
            logger.error(
                "Cloudflare is still challenging and the browser is headless, so "
                "nobody can click the checkbox. Re-run without --headless."
            )
            return False

        try:
            input(
                "\033[93mCloudflare is still asking for verification.\n"
                "Please solve the challenge in the browser window, wait for the "
                "course page to load, then press Enter here.\033[0m"
            )
        except (EOFError, KeyboardInterrupt):
            return False

        try:
            self.ensure_window()
        except SessionLostError:
            logger.error("Browser was lost while solving the challenge")
            return False

        if self.on_challenge_page():
            logger.error("Still on the Cloudflare challenge page")
            return False

        logger.info("Cloudflare challenge cleared")
        return True

    def handle_cloudflare_if_present(self):
        if self.on_challenge_page():
            self.bypass_cloudflare()

    # ---------------------------------------------------------------- extras

    def cookies_for_requests(self):
        """Selenium's cookie jar, keeping each cookie's domain and path.

        Returning ``{name: value}`` and feeding that to ``session.cookies.set()``
        produces *domain-less* cookies, which requests sends to every host it
        talks to. One redirect to an attacker-controlled server would then hand
        over the user's Teachable session. Preserving the domain lets requests
        scope each cookie the way the browser does.
        """
        try:
            raw = self.driver.get_cookies()
        except Exception as exc:
            logger.debug("Could not read cookies: %s", exc)
            return []

        cookies = []
        for cookie in raw:
            name, value = cookie.get("name"), cookie.get("value")
            if not name:
                continue
            cookies.append(
                {
                    "name": name,
                    "value": value or "",
                    "domain": cookie.get("domain") or "",
                    "path": cookie.get("path") or "/",
                    "secure": bool(cookie.get("secure")),
                }
            )
        return cookies
