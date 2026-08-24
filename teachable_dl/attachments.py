"""Downloading lecture attachments.

Upstream issue #38 asks for PDF and MP3 attachments to be picked up.  The old
code only looked at ``lecture-attachment-type-file`` and fetched the URLs with
``wget.download``, which sends no cookies -- so anything behind the enrolment
check came back as an HTML error page saved under a ``.pdf`` name.  We now walk
every ``lecture-attachment-type-*`` block and download through the browser's
authenticated session.
"""

import logging
import mimetypes
import os
import re
from urllib.parse import unquote, urlparse

import requests
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.remote.webdriver import By

from .netutil import (
    DownloadTooLargeError,
    UnsafeUrlError,
    check_url,
    safe_get,
    stream_to_file,
)
from .utils import clean_string, truncate_to_byte_budget

logger = logging.getLogger(__name__)

#: Attachment blocks we know how to save, and how to find the payload in each.
#: ``:not(...-type-video)`` guards every broad selector, not just the first.
#: The lecture video is fetched by the downloader itself, so ``a[download]``
#: would otherwise pull a multi-gigabyte video down a second time and store it
#: twice -- once in the chapter folder and once in the attachments folder.
_NOT_VIDEO = ":not(.lecture-attachment-type-video)"

ATTACHMENT_SELECTORS = [
    # Downloadable files of any kind (PDF, MP3, ZIP, ...)
    (By.CSS_SELECTOR, f".lecture-attachment{_NOT_VIDEO} a[href*='/attachments/']"),
    (By.CSS_SELECTOR, ".lecture-attachment-type-file a[href]"),
    (By.CSS_SELECTOR, ".lecture-attachment-type-pdf_embed a[href]"),
    (By.CSS_SELECTOR, ".lecture-attachment-type-audio a[href]"),
    (By.CSS_SELECTOR, ".lecture-attachment-type-image a[href]"),
    (By.CSS_SELECTOR, f".lecture-attachment{_NOT_VIDEO} a.download-link[href]"),
    (By.CSS_SELECTOR, f".lecture-attachment{_NOT_VIDEO} a[download]"),
]

#: Media embedded directly in the page rather than linked.
EMBEDDED_MEDIA_SELECTORS = [
    (By.CSS_SELECTOR, ".lecture-attachment-type-pdf_embed iframe[src]", "src"),
    (By.CSS_SELECTOR, ".lecture-attachment-type-pdf_embed object[data]", "data"),
    (By.CSS_SELECTOR, ".lecture-attachment-type-pdf_embed embed[src]", "src"),
    (By.CSS_SELECTOR, ".lecture-attachment-type-audio audio[src]", "src"),
    (By.CSS_SELECTOR, ".lecture-attachment-type-audio audio source[src]", "src"),
    (By.CSS_SELECTOR, ".lecture-attachment-type-image img[src]", "src"),
]

#: Anything served from these hosts is a player, not a file we can save directly.
_PLAYER_HOSTS = ("player.hotmart.com", "fast.wistia.net", "player.vimeo.com", "youtube.com")

_MAX_ATTACHMENT_NAME = 120


def _is_complete(path):
    """Non-empty and with no ``.part`` sibling left over from a failed transfer."""
    return (
        os.path.isfile(path)
        and os.path.getsize(path) > 0
        and not os.path.exists(path + ".part")
    )


def _is_player_url(url):
    """Is this a video player embed rather than a file we can save directly?"""
    parsed = urlparse(url or "")
    return any(host in parsed.netloc for host in _PLAYER_HOSTS)


def _is_downloadable(url, allow_private=False):
    """Should we fetch this URL at all?

    The old check was "the host is not one of four known players", which let
    through ``http://169.254.169.254/...`` and ``http://127.0.0.1:6379/`` --
    both perfectly valid URLs for a malicious school to put in an attachment
    link, and both reachable from the victim's machine.
    """
    if not url or _is_player_url(url):
        return False
    try:
        # Syntactic screen only: listing a page's links should not perform DNS.
        # safe_get re-checks with resolution before the request is sent.
        check_url(url, allow_private=allow_private, resolve=False)
    except UnsafeUrlError as exc:
        logger.warning("Skipping unsafe attachment URL: %s", exc)
        return False
    return True


def filename_from_response(url, response, fallback, ascii_only=False):
    """Work out a sensible filename from Content-Disposition, the URL, or a fallback."""
    disposition = response.headers.get("Content-Disposition", "") if response else ""
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", disposition, re.IGNORECASE)
    name = unquote(match.group(1)) if match else ""

    if not name:
        name = unquote(os.path.basename(urlparse(url).path))

    if not name or name in (".", "/"):
        name = fallback

    stem, extension = os.path.splitext(name)
    if not extension and response is not None:
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
        extension = mimetypes.guess_extension(content_type) or ""

    stem = clean_string(stem or fallback, ascii_only=ascii_only)
    stem = truncate_to_byte_budget(stem, _MAX_ATTACHMENT_NAME)
    return stem + extension


