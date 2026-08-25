"""Browser helpers: session recovery and page-to-PDF rendering."""

import base64
import pathlib

import pytest

from teachable_dl.browser import SessionLostError, is_dead_session_error, render_pdf

PDF_BYTES = b"%PDF-1.4 fake"
PDF_B64 = base64.b64encode(PDF_BYTES).decode()


class FakeDriver:
    """Stands in for a Chrome driver with configurable capabilities."""

    def __init__(self, cdp=None, w3c=None):
        self._cdp = cdp
        self._w3c = w3c

    def execute_cdp_cmd(self, cmd, params):
        if self._cdp is None:
            raise Exception("Page.printToPDF is not supported")
        return self._cdp

    def print_page(self):
        if self._w3c is None:
            raise Exception("PrintToPDF is only supported in headless mode")
        return self._w3c


# ------------------------------------------------------------------- PDF

def test_pdf_is_rendered_over_cdp_when_available():
    """The old code called save_print_page, which exists on neither library."""
    assert render_pdf(FakeDriver(cdp={"data": PDF_B64})) == PDF_BYTES


def test_pdf_falls_back_to_the_w3c_endpoint():
    """ChromeDriver serves print_page only in headless mode, CDP only sometimes."""
    assert render_pdf(FakeDriver(cdp=None, w3c=PDF_B64)) == PDF_BYTES


def test_pdf_returns_none_when_the_browser_cannot_print():
    assert render_pdf(FakeDriver(cdp=None, w3c=None)) is None


def test_render_pdf_never_raises_at_the_call_site():
    """A page that will not print must not abort the whole course download."""
    class Hostile:
        def execute_cdp_cmd(self, *a, **k):
            raise RuntimeError("boom")

        def print_page(self):
            raise RuntimeError("boom")

    assert render_pdf(Hostile()) is None


# --------------------------------------------------------- session recovery

@pytest.mark.parametrize(
    "message",
    [
        "invalid session id",
        "no such window: target window already closed",
        "unknown error: web view not found",
        "chrome not reachable",
        "session deleted because of page crash",
    ],
)
def test_dead_session_messages_are_recognised(message):
    """#51/#53: these used to be logged per lecture and otherwise ignored."""
    assert is_dead_session_error(Exception(message))


def test_dead_session_detection_is_case_insensitive():
    assert is_dead_session_error(Exception("Invalid Session Id"))


def test_ordinary_errors_are_not_treated_as_a_dead_session():
    assert not is_dead_session_error(Exception("element not interactable"))
    assert not is_dead_session_error(Exception("timeout waiting for element"))


def test_session_lost_error_is_always_a_dead_session():
    assert is_dead_session_error(SessionLostError("gone"))


# ------------------------------------------------- missing browser guidance

def test_a_missing_chrome_is_reported_as_a_setup_problem(monkeypatch):
    """SeleniumBase downloads a driver and only then says "Chrome not found!",
    which reads like a driver problem when the browser is simply not installed."""
    from teachable_dl.browser import Browser, BrowserNotFoundError
    from teachable_dl.config import Settings

    def explode(**kwargs):
        raise Exception("Chrome not found! Install it first!")

    monkeypatch.setattr("teachable_dl.browser.Driver", explode)

    browser = Browser.__new__(Browser)
    browser.settings = Settings()
    with pytest.raises(BrowserNotFoundError) as caught:
        browser.start()

    message = str(caught.value)
    assert "google.com/chrome" in message
    assert "Chrome not found" in message   # keeps the original cause


def test_an_unrelated_startup_failure_is_not_disguised(monkeypatch):
    from teachable_dl.browser import Browser, BrowserNotFoundError
    from teachable_dl.config import Settings

    def explode(**kwargs):
        raise ValueError("something else entirely")

    monkeypatch.setattr("teachable_dl.browser.Driver", explode)
    browser = Browser.__new__(Browser)
    browser.settings = Settings()

    with pytest.raises(ValueError):
        browser.start()


