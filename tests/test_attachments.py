"""Attachment handling -- upstream #38 (download PDFs and MP3s)."""

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


def test_pdf_and_audio_attachment_blocks_are_covered():
    """#38 asked specifically for PDF and MP3; both live in their own block types."""
    selectors = " ".join(selector for _, selector in ATTACHMENT_SELECTORS)
    assert "pdf_embed" in selectors
    assert "type-audio" in selectors


def test_video_players_are_not_treated_as_downloadable_files():
    assert not _is_downloadable("https://player.hotmart.com/embed/abc")
    assert not _is_downloadable("javascript:void(0)")
    assert not _is_downloadable("")
    assert not _is_downloadable(None)


def test_ordinary_file_urls_are_downloadable():
    assert _is_downloadable("https://cdn.filestackcontent.com/abc/workbook.pdf")
