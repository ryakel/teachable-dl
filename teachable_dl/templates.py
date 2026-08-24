"""Reading a course curriculum out of whatever template the school uses.

Teachable ships several course themes and schools can add their own, so the old
three-way ``if/elif/elif`` ended in::

    Downloader does not support this course template. Please open an issue on github.

which is exactly what upstream issues #43, #49 and #54 report.  The three known
themes still get purpose-built parsers, but there is now a generic fallback that
simply looks for links into ``/lectures/<id>`` and groups them under the nearest
heading.  That works on any theme, so an unknown template degrades to "slightly
worse chapter names" instead of "no download at all".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.remote.webdriver import By

from .utils import clean_string, truncate_title_to_fit_file_name

logger = logging.getLogger(__name__)

LECTURE_URL_RE = re.compile(r"/lectures/(\d+)")

COURSE_TITLE_SELECTORS = [
    (By.CSS_SELECTOR, ".course__title"),
    (By.CSS_SELECTOR, "body > section > div.course-sidebar > div > h2"),
    (By.CSS_SELECTOR, ".course-sidebar h2"),
    (By.CSS_SELECTOR, "#__next .heading"),
    (By.CSS_SELECTOR, "h1.course-title"),
    (By.CSS_SELECTOR, "h1"),
]

COURSE_IMAGE_SELECTORS = [
    (By.CSS_SELECTOR, ".course-image"),
    (By.CSS_SELECTOR, "#__next img[src*='course']"),
    (By.CSS_SELECTOR, ".course__image img"),
    (By.CSS_SELECTOR, "img.course-box-image"),
]

#: Walk the DOM for lecture links and the heading each one sits under.
_GENERIC_CURRICULUM_JS = r"""
const HEADING_SELECTOR = [
  'h1', 'h2', 'h3', 'h4',
  '.section-title', '.heading',
  '[class*="section__title"]', '[class*="section-title"]',
  '[class*="chapter-title"]'
].join(',');

// Whole-token class selectors. `[class*="locked"]` also matches "unlocked",
// which silently dropped free-preview lectures from the download.
const LOCKED_SELECTOR = [
  '.drip-tag', '.lecture-locked', '.locked', '.is-locked',
  '[data-locked="true"]'
].join(',');

function clean(text) {
  return (text || '').replace(/\s+/g, ' ').trim();
}

// Titles often sit next to a duration badge. textContent glues them together
// ("Alpha lesson5:32"), so drop a trailing timestamp.
function stripDuration(text) {
  return text.replace(/\s*[\(\[]?\d{1,3}:\d{2}(?::\d{2})?[\)\]]?\s*$/, '').trim();
}

function lectureTitle(anchor) {
  const named = anchor.querySelector(
    '.lecture-name, [class*="lecture-name"], [class*="lecture-title"], .text'
  );
  if (named) return stripDuration(clean(named.textContent));
  // Otherwise take the first child that yields text, so a sibling duration
  // element is not concatenated onto the name.
  for (const node of anchor.childNodes) {
    const text = stripDuration(clean(node.textContent || ''));
    if (text) return text;
  }
  return stripDuration(clean(anchor.textContent));
}

// The nearest heading *preceding* the anchor, not merely the first heading
// anywhere inside an ancestor. Taking the first meant a flat curriculum, where
// every heading and link are siblings, reported chapter 1 for every lecture and
// collapsed the whole course into one directory.
function nearestHeading(anchor) {
  let node = anchor.parentElement;
  for (let depth = 0; node && depth < 12; depth++, node = node.parentElement) {
    let best = '';
    for (const heading of node.querySelectorAll(HEADING_SELECTOR)) {
      // Skip a heading wrapping the anchor, and one nested inside it (which is
      // the lecture's own title, not its chapter).
      if (heading.contains(anchor) || anchor.contains(heading)) continue;
      const position = anchor.compareDocumentPosition(heading);
      if (position & Node.DOCUMENT_POSITION_PRECEDING) {
        const text = clean(heading.textContent);
        if (text) best = text;   // keep the last preceding one = the nearest
      }
    }
    if (best) return best;
  }
  return '';
}

const anchors = Array.from(document.querySelectorAll('a[href*="/lectures/"]'));
const seen = new Set();
const results = [];

