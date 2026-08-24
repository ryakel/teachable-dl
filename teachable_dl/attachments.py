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

from .utils import clean_string, truncate_to_byte_budget

logger = logging.getLogger(__name__)

#: Attachment blocks we know how to save, and how to find the payload in each.
ATTACHMENT_SELECTORS = [
    # Downloadable files of any kind (PDF, MP3, ZIP, ...)
    # ``:not(...-type-video)`` matters: the lecture video is fetched by the
    # downloader itself, and matching it here would download it a second time.
    (By.CSS_SELECTOR,
     ".lecture-attachment:not(.lecture-attachment-type-video) a[href*='/attachments/']"),
    (By.CSS_SELECTOR, ".lecture-attachment-type-file a[href]"),
    (By.CSS_SELECTOR, ".lecture-attachment-type-pdf_embed a[href]"),
    (By.CSS_SELECTOR, ".lecture-attachment-type-audio a[href]"),
    (By.CSS_SELECTOR, ".lecture-attachment-type-image a[href]"),
    (By.CSS_SELECTOR, "a.download-link[href]"),
    (By.CSS_SELECTOR, "a[download]"),
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


def _is_downloadable(url):
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    return not any(host in parsed.netloc for host in _PLAYER_HOSTS)


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
        for name, value in self.browser.cookies_for_requests().items():
            session.cookies.set(name, value)
        return session

    def collect_urls(self):
        """Every distinct attachment URL on the lecture page currently open."""
        urls = []
        seen = set()

        def add(url):
            if _is_downloadable(url) and url not in seen:
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
            path = self.download_url(session, url, target_dir, f"{index:02d}-attachment")
            if path:
                saved.append(path)
        return saved

    def download_url(self, session, url, target_dir, fallback_name):
        try:
            response = session.get(url, stream=True, timeout=60, allow_redirects=True)
            response.raise_for_status()
        except Exception as exc:
            logger.warning("Could not download attachment %s: %s", url, exc)
            return None

        content_type = (response.headers.get("Content-Type") or "").lower()
        if "text/html" in content_type:
            # We were handed a login or error page rather than the file itself.
            logger.warning("Skipping attachment %s: server returned an HTML page", url)
            response.close()
            return None

        filename = filename_from_response(
            url, response, fallback_name, ascii_only=self.settings.ascii_filenames
        )
        file_path = os.path.join(target_dir, filename)

        if self.settings.resume and os.path.isfile(file_path) and os.path.getsize(file_path) > 0:
            logger.info("Skipping existing attachment: %s", filename)
            response.close()
            return file_path

        try:
            with open(file_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        handle.write(chunk)
        except OSError as exc:
            logger.warning("Could not write attachment %s: %s", filename, exc)
            return None
        finally:
            response.close()

        logger.info("Downloaded attachment: %s", filename)
        return file_path
