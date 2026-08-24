"""Attachment handling -- upstream #38 (download PDFs and MP3s)."""

from selenium.webdriver.remote.webdriver import By

from tests.conftest import FakeElement, make_browser
from teachable_dl.attachments import ATTACHMENT_SELECTORS, _is_downloadable, filename_from_response


class FakeResponse:
    def __init__(self, headers=None):
        self.headers = headers or {}


def test_filename_comes_from_content_disposition():
    name = filename_from_response(
        "https://cdn/x?token=1",
        FakeResponse({"Content-Disposition": 'attachment; filename="Workbook 1.pdf"'}),
        "fallback",
    )
    assert name == "Workbook-1.pdf"


def test_utf8_content_disposition_is_decoded():
    name = filename_from_response(
        "https://cdn/x",
        FakeResponse({"Content-Disposition": "attachment; filename*=UTF-8''%D0%A3%D1%80%D0%BE%D0%BA.pdf"}),
        "fallback",
    )
    assert name == "Урок.pdf"


def test_filename_falls_back_to_the_url_path():
    name = filename_from_response("https://cdn/files/audio.mp3", FakeResponse(), "fallback")
    assert name == "audio.mp3"


def test_extension_is_inferred_from_the_content_type():
    """A signed CDN URL often has no extension at all."""
    name = filename_from_response(
        "https://cdn/files/abc123", FakeResponse({"Content-Type": "application/pdf"}), "lecture"
    )
    assert name.endswith(".pdf")


def test_filename_is_sanitised_and_length_capped():
    name = filename_from_response(
        "https://cdn/x",
        FakeResponse({"Content-Disposition": 'filename="' + "a" * 400 + '.pdf"'}),
        "fallback",
    )
    assert len(name.encode("utf-8")) < 200
    assert name.endswith(".pdf")


def _collect(mapping, **settings_kwargs):
    from teachable_dl.attachments import AttachmentDownloader

    browser = make_browser(mapping, **settings_kwargs)
    return AttachmentDownloader(browser, browser.settings).collect_urls()


def test_pdf_and_mp3_attachments_are_actually_collected():
    """#38 asked specifically for PDFs and MP3s; drive the real collection."""
    pdf = FakeElement(attributes={"href": "https://cdn.example.com/workbook.pdf"})
    mp3 = FakeElement(attributes={"href": "https://cdn.example.com/audio.mp3"})
    urls = _collect(
        {
            (By.CSS_SELECTOR, ".lecture-attachment-type-pdf_embed a[href]"): [pdf],
            (By.CSS_SELECTOR, ".lecture-attachment-type-audio a[href]"): [mp3],
        }
    )
    assert set(urls) == {
        "https://cdn.example.com/workbook.pdf",
        "https://cdn.example.com/audio.mp3",
    }


def test_the_same_attachment_matched_twice_is_only_downloaded_once():
    shared = FakeElement(attributes={"href": "https://cdn.example.com/a.pdf"})
    urls = _collect(
        {
            (By.CSS_SELECTOR, ".lecture-attachment-type-file a[href]"): [shared],
            (By.CSS_SELECTOR, ".lecture-attachment-type-pdf_embed a[href]"): [shared],
        }
    )
    assert urls == ["https://cdn.example.com/a.pdf"]


def test_an_internal_address_is_never_collected():
    """A malicious school linking to cloud metadata must not be followed."""
    evil = FakeElement(attributes={"href": "http://169.254.169.254/latest/meta-data/"})
    assert _collect({(By.CSS_SELECTOR, ".lecture-attachment-type-file a[href]"): [evil]}) == []


def test_private_addresses_can_be_opted_into_for_self_hosted_schools():
    local = FakeElement(attributes={"href": "http://127.0.0.1:8000/a.pdf"})
    mapping = {(By.CSS_SELECTOR, ".lecture-attachment-type-file a[href]"): [local]}
    assert _collect(mapping) == []
    assert _collect(mapping, allow_private_hosts=True) == ["http://127.0.0.1:8000/a.pdf"]


def test_video_players_are_not_treated_as_downloadable_files():
    assert not _is_downloadable("https://player.hotmart.com/embed/abc")
    assert not _is_downloadable("javascript:void(0)")
    assert not _is_downloadable("")
    assert not _is_downloadable(None)


def test_ordinary_file_urls_are_downloadable():
    assert _is_downloadable("https://cdn.filestackcontent.com/abc/workbook.pdf")