for (const anchor of anchors) {
  const href = (anchor.href || '').split('#')[0];
  const found = href.match(/\/lectures\/(\d+)/);
  if (!found) continue;

  // Dedupe on the lecture id, not the raw href: Teachable links the same
  // lecture with and without a query string ("?from=sidebar"), which used to
  // scrape and download it twice.
  const id = found[1];
  if (seen.has(id)) continue;
  seen.add(id);

  results.push({
    href: href,
    title: lectureTitle(anchor),
    section: nearestHeading(anchor),
    locked: !!anchor.closest(LOCKED_SELECTOR)
  });
}
return results;
"""

_UNHIDE_JS = (
    "document.querySelectorAll('.hidden').forEach(e => e.classList.remove('hidden'));"
)


@dataclass
class Lecture:
    url: str
    title: str
    index: int
    chapter: str
    lecture_id: str | None = None

    @property
    def basename(self):
        return "{:02d}-{}".format(self.index, self.title)


@dataclass
class Course:
    title: str
    lectures: list = field(default_factory=list)
    image_url: str | None = None
    template: str = "unknown"


def lecture_id_from_url(url):
    match = LECTURE_URL_RE.search(url or "")
    return match.group(1) if match else None


def chapter_dir_name(index, raw_title, ascii_only=False):
    cleaned = clean_string(raw_title or f"Chapter-{index}", ascii_only=ascii_only)
    return "{:02d}-{}".format(index, truncate_title_to_fit_file_name(cleaned))


class CurriculumParser:
    """Turns the course page currently loaded in the browser into a :class:`Course`."""

    def __init__(self, browser, settings):
        self.browser = browser
        self.settings = settings

    # ------------------------------------------------------------- dispatch

    def parse(self, course_url):
        self.browser.handle_cloudflare_if_present()
        self.browser.wait_for_body()

        template, parser = self._detect_template()
        logger.info("Detected course template: %s", template)

        try:
            course = parser()
        except Exception as exc:
            logger.warning("The %s parser failed (%s); falling back to generic", template, exc)
            course = self._parse_generic()
            template = "generic"

        if not course.lectures and template != "generic":
            logger.warning("The %s parser found no lectures; falling back to generic", template)
            course = self._parse_generic()
            template = "generic"

        course.template = template

        if not course.lectures:
            logger.error(
                "No lectures found on %s. If the course opens normally in a browser, "
                "please open an issue with the course URL so the template can be added.",
                course_url,
            )
        else:
            logger.info(
                "Found %s lecture(s) across %s chapter(s)",
                len(course.lectures),
                len({lecture.chapter for lecture in course.lectures}),
            )
        return course

    def _detect_template(self):
        if self._exists(By.ID, "__next"):
            return "next", self._parse_next
        if self._exists(By.CLASS_NAME, "course-mainbar"):
            return "classic", self._parse_classic
        if self._exists(By.CSS_SELECTOR, ".block__curriculum"):
            return "colossal", self._parse_colossal
        logger.info("Unknown course template, using the generic parser")
        return "generic", self._parse_generic

    def _exists(self, by, selector):
        try:
            return bool(self.browser.driver.find_elements(by, selector))
        except WebDriverException:
            return False

    # ---------------------------------------------------------------- pieces

    def course_title(self):
        for by, selector in COURSE_TITLE_SELECTORS:
            try:
                elements = self.browser.driver.find_elements(by, selector)
            except WebDriverException:
                continue
            for element in elements:
                try:
                    text = (element.text or "").strip()
                except WebDriverException:
                    continue
                if text:
                    return clean_string(text, ascii_only=self.settings.ascii_filenames)

        logger.warning("Could not read the course title, using the tab title instead")
        return clean_string(self.browser.driver.title, ascii_only=self.settings.ascii_filenames)

    def course_image_url(self):
        for by, selector in COURSE_IMAGE_SELECTORS:
            try:
                elements = self.browser.driver.find_elements(by, selector)
            except WebDriverException:
                continue
            for element in elements:
                try:
                    src = element.get_attribute("src")
                except WebDriverException:
                    continue
                if src:
                    # Teachable serves a resized copy; the unsized path is the original.
                    return re.sub(r"/resize=.+?/", "/", src)
        return None

    def _unhide(self):
        try:
            self.browser.driver.execute_script(_UNHIDE_JS)
        except WebDriverException as exc:
            logger.debug("Could not unhide elements: %s", exc)

    def _make_lecture(self, url, raw_title, index, chapter):
        title = clean_string(raw_title, ascii_only=self.settings.ascii_filenames)
        title = truncate_title_to_fit_file_name(title)
        return Lecture(
            url=url,
            title=title,
            index=index,
            chapter=chapter,
            lecture_id=lecture_id_from_url(url),
        )

    # --------------------------------------------------------------- parsers

    def _parse_colossal(self):
        self._unhide()
        course = Course(title=self.course_title(), image_url=self.course_image_url())

        sections = self.browser.driver.find_elements(
            By.CSS_SELECTOR, ".block__curriculum__section"
        )
        for chapter_index, section in enumerate(sections, start=1):
            raw_chapter = self._text(section, ".block__curriculum__section__title")
            chapter = chapter_dir_name(chapter_index, raw_chapter, self.settings.ascii_filenames)
            logger.info("Found chapter: %s", chapter)

            items = section.find_elements(
                By.CSS_SELECTOR, ".block__curriculum__section__list__item__link"
            )
            for lecture_index, item in enumerate(items, start=1):
                url = item.get_attribute("href")
                raw_title = self._text(
                    item, ".block__curriculum__section__list__item__lecture-name"
                ) or item.text
                if not url:
                    continue
                course.lectures.append(
                    self._make_lecture(url, raw_title, lecture_index, chapter)
                )
        return course

    def _parse_classic(self):
        course = Course(title=self.course_title(), image_url=self.course_image_url())

        sections = self.browser.driver.find_elements(By.CSS_SELECTOR, ".course-section")
        for chapter_index, section in enumerate(sections, start=1):
            raw_chapter = self._text(section, ".section-title")
            chapter = chapter_dir_name(chapter_index, raw_chapter, self.settings.ascii_filenames)
            logger.info("Found chapter: %s", chapter)

            items = section.find_elements(By.CSS_SELECTOR, ".section-item")
            for lecture_index, item in enumerate(items, start=1):
                try:
                    url = item.find_element(By.CLASS_NAME, "item").get_attribute("href")
                except WebDriverException:
                    continue
                raw_title = self._text(item, ".lecture-name") or item.text
                if not url:
                    continue
                course.lectures.append(
                    self._make_lecture(url, raw_title, lecture_index, chapter)
                )
        return course

    def _parse_next(self):
        course = Course(title=self.course_title(), image_url=self.course_image_url())

        sections = self.browser.driver.find_elements(By.CSS_SELECTOR, ".slim-section")
        for chapter_index, section in enumerate(sections, start=1):
            raw_chapter = self._text(section, ".heading")
            chapter = chapter_dir_name(chapter_index, raw_chapter, self.settings.ascii_filenames)

            if section.find_elements(By.CSS_SELECTOR, ".drip-tag"):
                logger.warning('Chapter "%s" is not available yet, skipping', chapter)
                continue

            logger.info("Found chapter: %s", chapter)
            for lecture_index, bar in enumerate(section.find_elements(By.CSS_SELECTOR, ".bar"),
                                                start=1):
                try:
                    link = bar.find_element(By.CSS_SELECTOR, ".text")
                except WebDriverException:
                    continue
                url = link.get_attribute("href")
                if not url:
                    continue
                course.lectures.append(
                    self._make_lecture(url, link.text, lecture_index, chapter)
                )
        return course

    def _parse_generic(self):
        """Theme-agnostic fallback: any link into ``/lectures/<id>`` is a lecture."""
        self._unhide()
        course = Course(title=self.course_title(), image_url=self.course_image_url())

        try:
            entries = self.browser.driver.execute_script(_GENERIC_CURRICULUM_JS) or []
        except WebDriverException as exc:
            logger.error("Generic curriculum scan failed: %s", exc)
            return course

        return build_course_from_entries(
            course, entries, ascii_only=self.settings.ascii_filenames
        )

    @staticmethod
    def _text(scope, selector):
        try:
            return scope.find_element(By.CSS_SELECTOR, selector).text
        except WebDriverException:
            return ""


def build_course_from_entries(course, entries, ascii_only=False):
    """Group flat ``{href, title, section, locked}`` records into chapters.

    Split out from the DOM walk so the grouping rules can be unit tested.
    """
    chapter_names = {}
    chapter_counters = {}
    seen_ids = set()

    for entry in entries:
        url = entry.get("href")
        if not url:
            continue

        # Guard against the same lecture appearing twice (sidebar link plus
        # "continue" button), which would download the video twice.
        identifier = lecture_id_from_url(url)
        if identifier:
            if identifier in seen_ids:
                logger.debug("Skipping duplicate lecture %s", identifier)
                continue
            seen_ids.add(identifier)
        if entry.get("locked"):
            logger.warning("Skipping locked lecture: %s", entry.get("title") or url)
            continue

        raw_section = (entry.get("section") or "").strip()
        key = raw_section or "__ungrouped__"
        if key not in chapter_names:
            chapter_names[key] = chapter_dir_name(
                len(chapter_names) + 1, raw_section or "Lectures", ascii_only
            )
            chapter_counters[key] = 0
        chapter_counters[key] += 1

        raw_title = (entry.get("title") or "").strip() or f"lecture-{lecture_id_from_url(url)}"
        title = truncate_title_to_fit_file_name(clean_string(raw_title, ascii_only=ascii_only))

        course.lectures.append(
            Lecture(
                url=url,
                title=title,
                index=chapter_counters[key],
                chapter=chapter_names[key],
                lecture_id=lecture_id_from_url(url),
            )
        )

    return course
