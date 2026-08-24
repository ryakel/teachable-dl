"""URL safety and bounded downloads.

Every URL this tool fetches -- attachment links, the course image, subtitle
playlists -- comes from a page served by the school.  A malicious or
compromised school therefore chooses them, which makes two attacks possible if
the URLs are fetched naively:

* **Session theft.** Selenium hands us the browser's cookies, and the obvious
  ``session.cookies.set(name, value)`` produces a *domain-less* cookie.  requests
  sends those to every host, so one redirect to an attacker-controlled server
  hands over the user's Teachable session. Cookies must carry their domain, and
  redirects must be followed one hop at a time so each hop can be re-checked.

* **SSRF.** ``http://169.254.169.254/...`` or ``http://127.0.0.1:6379/`` are
  perfectly ordinary-looking URLs. Fetching them reaches the user's own network.

So: resolve the host, refuse anything that is not a public address, follow
redirects manually, and cap how many bytes anyone can make us write.
"""

import ipaddress
import logging
import os
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("http", "https")

#: Nothing a course page legitimately links to is this big.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024 * 1024  # 8 GiB
#: Playlists and subtitle tracks are text; anything larger is not one.
TEXT_MAX_BYTES = 32 * 1024 * 1024  # 32 MiB
MAX_REDIRECTS = 5


class UnsafeUrlError(ValueError):
    """The URL points somewhere we refuse to fetch from."""


class DownloadTooLargeError(RuntimeError):
    """The response exceeded the byte ceiling for this kind of download."""


def _addresses_for(host):
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"could not resolve {host!r}: {exc}") from exc
    return {info[4][0] for info in infos}


def is_public_address(address):
    """Is this a routable public IP? Loopback, private and link-local are not."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    return parsed.is_global and not parsed.is_multicast


def check_url(url, allow_private=False, resolve=True):
    """Raise :class:`UnsafeUrlError` unless ``url`` is safe to fetch.

    ``resolve`` controls whether hostnames are looked up. Resolution is the part
    that actually stops SSRF, but it costs a DNS round trip, so it belongs at
    fetch time rather than while merely listing the links on a page. With
    ``resolve=False`` this stays a cheap syntactic screen: the scheme must be
    HTTP(S), and a literal IP address must already be public.
    """
    if not url:
        raise UnsafeUrlError("empty URL")

    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"refusing scheme {parsed.scheme!r} in {url!r}")

    host = parsed.hostname
    if not host:
        raise UnsafeUrlError(f"no host in {url!r}")

    if allow_private:
        return url

    literal = _as_ip_literal(host)
    if literal is not None:
        if not is_public_address(literal):
            raise UnsafeUrlError(
                f"refusing to fetch {url!r}: {host} is a non-public address"
            )
        return url

    if not resolve:
        # A name we have not looked up yet; safe_get re-checks with resolution
        # before any request actually goes out.
        return url

    # Every address the name maps to must be public, or a round-robin record
    # with one internal entry would slip through.
    for address in _addresses_for(host):
        if not is_public_address(address):
            raise UnsafeUrlError(
                f"refusing to fetch {url!r}: {host} resolves to the "
                f"non-public address {address}"
            )
    return url


def _as_ip_literal(host):
    """Return the address if ``host`` is written as a literal IP, else ``None``."""
    try:
        return str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        return None


def same_site(first, second):
    """Do two URLs share a registrable-ish domain? Used to decide cookie reuse."""
    def key(url):
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host

    return bool(key(first)) and key(first) == key(second)


def safe_get(session, url, allow_private=False, stream=True, timeout=60, headers=None):
    """GET ``url``, validating every redirect hop instead of trusting requests.

    ``requests`` would follow redirects for us, but then an attacker-chosen
    ``Location`` is fetched before we ever see it. Following by hand means each
    hop passes :func:`check_url` first.
    """
    current = check_url(url, allow_private)
    seen = set()

    for _ in range(MAX_REDIRECTS + 1):
        if current in seen:
            raise UnsafeUrlError(f"redirect loop at {current!r}")
        seen.add(current)

        response = session.get(
            current, stream=stream, timeout=timeout, allow_redirects=False, headers=headers
        )

        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise UnsafeUrlError(f"redirect from {current!r} without a Location")
            current = check_url(response.url and _resolve(current, location), allow_private)
            continue

        return response

    raise UnsafeUrlError(f"too many redirects starting at {url!r}")


def _resolve(base, location):
    from urllib.parse import urljoin

    return urljoin(base, location)


def stream_to_file(response, path, max_bytes=DEFAULT_MAX_BYTES, chunk_size=64 * 1024):
    """Write a response body to ``path`` via a temporary file, enforcing a cap.

    Writing straight to the final name means an interrupted transfer leaves a
    truncated file that later looks complete. Everything lands in ``.part``
    first and is only renamed once the body is fully read.
    """
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise DownloadTooLargeError(
            f"{path}: server declared {int(declared)} bytes, over the {max_bytes} limit"
        )

    partial = path + ".part"
    written = 0
    try:
        with open(partial, "wb") as handle:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise DownloadTooLargeError(
                        f"{path}: exceeded the {max_bytes} byte limit"
                    )
                handle.write(chunk)
    except BaseException:
        # Never leave a half-written .part lying around to confuse the next run.
        try:
            os.remove(partial)
        except OSError:
            pass
        raise

    os.replace(partial, path)
    return written


def read_capped(response, max_bytes=TEXT_MAX_BYTES):
    """Read a whole response into memory, but refuse an unbounded one."""
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise DownloadTooLargeError(f"response exceeded {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)
