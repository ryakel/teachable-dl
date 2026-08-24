"""URL safety and bounded downloads."""

import os

import pytest

from teachable_dl.netutil import (
    DownloadTooLargeError,
    UnsafeUrlError,
    check_url,
    is_public_address,
    read_capped,
    safe_get,
    same_site,
    stream_to_file,
)


@pytest.fixture(autouse=True)
def stub_dns(monkeypatch):
    """Resolve example hosts to a public address.

    Without this the sandbox cannot resolve them, so every URL raises
    UnsafeUrlError -- which is the exception these tests expect, meaning the
    redirect tests would pass without exercising any redirect logic at all.
    """
    monkeypatch.setattr(
        "teachable_dl.netutil._addresses_for",
        lambda host: {"93.184.216.34"},
    )


class FakeResponse:
    def __init__(self, chunks=(b"data",), headers=None, status=200, location=None):
        self.chunks = list(chunks)
        self.headers = headers or {}
        self.status_code = status
        self.url = "https://cdn.example.com/f"
        self.closed = False
        if location:
            self.headers["Location"] = location

    @property
    def is_redirect(self):
        return 300 <= self.status_code < 400 and "Location" in self.headers

    is_permanent_redirect = is_redirect

    def iter_content(self, chunk_size=None):
        yield from self.chunks

    def close(self):
        self.closed = True

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requested = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.responses.pop(0)


# ------------------------------------------------------------------ SSRF

@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "http://127.0.0.1:8080/admin",
        "http://[::1]/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/router",
        "http://172.16.0.1/",
    ],
)
def test_internal_addresses_are_refused(url):
    with pytest.raises(UnsafeUrlError):
        check_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://host/x", "javascript:alert(1)"])
def test_non_http_schemes_are_refused(url):
    with pytest.raises(UnsafeUrlError):
        check_url(url)


def test_public_urls_are_allowed():
    assert check_url("https://cdn.filestackcontent.com/a.pdf", resolve=False)


def test_a_literal_internal_ip_is_refused_even_without_resolution():
    """The cheap screen used while listing links must still stop literal IPs."""
    with pytest.raises(UnsafeUrlError):
        check_url("http://169.254.169.254/x", resolve=False)


def test_private_addresses_can_be_allowed_deliberately():
    assert check_url("http://127.0.0.1:8000/a.pdf", allow_private=True)


def test_ipv4_mapped_ipv6_loopback_is_refused():
    """``::ffff:127.0.0.1`` is loopback wearing an IPv6 hat."""
    assert not is_public_address("::ffff:127.0.0.1")


# ------------------------------------------------------------- redirects

def test_each_redirect_hop_is_revalidated():
    """Letting requests follow redirects means an attacker's Location is fetched
    before we ever see it."""
    session = FakeSession([FakeResponse(status=302, location="http://127.0.0.1/steal")])
    with pytest.raises(UnsafeUrlError):
        safe_get(session, "https://cdn.example.com/file")


def test_a_safe_redirect_is_followed():
    session = FakeSession(
        [
            FakeResponse(status=302, location="https://cdn2.example.com/real"),
            FakeResponse(chunks=[b"ok"]),
        ]
    )
    response = safe_get(session, "https://cdn.example.com/file")
    assert session.requested == ["https://cdn.example.com/file", "https://cdn2.example.com/real"]
    assert response.status_code == 200


def test_a_redirect_loop_terminates():
    session = FakeSession(
        [FakeResponse(status=302, location="https://cdn.example.com/file")] * 3
    )
    with pytest.raises(UnsafeUrlError):
        safe_get(session, "https://cdn.example.com/file")


def test_same_site_comparison():
    assert same_site("https://school.teachable.com/a", "https://cdn.teachable.com/b")
    assert not same_site("https://school.teachable.com/a", "https://evil.example.com/b")


# ---------------------------------------------------------------- limits

def test_a_download_is_written_through_a_part_file(tmp_path):
    target = str(tmp_path / "f.bin")
    stream_to_file(FakeResponse([b"abc", b"def"]), target)
    assert open(target, "rb").read() == b"abcdef"
    assert not os.path.exists(target + ".part")


def test_an_interrupted_download_leaves_no_file_behind(tmp_path):
    """Writing straight to the final name left a truncated file that the next
    run happily skipped as 'already downloaded'."""

    class Exploding(FakeResponse):
        def iter_content(self, chunk_size=None):
            yield b"partial"
            raise ConnectionError("dropped")

    target = str(tmp_path / "f.bin")
    with pytest.raises(ConnectionError):
        stream_to_file(Exploding(), target)
    assert not os.path.exists(target)
    assert not os.path.exists(target + ".part")


def test_an_oversized_body_is_refused_mid_stream(tmp_path):
    target = str(tmp_path / "f.bin")
    with pytest.raises(DownloadTooLargeError):
        stream_to_file(FakeResponse([b"x" * 100] * 10), target, max_bytes=200)
    assert not os.path.exists(target)


def test_a_declared_oversized_length_is_refused_before_reading(tmp_path):
    response = FakeResponse([b"x"], headers={"Content-Length": str(10**12)})
    with pytest.raises(DownloadTooLargeError):
        stream_to_file(response, str(tmp_path / "f.bin"), max_bytes=1024)


def test_read_capped_refuses_an_endless_body():
    with pytest.raises(DownloadTooLargeError):
        read_capped(FakeResponse([b"x" * 1000] * 100), max_bytes=5000)


def test_read_capped_returns_a_small_body():
    assert read_capped(FakeResponse([b"ab", b"cd"]), max_bytes=100) == b"abcd"
