"""The orchestrator: log in, walk the curriculum, save everything, rewrite for offline use."""

import json
import logging
import os
import time

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.remote.webdriver import By

from . import __version__, offline
from .attachments import AttachmentDownloader, filename_from_response
from .auth import Authenticator, LoginError
from .browser import Browser, SessionLostError, is_dead_session_error, render_pdf
from .media import MediaDownloader
from .templates import CurriculumParser

logger = logging.getLogger(__name__)

#: Iframes that host a lecture video.
VIDEO_IFRAME_SELECTORS = [
    (By.XPATH, "//iframe[starts-with(@data-testid, 'embed-player')]"),
    (By.CSS_SELECTOR, "iframe[src*='player.hotmart.com']"),
    (By.CSS_SELECTOR, "iframe[src*='fast.wistia.net']"),
    (By.CSS_SELECTOR, "iframe[src*='player.vimeo.com']"),
    (By.CSS_SELECTOR, "iframe[src*='youtube.com/embed']"),
]


class CourseDownloader:
    def __init__(self, settings):
        self.settings = settings
        self.browser = Browser(settings)
        self.auth = Authenticator(self.browser, settings)
        self.media = MediaDownloader(settings)
        self.attachments = AttachmentDownloader(self.browser, settings)
        self._authenticated_for = None
        # After an automatic browser restart we have a fresh, logged-out Chrome.
        self.browser.on_restart = self._reauthenticate

    # -------------------------------------------------------------- lifecycle

    def close(self):
        self.browser.quit()
        if os.path.exists("cookies.txt"):
            os.remove("cookies.txt")

    def _reauthenticate(self):
        if self._authenticated_for is None:
            return
        logger.info("Logging back in after the browser restart")
        try:
            self.auth.authenticate(self._authenticated_for)
        except Exception as exc:
            logger.error("Could not log back in: %s", exc)

    # ------------------------------------------------------------------- run

    def run(self, course_urls):
        """Log in once, then download each course in turn. Returns an exit code."""
        if not course_urls:
            logger.error("No course URLs given")
            return 1

        try:
            self._authenticated_for = course_urls[0]
            self.auth.authenticate(course_urls[0])
        except LoginError as exc:
            logger.error("%s", exc)
            return 1
        except Exception as exc:
            logger.error("Could not log in: %s", exc, exc_info=self.settings.verbose)
            return 1

        failures = 0
        for course_url in course_urls:
            try:
                self.download_course(course_url)
            except SessionLostError as exc:
                logger.error("Browser session lost while downloading %s: %s", course_url, exc)
                failures += 1
            except Exception as exc:
                logger.error(
                    "Could not download course %s: %s",
                    course_url,
                    exc,
                    exc_info=self.settings.verbose,
                )
                failures += 1

        return 1 if failures else 0

    # ---------------------------------------------------------------- course

    def download_course(self, course_url):
        logger.info("Starting download of course: %s", course_url)

        if self.browser.current_url != course_url:
            self.browser.get(course_url)
        self.browser.handle_cloudflare_if_present()

        course = CurriculumParser(self.browser, self.settings).parse(course_url)
        course_root = os.path.join(self.settings.output_dir, course.title)
        os.makedirs(course_root, exist_ok=True)
        logger.info("Saving to %s", course_root)

        self._save_text(os.path.join(course_root, "course.html"), self.browser.page_source)
        self._save_course_image(course, course_root)

        manifest = {
            "title": course.title,
            "url": course_url,
            "template": course.template,
            "generated_by": f"teachable-dl {__version__}",
            "lectures": [],
        }

        for lecture in course.lectures:
            try:
                manifest["lectures"].append(self.download_lecture(lecture, course_root))
            except SessionLostError:
                raise
            except Exception as exc:
                logger.error(
                    "Could not download lecture %s: %s",
                    lecture.title,
                    exc,
                    exc_info=self.settings.verbose,
                )

        offline.write_manifest(course_root, manifest)

        if self.settings.offline_rewrite:
            logger.info("Rewriting saved pages for offline viewing")
            offline.apply_offline_rewrite(course_root, manifest)

        logger.info("Finished course: %s", course.title)
        return manifest

    def _save_course_image(self, course, course_root):
        if not course.image_url:
            return
        session = self.attachments.new_session()
        try:
            response = session.get(course.image_url, timeout=30)
            response.raise_for_status()
        except Exception as exc:
            logger.warning("Could not download the course image: %s", exc)
            return
        name = filename_from_response(
            course.image_url, response, "course-image",
            ascii_only=self.settings.ascii_filenames,
        )
        if not os.path.splitext(name)[1]:
            name += ".jpg"
        try:
            with open(os.path.join(course_root, name), "wb") as handle:
                handle.write(response.content)
            logger.info("Downloaded the course image")
        except OSError as exc:
            logger.warning("Could not save the course image: %s", exc)

    # --------------------------------------------------------------- lecture

    def download_lecture(self, lecture, course_root):
        chapter_dir = os.path.join(course_root, lecture.chapter)
        os.makedirs(chapter_dir, exist_ok=True)

        logger.info("Lecture: %s / %s", lecture.chapter, lecture.basename)
        self.browser.get(lecture.url)
        self.browser.handle_cloudflare_if_present()

        entry = {
            "lecture_id": lecture.lecture_id,
            "title": lecture.title,
            "chapter": lecture.chapter,
            "url": lecture.url,
            "html": None,
            "videos": [],
            "attachments": [],
        }

        html_path = os.path.join(chapter_dir, lecture.basename + ".html")
        if self._save_text(html_path, self.browser.page_source):
            entry["html"] = os.path.relpath(html_path, course_root)

        if self.settings.save_pdf:
            self._save_pdf(lecture, chapter_dir)
        if self.settings.save_screenshot:
            self._save_screenshot(lecture, chapter_dir)

        if self.settings.download_attachments:
            try:
                saved = self.attachments.download_all(lecture.basename, chapter_dir)
                entry["attachments"] = [os.path.relpath(p, course_root) for p in saved]
            except Exception as exc:
                logger.warning("Could not download attachments for %s: %s", lecture.title, exc)

        direct = self._download_direct_video(lecture, chapter_dir)
        if direct:
            entry["videos"].append(
                {"path": os.path.relpath(direct, course_root), "subtitles": []}
            )
        else:
            entry["videos"] = self._download_embedded_videos(lecture, chapter_dir, course_root)

        if self.settings.complete_lecture:
            self._complete_lecture()

        return entry

    # ---------------------------------------------------------------- videos

    def _download_direct_video(self, lecture, output_path):
        """Some lectures offer the mp4 as a plain download rather than a player.

        The old implementation drove Chrome's own downloader over CDP and then
        polled ``os.listdir`` in a ``while True`` whose default timeout of ``-1``
        meant it could spin forever.  Fetching the URL with the session cookies
        is both faster and terminates.
        """
        try:
            blocks = self.browser.driver.find_elements(
                By.CSS_SELECTOR, ".lecture-attachment-type-video a[href]"
            )
        except WebDriverException as exc:
            if is_dead_session_error(exc):
                raise SessionLostError(str(exc)) from exc
            return None

        for element in blocks:
            try:
                url = element.get_attribute("href")
            except WebDriverException:
                continue
            if not url:
                continue

            session = self.attachments.new_session()
            path = self.attachments.download_url(
                session, url, output_path, lecture.basename
            )
            if path:
                # Normalise the name so the offline rewriter can find it.
                wanted = os.path.join(
                    output_path, lecture.basename + os.path.splitext(path)[1]
                )
                if path != wanted:
                    try:
                        os.replace(path, wanted)
                        path = wanted
                    except OSError:
                        pass
                logger.info("Downloaded video file: %s", os.path.basename(path))
                return path
        return None

    def _video_sources(self):
        """Every playable stream on the lecture page, with the embed that served it."""
        sources = []
        seen_frames = set()

        for by, selector in VIDEO_IFRAME_SELECTORS:
            try:
                iframes = self.browser.driver.find_elements(by, selector)
            except WebDriverException as exc:
                if is_dead_session_error(exc):
                    raise SessionLostError(str(exc)) from exc
                continue

            for iframe in iframes:
                try:
                    embed_url = iframe.get_attribute("src") or ""
                except WebDriverException:
                    continue
                if embed_url in seen_frames:
                    continue
                seen_frames.add(embed_url)

                stream_url = self._stream_url_from_iframe(iframe)
                # Fall back to the embed URL itself: yt-dlp can resolve Wistia,
                # Vimeo and YouTube embeds directly.
                sources.append(
                    {"embed_url": embed_url, "stream_url": stream_url or embed_url}
                )

        return [source for source in sources if source["stream_url"]]

    def _stream_url_from_iframe(self, iframe):
        """Pull the signed media URL out of a Hotmart player frame."""
        try:
            self.browser.driver.switch_to.frame(iframe)
        except WebDriverException as exc:
            if is_dead_session_error(exc):
                raise SessionLostError(str(exc)) from exc
            return None

        try:
            element = self.browser.driver.find_element(By.ID, "__NEXT_DATA__")
            data = json.loads(element.get_attribute("innerHTML"))
            assets = data["props"]["pageProps"]["applicationData"]["mediaAssets"]
            return assets[0]["urlEncrypted"]
        except (WebDriverException, KeyError, IndexError, ValueError, TypeError) as exc:
            logger.debug("No Hotmart payload in this frame: %s", exc)
            return None
        finally:
            try:
                self.browser.driver.switch_to.default_content()
            except WebDriverException:
                pass

    def _download_embedded_videos(self, lecture, output_path, course_root):
        sources = self._video_sources()
        if not sources:
            logger.info("No video on this lecture page")
            return []

        results = []
        for index, source in enumerate(sources, start=1):
            # Only suffix when there is more than one video, so single-video
            # lectures keep the plain "01-Title.mp4" name.
            basename = lecture.basename if len(sources) == 1 else f"{lecture.basename}-{index}"

            subtitles = []
            if self.settings.download_subtitles:
                try:
                    subtitles = self.media.download_subtitles(
                        source["stream_url"], basename, output_path, source["embed_url"]
                    )
                except Exception as exc:
                    logger.warning("Could not download subtitles for %s: %s", basename, exc)

            video_path = self.media.download_video(
                source["stream_url"],
                basename,
                output_path,
                embed_url=source["embed_url"],
                refresh_link=lambda i=index: self._refresh_stream_url(lecture, i),
            )
            if not video_path:
                continue

            results.append(
                {
                    "path": os.path.relpath(video_path, course_root),
                    "subtitles": [
                        {
                            "lang": self._lang_from_subtitle_path(path, basename),
                            "path": os.path.relpath(path, course_root),
                        }
                        for path in subtitles
                    ],
                }
            )

        return results

    def _refresh_stream_url(self, lecture, index):
        """Re-extract a signed stream URL after the original expired (#44, #55)."""
        logger.info("Re-opening %s to get a fresh stream URL", lecture.title)
        self.browser.get(lecture.url)
        self.browser.handle_cloudflare_if_present()
        sources = self._video_sources()
        if index <= len(sources):
            return sources[index - 1]["stream_url"]
        return None

    @staticmethod
    def _lang_from_subtitle_path(path, basename):
        name = os.path.basename(path)
        stem = name[len(basename):].lstrip(".") if name.startswith(basename) else name
        parts = stem.split(".")
        return parts[0] if parts and parts[0] else "und"

    # ----------------------------------------------------------------- pages

    def _save_text(self, path, content):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as exc:
            logger.error("Could not save %s: %s", path, exc)
            return False
        logger.debug("Saved %s", path)
        return True

    def _save_pdf(self, lecture, output_path):
        """#39: keep a print-ready copy of the lecture page."""
        target = os.path.join(output_path, lecture.basename + ".pdf")
        if self.settings.resume and os.path.isfile(target) and os.path.getsize(target) > 0:
            logger.info("Skipping existing PDF: %s", os.path.basename(target))
            return
        data = render_pdf(self.browser.driver)
        if not data:
            logger.warning(
                "Could not save %s as PDF: this browser will not print to PDF. "
                "Try --headless, or use --save-screenshot instead.",
                lecture.title,
            )
            return
        try:
            with open(target, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            logger.warning("Could not write %s: %s", target, exc)
            return
        logger.info("Saved page as PDF: %s", os.path.basename(target))

    def _save_screenshot(self, lecture, output_path):
        """#39: a full-page PNG, handy on tablets where HTML is awkward."""
        target = os.path.join(output_path, lecture.basename + ".png")
        if self.settings.resume and os.path.isfile(target) and os.path.getsize(target) > 0:
            logger.info("Skipping existing screenshot: %s", os.path.basename(target))
            return
        try:
            self.browser.driver.save_screenshot(target)
            logger.info("Saved page screenshot: %s", os.path.basename(target))
        except Exception as exc:
            logger.warning("Could not screenshot %s: %s", lecture.title, exc)

    def _complete_lecture(self):
        try:
            self.browser.driver.switch_to.default_content()
            buttons = self.browser.driver.find_elements(By.ID, "lecture_complete_button")
            if not buttons:
                logger.debug("No complete button on this lecture")
                return
            buttons[0].click()
            logger.info("Marked the lecture as complete")
            time.sleep(2)
        except WebDriverException as exc:
            if is_dead_session_error(exc):
                raise SessionLostError(str(exc)) from exc
            logger.warning("Could not mark the lecture complete: %s", exc)