@pytest.mark.parametrize(
    "platform,expected",
    [("darwin", "brew install --cask google-chrome"),
     ("win32", "winget install Google.Chrome"),
     ("linux", "google-chrome-stable")],
)
def test_install_hint_matches_the_platform(monkeypatch, platform, expected):
    from teachable_dl import browser as browser_module

    monkeypatch.setattr(browser_module.sys, "platform", platform)
    assert expected in browser_module._install_hint()


def test_an_apple_silicon_mac_without_rosetta_is_told_what_to_install(monkeypatch):
    """SeleniumBase's UC Mode runs the x86_64 chromedriver even on Apple
    Silicon, so a stock arm64 Mac fails with "Bad CPU type in executable"."""
    from teachable_dl.browser import Browser, MissingRosettaError
    from teachable_dl.config import Settings

    def explode(**kwargs):
        raise Exception(
            "[Errno 86] Bad CPU type in executable: '.../seleniumbase/drivers/uc_driver'"
        )

    monkeypatch.setattr("teachable_dl.browser.Driver", explode)
    browser = Browser.__new__(Browser)
    browser.settings = Settings()

    with pytest.raises(MissingRosettaError) as caught:
        browser.start()
    assert "softwareupdate --install-rosetta" in str(caught.value)


def test_rosetta_is_distinguished_from_a_missing_browser(monkeypatch):
    """The two failures need different fixes, so they must not be conflated."""
    from teachable_dl.browser import Browser, BrowserNotFoundError
    from teachable_dl.config import Settings

    monkeypatch.setattr(
        "teachable_dl.browser.Driver",
        lambda **k: (_ for _ in ()).throw(Exception("Chrome not found! Install it first!")),
    )
    browser = Browser.__new__(Browser)
    browser.settings = Settings()

    with pytest.raises(BrowserNotFoundError):
        browser.start()


# ------------------------------------------------------------ stealth toggle

def test_undetected_mode_is_on_by_default(monkeypatch):
    """UC mode is what gets past a Cloudflare interstitial, so it stays default."""
    from teachable_dl.browser import Browser
    from teachable_dl.config import Settings

    seen = {}

    class FakeDriver:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def set_page_load_timeout(self, value):
            pass

    monkeypatch.setattr("teachable_dl.browser.Driver", FakeDriver)
    browser = Browser.__new__(Browser)
    browser.settings = Settings()
    browser.start()
    assert seen["uc"] is True


def test_no_stealth_uses_a_plain_driver(monkeypatch):
    """--no-stealth avoids the x86_64 chromedriver, and so the Rosetta 2
    requirement on Apple Silicon."""
    from teachable_dl.browser import Browser
    from teachable_dl.config import Settings

    seen = {}

    class FakeDriver:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def set_page_load_timeout(self, value):
            pass

    monkeypatch.setattr("teachable_dl.browser.Driver", FakeDriver)
    browser = Browser.__new__(Browser)
    browser.settings = Settings(stealth=False)
    browser.start()
    assert seen["uc"] is False


def test_the_rosetta_error_offers_both_ways_out(monkeypatch):
    from teachable_dl.browser import Browser, MissingRosettaError
    from teachable_dl.config import Settings

    monkeypatch.setattr(
        "teachable_dl.browser.Driver",
        lambda **k: (_ for _ in ()).throw(Exception("[Errno 86] Bad CPU type in executable")),
    )
    browser = Browser.__new__(Browser)
    browser.settings = Settings()

    with pytest.raises(MissingRosettaError) as caught:
        browser.start()
    message = str(caught.value)
    assert "softwareupdate --install-rosetta" in message
    assert "--no-stealth" in message


# ------------------------------------------------------ cloudflare challenge

class ChallengeDriver(FakeDriver):
    """A driver that reports a Cloudflare challenge until it is cleared."""

    def __init__(self, title="Just a moment...", elements=True):
        super().__init__()
        self.title = title
        self._elements = elements
        self.reconnect_calls = 0
        self.captcha_clicks = 0
        self.current_url = "https://school.teachable.com/courses/enrolled/1"

    def find_elements(self, by, selector):
        return ["el"] if self._elements else []

    def uc_open_with_reconnect(self, url, seconds):
        self.reconnect_calls += 1

    def uc_gui_click_captcha(self):
        self.captcha_clicks += 1
        self.title = "Course"
        self._elements = False


