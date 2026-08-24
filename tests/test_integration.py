"""End-to-end tests against a real browser and a synthetic course site.

These cover the half of the project the unit suite cannot reach: template
detection, curriculum scraping (the injected JavaScript actually running in a
browser engine), and the offline rewrite applied to pages a browser really
saved. No Teachable account and no network access are involved -- the site is
served from localhost by ``tests/fixtures/site.py``.

They are skipped automatically when no usable ChromeDriver/Chrome pair is
present, so the default ``pytest`` run stays green anywhere.
"""

import os

import pytest

from teachable_dl.browser import Browser
from teachable_dl.config import Settings
from teachable_dl.templates import CurriculumParser
from tests.fixtures.site import serve

pytestmark = pytest.mark.integration


def _make_driver():
    """A plain headless Chrome, or ``None`` if this machine cannot provide one.

    Deliberately not SeleniumBase's undetected mode: these tests are about our
    parsing logic in a real DOM, and uc mode wants to download its own driver.
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        return None

    options = Options()
    binary = os.environ.get("TEACHABLE_DL_TEST_CHROME")
    if binary:
        options.binary_location = binary
    for argument in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                     "--disable-gpu", "--window-size=1280,1024"):
        options.add_argument(argument)

    service = None
    driver_path = os.environ.get("TEACHABLE_DL_TEST_CHROMEDRIVER")
    if not driver_path:
        # chromedriver-py is an easy way to pin a driver matching your Chrome:
        #   pip install chromedriver-py==<your chrome major>.*
        try:
            from chromedriver_py import binary_path

            driver_path = binary_path
        except ImportError:
            driver_path = None

    if driver_path:
        from selenium.webdriver.chrome.service import Service

        service = Service(driver_path)

    try:
        return webdriver.Chrome(options=options, service=service)
    except Exception:
        return None


@pytest.fixture(scope="module")
def driver():
    instance = _make_driver()
    if instance is None:
        pytest.skip(
            "no usable Chrome/ChromeDriver pair; set TEACHABLE_DL_TEST_CHROME to a "
            "Chrome binary whose version matches the chromedriver on PATH"
        )
    yield instance
    instance.quit()


@pytest.fixture(scope="module")
def site():
    with serve() as base_url:
        yield base_url


@pytest.fixture
def browser(driver):
    """A Browser wrapping the real driver, without SeleniumBase startup."""
    wrapper = Browser.__new__(Browser)
    wrapper.settings = Settings(timeout=5, allow_private_hosts=True)
    wrapper.driver = driver
    wrapper.restarts = 0
    wrapper.on_restart = None
    return wrapper


def parse(browser, site, slug):
    url = f"{site}/courses/enrolled/{slug}"
    browser.driver.get(url)
    return CurriculumParser(browser, browser.settings).parse(url)


# ------------------------------------------------------- template detection

@pytest.mark.parametrize(
    "slug,template,lectures,chapters",
    [
        ("next", "next", 4, 2),
        ("classic", "classic", 3, 2),
        ("colossal", "colossal", 3, 2),
    ],
)
def test_each_known_template_is_parsed(browser, site, slug, template, lectures, chapters):
    course = parse(browser, site, slug)
    assert course.template == template
    assert len(course.lectures) == lectures
    assert len({lecture.chapter for lecture in course.lectures}) == chapters


def test_chapter_and_lecture_names_come_through(browser, site):
    course = parse(browser, site, "classic")
    assert course.lectures[0].chapter == "01-Module-One"
    assert course.lectures[0].title == "Kickoff"


def test_a_duration_badge_is_not_glued_onto_the_title(browser, site):
    course = parse(browser, site, "next")
    assert all(":" not in lecture.title for lecture in course.lectures)


# --------------------------------------------------- the unknown template

def test_an_unrecognised_template_still_yields_a_curriculum(browser, site):
    """Upstream #43/#49/#54: this used to be a hard 'unsupported template' stop."""
    course = parse(browser, site, "unknown")
    assert course.template == "generic"
    assert len(course.lectures) == 3