class AttachmentDownloader:
    def __init__(self, browser, settings):
        self.browser = browser
        self.settings = settings

    def new_session(self):
        session = requests.Session()
        session.headers.update(
            {"User-Agent": self.settings.user_agent, "Referer": self.browser.current_url}
        )
        for cookie in self.browser.cookies_for_requests():
            # Passing the domain is what stops requests from broadcasting the
            # Teachable session cookie to every host a redirect leads to.
            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie["domain"],
                path=cookie["path"],
                secure=cookie["secure"],
            )
        return session

    def collect_urls(self):
        """Every distinct attachment URL on the lecture page currently open."""
        urls = []
        seen = set()

        def add(url):
            if url not in seen and _is_downloadable(
                url, allow_private=self.settings.allow_private_hosts
            ):
                seen.add(url)
                urls.append(url)

        for by, selector in ATTACHMENT_SELECTORS:
            try:
                elements = self.browser.driver.find_elements(by, selector)
            except WebDriverException as exc:
                logger.debug("Attachment selector %s failed: %s", selector, exc)
                continue
            for element in elements:
                try:
                    add(element.get_attribute("href"))
                except WebDriverException:
                    continue

        for by, selector, attribute in EMBEDDED_MEDIA_SELECTORS:
            try:
                elements = self.browser.driver.find_elements(by, selector)
            except WebDriverException as exc:
                logger.debug("Embedded media selector %s failed: %s", selector, exc)
                continue
            for element in elements:
                try:
                    add(element.get_attribute(attribute))
                except WebDriverException:
                    continue

        return urls

    def download_all(self, basename, output_path):
        """Save every attachment for the current lecture into ``<output_path>/<basename>``."""
        urls = self.collect_urls()
        if not urls:
            logger.debug("No attachments found for %s", basename)
            return []

        target_dir = os.path.join(output_path, basename)
        os.makedirs(target_dir, exist_ok=True)

        session = self.new_session()
        saved = []
        for index, url in enumerate(urls, start=1):
            try:
                path = self.download_url(session, url, target_dir, f"{index:02d}-attachment")
            except Exception as exc:
                # One unreachable attachment must not cost us the others.
                logger.warning("Could not download attachment %s: %s", url, exc)
                continue
            if path:
                saved.append(path)
        return saved

    def download_url(self, session, url, target_dir, fallback_name, expected_path=None):
        """Fetch one URL into ``target_dir``, safely and resumably.

        ``expected_path`` lets the caller say "I am going to rename the result to
        this"; the resume check then looks at that name instead of the one
        derived from the response, which is what made direct-download videos
        re-download in full on every run.
        """
        if expected_path and self.settings.resume and _is_complete(expected_path):
            logger.info("Skipping existing file: %s", os.path.basename(expected_path))
            return expected_path

        try:
            response = safe_get(
                session, url, allow_private=self.settings.allow_private_hosts, stream=True
            )
            response.raise_for_status()
        except UnsafeUrlError as exc:
            logger.warning("Refusing to download %s: %s", url, exc)
            return None
        except Exception as exc:
            logger.warning("Could not download attachment %s: %s", url, exc)
            return None

        try:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/html" in content_type:
                # We were handed a login or error page rather than the file itself.
                logger.warning("Skipping attachment %s: server returned an HTML page", url)
                return None

            filename = filename_from_response(
                url, response, fallback_name, ascii_only=self.settings.ascii_filenames
            )
            file_path = os.path.join(target_dir, filename)

            if self.settings.resume and _is_complete(file_path):
                logger.info("Skipping existing attachment: %s", filename)
                return file_path

            try:
                stream_to_file(response, file_path, max_bytes=self.settings.max_file_bytes)
            except DownloadTooLargeError as exc:
                logger.warning("Refusing oversized attachment %s: %s", filename, exc)
                return None
            except OSError as exc:
                logger.warning("Could not write attachment %s: %s", filename, exc)
                return None
            except Exception as exc:
                # A dropped connection mid-body used to leave a truncated file
                # under the final name, which the next run then skipped as
                # "already downloaded". stream_to_file cleans up its .part.
                logger.warning("Download of %s failed part-way: %s", filename, exc)
                return None
        finally:
            response.close()

        logger.info("Downloaded attachment: %s", filename)
        return file_path
