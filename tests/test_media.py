"""Download options -- upstream #59 (slow), #41 (unplayable mp4), #44 (403)."""

from unittest import mock

import pytest

from teachable_dl.config import Settings
from teachable_dl.media import build_ydl_opts, find_existing_video, headers_for_embed


@pytest.fixture
def settings():
    return Settings(concurrent_fragments=16, retries=10, fragment_retries=25)


@pytest.fixture
def with_ffmpeg():
    with mock.patch("teachable_dl.media.ffmpeg_available", return_value=True):
        yield


@pytest.fixture
def without_ffmpeg():
    with mock.patch("teachable_dl.media.ffmpeg_available", return_value=False):
        yield


def test_parallel_fragment_option_is_spelled_correctly(settings, with_ffmpeg):
    """#59: the old code passed 'concurrentfragments', which yt-dlp ignores.

    Downloads therefore ran one fragment at a time no matter what the value was.
    """
    opts = build_ydl_opts(settings, "out.%(ext)s", {})
    assert opts["concurrent_fragment_downloads"] == 16
    assert "concurrentfragments" not in opts


def test_the_option_name_is_one_yt_dlp_actually_accepts(settings, with_ffmpeg):
    """Guard against the typo coming back: check against yt-dlp's real signature."""
    import inspect

    import yt_dlp

    assert "concurrent_fragment_downloads" in inspect.getsource(yt_dlp.YoutubeDL)
    # And that the misspelling really is not a thing yt-dlp knows about.
    assert "concurrentfragments" not in inspect.getsource(yt_dlp.YoutubeDL)


def test_concurrency_is_clamped_to_at_least_one(with_ffmpeg):
    opts = build_ydl_opts(Settings(concurrent_fragments=0), "out.%(ext)s", {})
    assert opts["concurrent_fragment_downloads"] >= 1


def test_with_ffmpeg_streams_are_merged_into_mp4(settings, with_ffmpeg):
    opts = build_ydl_opts(settings, "out.%(ext)s", {})
    assert opts["merge_output_format"] == "mp4"
    assert opts["format"] == "bestvideo*+bestaudio/best"


def test_without_ffmpeg_only_single_file_formats_are_requested(settings, without_ffmpeg):
    """#41: asking for video+audio without a merger leaves an unplayable .mp4."""
    opts = build_ydl_opts(settings, "out.%(ext)s", {})
    assert "+" not in opts["format"]
    assert "merge_output_format" not in opts
    assert "postprocessors" not in opts


def test_no_blind_reencode_pass(settings, with_ffmpeg):
    """#41: FFmpegVideoConvertor ran unconditionally and corrupted output."""
    keys = {p["key"] for p in build_ydl_opts(settings, "out.%(ext)s", {})["postprocessors"]}
    assert "FFmpegVideoConvertor" not in keys


def test_long_downloads_get_generous_retries(settings, with_ffmpeg):
    """#55: a three-hour video outlives its signed URL and any transient error."""
    opts = build_ydl_opts(settings, "out.%(ext)s", {})
    assert opts["fragment_retries"] >= 10
    assert opts["retries"] >= 5
    assert opts["continuedl"] is True
    assert opts["skip_unavailable_fragments"] is False


def test_headers_follow_the_actual_embed_host():
    """#44: Origin/Referer were hardcoded to player.hotmart.com, so others 403'd."""
    headers = headers_for_embed("https://fast.wistia.net/embed/iframe/abc123", "UA/1.0")
    assert headers["Origin"] == "https://fast.wistia.net"
    assert headers["Referer"].startswith("https://fast.wistia.net")
    assert headers["User-Agent"] == "UA/1.0"


def test_headers_without_an_embed_url_still_carry_the_user_agent():
    headers = headers_for_embed(None, "UA/1.0")
    assert headers == {"User-Agent": "UA/1.0"}


def test_headers_ignore_a_non_http_embed():
    assert "Origin" not in headers_for_embed("about:blank", "UA/1.0")


def test_finished_download_is_detected(tmp_path):
    (tmp_path / "01-Intro.mp4").write_bytes(b"data")
    assert find_existing_video(str(tmp_path), "01-Intro") is not None


def test_partial_download_is_not_mistaken_for_a_finished_one(tmp_path):
    """#41: a leftover .part means ffmpeg never merged; do not skip or serve it."""
    (tmp_path / "01-Intro.mp4").write_bytes(b"data")
    (tmp_path / "01-Intro.mp4.part").write_bytes(b"partial")
    assert find_existing_video(str(tmp_path), "01-Intro") is None


def test_empty_file_is_not_a_finished_download(tmp_path):
    (tmp_path / "01-Intro.mp4").write_bytes(b"")
    assert find_existing_video(str(tmp_path), "01-Intro") is None