def test_the_generic_parser_separates_chapters(browser, site):
    """A flat curriculum used to collapse into a single chapter."""
    course = parse(browser, site, "unknown")
    chapters = [lecture.chapter for lecture in course.lectures]
    assert chapters == ["01-Chapter-One", "01-Chapter-One", "02-Chapter-Two"]


def test_an_unlocked_lecture_is_not_mistaken_for_a_locked_one(browser, site):
    """``[class*="locked"]`` matches "unlocked" too, which dropped free lectures."""
    course = parse(browser, site, "unknown")
    assert "Alpha" in [lecture.title for lecture in course.lectures]


def test_a_genuinely_locked_lecture_is_skipped(browser, site):
    course = parse(browser, site, "unknown")
    assert "Members-only" not in [lecture.title for lecture in course.lectures]


def test_a_lecture_linked_twice_is_only_listed_once(browser, site):
    course = parse(browser, site, "unknown")
    ids = [lecture.lecture_id for lecture in course.lectures]
    assert len(ids) == len(set(ids))


# ------------------------------------------ saved pages survive the rewrite

def test_a_really_saved_page_rewrites_correctly(browser, site, tmp_path):
    """Runs the rewriter over HTML a browser actually serialised, which is where
    the embedded __NEXT_DATA__ payload bites."""
    from teachable_dl import offline

    course = parse(browser, site, "next")
    course_root = tmp_path / "Course"
    chapter = course_root / course.lectures[0].chapter
    chapter.mkdir(parents=True)

    entries = []
    for lecture in course.lectures[:2]:
        browser.driver.get(lecture.url)
        page = browser.driver.page_source
        relative = f"{lecture.chapter}/{lecture.basename}.html"
        (course_root / relative).parent.mkdir(parents=True, exist_ok=True)
        (course_root / relative).write_text(page, encoding="utf-8")
        entries.append(
            {
                "lecture_id": lecture.lecture_id,
                "title": lecture.title,
                "chapter": lecture.chapter,
                "html": relative,
                "videos": [
                    {
                        "embed_url": f"https://player.hotmart.com/embed/{lecture.lecture_id}",
                        "path": f"{lecture.chapter}/{lecture.basename}.mp4",
                        "subtitles": [],
                    }
                ],
            }
        )

    offline.apply_offline_rewrite(str(course_root), {"title": "Course", "lectures": entries})

    first = (course_root / entries[0]["html"]).read_text(encoding="utf-8")
    # The visible player became a local <video>...
    assert "<video" in first and entries[0]["videos"][0]["path"].split("/")[-1] in first
    # ...the decoy inside the JSON payload was left alone...
    assert "player.hotmart.com/embed/decoy" in first
    # ...and so were the commented-out and noscript decoys.
    assert "player.vimeo.com/video/decoy" in first
    assert "www.youtube.com/embed/decoy" in first
    assert (course_root / "index.html").is_file()


def test_sidebar_links_point_at_local_files_after_the_rewrite(browser, site, tmp_path):
    from teachable_dl import offline

    course = parse(browser, site, "next")
    course_root = tmp_path / "Course"
    entries = []
    for lecture in course.lectures[:2]:
        browser.driver.get(lecture.url)
        relative = f"{lecture.chapter}/{lecture.basename}.html"
        (course_root / relative).parent.mkdir(parents=True, exist_ok=True)
        (course_root / relative).write_text(browser.driver.page_source, encoding="utf-8")
        entries.append({"lecture_id": lecture.lecture_id, "title": lecture.title,
                        "chapter": lecture.chapter, "html": relative, "videos": []})

    offline.apply_offline_rewrite(str(course_root), {"title": "Course", "lectures": entries})

    first = (course_root / entries[0]["html"]).read_text(encoding="utf-8")
    assert f'href="{os.path.basename(entries[1]["html"])}"' in first