def make_browser_with(driver, **settings_kwargs):
    from teachable_dl.browser import Browser
    from teachable_dl.config import Settings

    browser = Browser.__new__(Browser)
    browser.settings = Settings(timeout=1, **settings_kwargs)
    browser.driver = driver
    browser.restarts = 0
    browser.on_restart = None
    return browser


def test_a_turnstile_challenge_is_detected():
    """Matching only #challenge-stage missed Turnstile, so the run looped."""
    browser = make_browser_with(ChallengeDriver())
    assert browser.on_challenge_page()


def test_a_challenge_is_detected_by_page_title_alone():
    browser = make_browser_with(ChallengeDriver(title="Just a moment...", elements=False))
    assert browser.on_challenge_page()


def test_an_ordinary_page_is_not_a_challenge():
    browser = make_browser_with(ChallengeDriver(title="Course", elements=False))
    assert not browser.on_challenge_page()


def test_the_challenge_is_cleared_via_the_uc_handler():
    driver = ChallengeDriver()
    browser = make_browser_with(driver)
    assert browser.bypass_cloudflare() is True
    assert driver.captcha_clicks == 1


def test_navigation_detaches_the_driver_in_stealth_mode():
    """Cloudflare notices the attached chromedriver; UC mode drops it."""
    driver = ChallengeDriver(title="Course", elements=False)
    browser = make_browser_with(driver)
    browser._navigate("https://school.teachable.com/x")
    assert driver.reconnect_calls == 1


def test_navigation_is_plain_when_stealth_is_off():
    class Plain(ChallengeDriver):
        def __init__(self):
            super().__init__(title="Course", elements=False)
            self.plain_gets = 0

        def get(self, url):
            self.plain_gets += 1

    driver = Plain()
    browser = make_browser_with(driver, stealth=False)
    browser._navigate("https://school.teachable.com/x")
    assert driver.plain_gets == 1 and driver.reconnect_calls == 0


def test_a_headless_run_cannot_ask_a_human_to_click():
    """No point prompting for a click nobody can see."""
    driver = ChallengeDriver()
    driver.uc_gui_click_captcha = lambda: (_ for _ in ()).throw(Exception("no gui"))
    browser = make_browser_with(driver, headless=True, stealth=True)
    assert browser.bypass_cloudflare(attempts=1) is False


# ------------------------------------------------------- real Chrome profile

def test_by_default_no_profile_is_requested(monkeypatch):
    """Undetected mode's own throwaway profile stays the default."""
    from teachable_dl.browser import Browser
    from teachable_dl.config import Settings

    seen = {}

    class FakeDriver:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def set_page_load_timeout(self, value):
            pass

        def execute_script(self, script):
            return "UA/1.0"

    monkeypatch.setattr("teachable_dl.browser.Driver", FakeDriver)
    browser = Browser.__new__(Browser)
    browser.settings = Settings()
    browser.start()
    assert "user_data_dir" not in seen




def test_that_error_is_not_raised_when_no_profile_was_requested(monkeypatch):
    """Without --chrome-profile the same message means something else."""
    from teachable_dl.browser import Browser, BrowserProfileInUseError
    from teachable_dl.config import Settings

    monkeypatch.setattr(
        "teachable_dl.browser.Driver",
        lambda **k: (_ for _ in ()).throw(Exception("user data directory is already in use")),
    )
    browser = Browser.__new__(Browser)
    browser.settings = Settings()
    with pytest.raises(Exception) as caught:
        browser.start()
    assert not isinstance(caught.value, BrowserProfileInUseError)



def test_without_a_profile_undetected_mode_is_untouched(monkeypatch):
    from teachable_dl.browser import Browser
    from teachable_dl.config import Settings

    seen = {}

    class FakeDriver:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def set_page_load_timeout(self, value):
            pass

        def execute_script(self, script):
            return "UA/1.0"

    monkeypatch.setattr("teachable_dl.browser.Driver", FakeDriver)
    browser = Browser.__new__(Browser)
    browser.settings = Settings(stealth=True)
    browser.start()
    assert seen["uc"] is True


