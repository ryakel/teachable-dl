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
        kwargs = {"uc": bool(self.settings.stealth), "agent": self.settings.user_agent}
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
            if any(marker in lowered for marker in _MISSING_BROWSER_MARKERS):
                raise BrowserNotFoundError(
                    f"Google Chrome could not be found, so there is no browser "
                    f"to drive.\n  {_install_hint()}\n"
                    f"  (original error: {exc})"
                ) from exc
            raise
        self.driver.set_page_load_timeout(max(self.settings.timeout * 6, 60))
        return self.driver

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
                self.driver.get(url)
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

    def bypass_cloudflare(self):
        """Solve the interstitial without orphaning the driver on a closed tab.

        The original implementation compared browser versions as *strings*
        (``"99" < "115"`` is False), then closed the original tab. If any step
        raised, the driver was left on a dead handle.
        """
        if self.browser_major_version() < 115:
            logger.debug("Browser too old for the automated challenge flow, skipping")
            return

        if not self.check_elem_exists(By.ID, "challenge-stage", timeout=3):
            logger.debug("No Cloudflare challenge present")
            return

        logger.info("Bypassing Cloudflare")
        original_handles = list(self.driver.window_handles)
        try:
            self.driver.find_element(By.ID, "challenge-stage").click()
            current = self.driver.current_url
            self.driver.execute_script("window.open(arguments[0], '_blank');", current)

            new_handles = [h for h in self.driver.window_handles if h not in original_handles]
            if not new_handles:
                logger.warning("Could not open a second tab for the challenge")
                return

            if self.settings.headless:
                logger.warning(
                    "A Cloudflare challenge appeared but the browser is headless; "
                    "re-run without --headless if the download stalls"
                )
            else:
                input(
                    "\033[93mWarning: Bypassing Cloudflare\n"
                    "please click on the captcha checkbox if not done already "
                    "and press enter to continue (do not close any of the tabs)\033[0m"
                )

            # Close the challenged tab, then settle on the fresh one.
            for handle in original_handles:
                try:
                    self.driver.switch_to.window(handle)
                    self.driver.close()
                except WebDriverException as exc:
                    logger.debug("Could not close stale tab: %s", exc)
            self.driver.switch_to.window(new_handles[0])
        except Exception as exc:
            logger.error("Could not bypass Cloudflare: %s", exc, exc_info=self.settings.verbose)
        finally:
            # Whatever happened above, never leave the caller on a dead handle.
            try:
                self.ensure_window()
            except SessionLostError:
                logger.error("Browser lost during the Cloudflare bypass")

    def handle_cloudflare_if_present(self):
        if self.check_elem_exists(By.ID, "challenge-stage", timeout=3):
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
