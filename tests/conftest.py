"""Fakes that let the browser-driven code be tested without a browser."""

import pytest
from selenium.common.exceptions import NoSuchElementException

from teachable_dl.browser import Browser
from teachable_dl.config import Settings


class FakeElement:
    """A stand-in for a Selenium WebElement."""

    def __init__(self, tag="input", text="", attributes=None, displayed=True):
        self.tag = tag
        self.text = text
        self.attributes = attributes or {}
        self._displayed = displayed
        self.value = ""
        self.clicks = 0

    def is_displayed(self):
        return self._displayed

    def click(self):
        self.clicks += 1

    def clear(self):
        self.value = ""

    def send_keys(self, keys):
        self.value += keys

    def get_attribute(self, name):
        return self.attributes.get(name)


class FakeDriver:
    """Answers find_elements from a {(by, selector): [elements]} mapping.

    Every page has a ``<body>``, and a miss raises the exception Selenium
    really raises, so code that waits for the document behaves as it would
    against a real driver.
    """

    def __init__(self, mapping=None, cookies=None):
        self.mapping = mapping or {}
        self._cookies = cookies or []
        self.current_url = "https://school.teachable.com/lecture"
        self.title = "Course"

    def find_elements(self, by, selector):
        found = self.mapping.get((by, selector))
        if found is None and str(selector).lower() == "body":
            return [FakeElement(tag="body")]
        return list(found or [])

    def find_element(self, by, selector):
        found = self.find_elements(by, selector)
        if not found:
            raise NoSuchElementException(selector)
        return found[0]

    def get_cookies(self):
        return list(self._cookies)

    # -- enough of a real driver that Browser.get() works against the fake,
    #    rather than deciding the session is dead and restarting Chrome.
    @property
    def window_handles(self):
        return ["window-1"]

    @property
    def current_window_handle(self):
        return "window-1"

    @property
    def switch_to(self):
        class _SwitchTo:
            def window(self, name):
                pass

            def frame(self, element):
                pass

            def default_content(self):
                pass

        return _SwitchTo()

    def get(self, url):
        self.current_url = url

    def set_page_load_timeout(self, seconds):
        pass

    def execute_script(self, script, *args):
        return None


def make_browser(mapping=None, cookies=None, **settings_kwargs):
    """A Browser wired to a FakeDriver, bypassing real driver startup."""
    browser = Browser.__new__(Browser)
    browser.settings = Settings(timeout=1, **settings_kwargs)
    browser.driver = FakeDriver(mapping, cookies)
    browser.restarts = 0
    browser.on_restart = None
    return browser


@pytest.fixture
def browser_factory():
    return make_browser