def _fake_chrome_profile(tmp_path, name="Default"):
    """A directory shaped like a real Chrome user-data dir."""
    profile = tmp_path / name
    profile.mkdir(parents=True)
    (profile / "Preferences").write_text("{}", encoding="utf-8")
    (profile / "Cookies").write_bytes(b"cookie-db")
    (tmp_path / "Local State").write_text("{}", encoding="utf-8")
    (profile / "SingletonLock").write_text("lock", encoding="utf-8")
    return tmp_path


class RecordingDriver:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        RecordingDriver.last = kwargs

    def set_page_load_timeout(self, value):
        pass

    def execute_script(self, script):
        return "UA/1.0"


def _start_with(monkeypatch, settings):
    from teachable_dl.browser import Browser

    monkeypatch.setattr("teachable_dl.browser.Driver", RecordingDriver)
    browser = Browser.__new__(Browser)
    browser.settings = settings
    browser._profile_copy = None
    browser.start()
    return browser, RecordingDriver.last


def test_a_profile_is_passed_as_raw_chrome_flags(tmp_path, monkeypatch):
    """SeleniumBase's own user_data_dir parameter refuses the directory
    outright, so the flags go to Chrome directly instead."""
    from teachable_dl.config import Settings

    source = _fake_chrome_profile(tmp_path)
    _, kwargs = _start_with(monkeypatch, Settings(user_data_dir=str(source)))

    assert "user_data_dir" not in kwargs
    assert "--user-data-dir=" in kwargs["chromium_arg"]
    assert "--profile-directory=Default" in kwargs["chromium_arg"]


def test_the_real_profile_is_copied_not_driven(tmp_path, monkeypatch):
    """Chrome locks a live profile and background processes keep holding it, so
    the copy is what makes this work at all."""
    from teachable_dl.config import Settings

    source = _fake_chrome_profile(tmp_path)
    browser, kwargs = _start_with(monkeypatch, Settings(user_data_dir=str(source)))

    used = kwargs["chromium_arg"].split("--user-data-dir=")[1].split(",")[0]
    assert used != str(source), "must not drive the real profile"
    assert browser._profile_copy == used
    # The copy carries the session and none of the locks.
    assert (pathlib.Path(used) / "Default" / "Cookies").is_file()
    assert not (pathlib.Path(used) / "Default" / "SingletonLock").exists()


def test_the_copy_is_removed_on_quit(tmp_path, monkeypatch):
    from teachable_dl.config import Settings

    source = _fake_chrome_profile(tmp_path)
    browser, _ = _start_with(monkeypatch, Settings(user_data_dir=str(source)))
    copy = browser._profile_copy
    assert pathlib.Path(copy).exists()

    browser.driver = None
    browser.quit()
    assert not pathlib.Path(copy).exists()


def test_live_mode_drives_the_real_profile(tmp_path, monkeypatch):
    from teachable_dl.config import Settings

    source = _fake_chrome_profile(tmp_path)
    browser, kwargs = _start_with(
        monkeypatch, Settings(user_data_dir=str(source), use_live_profile=True)
    )
    assert f"--user-data-dir={source}" in kwargs["chromium_arg"]
    assert browser._profile_copy is None


def test_a_profile_turns_undetected_mode_off(tmp_path, monkeypatch):
    """UC mode manages its own throwaway profile and cannot attach to another."""
    from teachable_dl.config import Settings

    source = _fake_chrome_profile(tmp_path)
    browser, kwargs = _start_with(
        monkeypatch, Settings(user_data_dir=str(source), stealth=True)
    )
    assert kwargs["uc"] is False
    assert browser.settings.stealth is False


def test_a_directory_that_is_not_a_profile_is_reported(tmp_path, monkeypatch):
    from teachable_dl.browser import BrowserProfileError
    from teachable_dl.config import Settings

    with pytest.raises(BrowserProfileError):
        _start_with(monkeypatch, Settings(user_data_dir=str(tmp_path / "nowhere")))
