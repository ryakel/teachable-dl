"""Browser helpers: session recovery and page-to-PDF rendering."""

import base64

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
