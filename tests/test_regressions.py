"""Regressions from the adversarial code review.

Each test here pins a defect that was found, reproduced, and fixed. They are
grouped by the lens that caught them.
"""

import os

import pytest

from teachable_dl.offline import (
    build_video_block,
    iframe_src,
    render_index,
    render_lecture_page,
    replace_player_iframes,
    rewrite_lecture_links,
    strip_previous_rewrite,
    subtitle_track_src,
)
from teachable_dl.templates import Course, build_course_from_entries
from teachable_dl.utils import clean_string

ROOT = "/courses/Demo"
IDS = {"111": "01-Ch/01-Intro.html", "222": "01-Ch/02-Second.html"}


def block(name, embed=None):
    return (embed, build_video_block(name, []))


# ------------------------------------------------- the HTML rewriter is naive

def test_an_iframe_inside_a_script_payload_is_not_rewritten():
    """Teachable is Next.js: __NEXT_DATA__ carries escaped markup and lecture
    URLs. Rewriting inside it corrupted the JSON and, worse, consumed the video
    block meant for the real player -- which then showed "no video"."""
    page = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"embed":"<iframe src=\'https://fast.wistia.net/embed/iframe/x\'></iframe>"}'
        "</script>"
        '<iframe data-testid="embed-player-0" src="https://player.hotmart.com/e"></iframe>'
    )
    out = replace_player_iframes(page, [block("real.mp4")])
    script, _, body = out.partition("</script>")
    assert "teachable-dl-player" not in script
    assert "real.mp4" in body and "<iframe" not in body


@pytest.mark.parametrize(
    "wrapper",
    [
        '<!-- <iframe src="https://player.vimeo.com/video/9"></iframe> -->',
        '<noscript><iframe src="https://www.youtube.com/embed/x"></iframe></noscript>',
    ],
)
def test_commented_out_and_noscript_players_do_not_steal_the_video(wrapper):
    page = wrapper + '<iframe data-testid="embed-player-0" src="https://player.hotmart.com/e"></iframe>'
    out = replace_player_iframes(page, [block("real.mp4")])
    assert wrapper in out
    assert "real.mp4" in out.split(wrapper)[-1]


def test_lecture_urls_inside_inline_javascript_are_left_alone():
    page = '<script>window.location.href = "/courses/d/lectures/222";</script>'
    assert rewrite_lecture_links(page, "01-Ch", IDS, ROOT) == page


@pytest.mark.parametrize("attr", ["data-href", "xlink:href"])
def test_lookalike_attributes_are_not_rewritten(attr):
    """The page's own JS and SVG read these back; mutating them breaks it."""
    page = f'<a {attr}="/courses/d/lectures/222">x</a>'
    assert rewrite_lecture_links(page, "01-Ch", IDS, ROOT) == page


def test_a_real_href_is_still_rewritten():
    page = '<a href="/courses/d/lectures/222">x</a>'
    assert 'href="02-Second.html"' in rewrite_lecture_links(page, "01-Ch", IDS, ROOT)


def test_a_slash_gt_inside_an_attribute_does_not_truncate_the_tag():
    page = '<iframe title="Lesson a/>b" src="https://player.vimeo.com/video/7"></iframe><p>after</p>'
    out = replace_player_iframes(page, [block("v.mp4")])
    assert "<iframe" not in out
    assert "<p>after</p>" in out
    assert 'b"' not in out          # no leftover attribute text rendered as page copy


def test_iframe_src_resolves_html_entities():
    assert iframe_src('<iframe src="https://h/e?a=1&amp;b=2">') == "https://h/e?a=1&b=2"


# ------------------------------------ videos must land under the right player

def test_a_video_never_lands_under_a_different_players_iframe():
    """With one of two downloads failed, block 0 used to fill the first slot --
    showing the wrong lecture's video with no error at all."""
    page = (
        '<iframe src="https://fast.wistia.net/embed/iframe/A"></iframe>'
        '<iframe src="https://www.youtube.com/embed/B"></iframe>'
    )
    out = replace_player_iframes(page, [block("second.mp4", "https://www.youtube.com/embed/B")])
    first, second = out.split('<div class="teachable-dl-')[1:3]
    assert first.startswith("missing")
    assert "second.mp4" in second


def test_manifest_order_does_not_have_to_match_document_order():
    page = (
        '<iframe src="https://fast.wistia.net/embed/iframe/A"></iframe>'
        '<iframe src="https://www.youtube.com/embed/B"></iframe>'
    )
    out = replace_player_iframes(
        page,
        [block("b.mp4", "https://www.youtube.com/embed/B"),
         block("a.mp4", "https://fast.wistia.net/embed/iframe/A")],
    )
    first, second = out.split('<div class="teachable-dl-')[1:3]
    assert "a.mp4" in first and "b.mp4" in second


def test_manifests_without_embed_urls_still_map_positionally():
    """Courses downloaded before embed URLs were recorded must keep working."""
    page = (
        '<iframe src="https://fast.wistia.net/embed/iframe/A"></iframe>'
        '<iframe src="https://www.youtube.com/embed/B"></iframe>'
    )
    out = replace_player_iframes(
        page, [build_video_block("one.mp4", []), build_video_block("two.mp4", [])]
    )
    first, second = out.split('<div class="teachable-dl-')[1:3]
    assert "one.mp4" in first and "two.mp4" in second


# ------------------------------------------------------ generated page markup

def test_every_chapter_list_is_closed():
    """Only the final </ul> was emitted, so each chapter nested in the previous."""
    index = render_index("C", [
        {"html": f"{c}.html", "title": c, "chapter": f"Ch{c}"} for c in "ABC"
    ])
    assert index.count("<ul>") == index.count("</ul>") == 3


def test_rewriting_twice_does_not_stack_navigation_bars():
    entry = {"html": "01-Ch/01-Intro.html", "title": "I", "chapter": "01-Ch", "videos": []}
    page = "<html><head></head><body>x</body></html>"
    once = render_lecture_page(page, entry, None, None, IDS, ROOT)
    twice = render_lecture_page(once, entry, None, None, IDS, ROOT)
    assert twice.count('<nav class="teachable-dl-nav"') == 1
    assert twice.count('<style id="teachable-dl-offline-style"') == 1


def test_strip_previous_rewrite_leaves_original_content_intact():
    page = '<body><nav class="teachable-dl-nav">x</nav><p>real</p></body>'
    assert strip_previous_rewrite(page) == "<body><p>real</p></body>"


def test_a_malformed_manifest_entry_does_not_abort_the_index():
    """Only OSError was caught, so a None title raised AttributeError and killed
    the rewrite of every other page."""
    assert render_index("C", [{"html": "a.html", "title": None, "chapter": None}])


def test_subtitles_are_inlined_so_tracks_load_from_file_urls(tmp_path):
    """Chromium refuses to load a <track> from a sibling file on a file:// page."""
    (tmp_path / "s.vtt").write_bytes(b"WEBVTT\n\n00:00.000 --> 00:01.000\nhi\n")
    src = subtitle_track_src(str(tmp_path), "s.vtt", "s.vtt")
    assert src.startswith("data:text/vtt;base64,")


def test_an_oversized_subtitle_falls_back_to_a_plain_link(tmp_path):
    (tmp_path / "s.vtt").write_bytes(b"x" * (2 * 1024 * 1024))
    assert subtitle_track_src(str(tmp_path), "s.vtt", "s.vtt") == "s.vtt"


def test_a_missing_subtitle_falls_back_to_a_plain_link(tmp_path):
    assert subtitle_track_src(str(tmp_path), "nope.vtt", "nope.vtt") == "nope.vtt"


# ------------------------------------------------------------ curriculum data

def test_the_same_lecture_linked_twice_is_downloaded_once():
    """Teachable links a lecture from the sidebar and from "continue", the
    second with a query string, which used to scrape and download it twice."""
    course = build_course_from_entries(Course(title="T"), [
        {"href": "https://x/courses/c/lectures/501", "title": "L", "section": "S"},
        {"href": "https://x/courses/c/lectures/501?from=sidebar", "title": "L", "section": "S"},
    ])
    assert len(course.lectures) == 1


# ---------------------------------------------------- remote input is hostile

@pytest.mark.parametrize(
    "title",
    [
        "../../../../etc/passwd",
        "../../.ssh/authorized_keys",
        "/etc/shadow",
        "C:\\Windows\\System32\\config",
        "..\\..\\Windows",
        "....//....//etc",
    ],
)
def test_a_hostile_course_title_cannot_escape_the_output_directory(title, tmp_path):
    """Course, chapter and lecture titles all come from the school's HTML."""
    cleaned = clean_string(title)
    assert os.sep not in cleaned and "/" not in cleaned and "\\" not in cleaned

    resolved = os.path.realpath(os.path.join(str(tmp_path), cleaned))
    assert resolved.startswith(os.path.realpath(str(tmp_path)) + os.sep)


def test_a_title_of_only_separators_still_yields_a_usable_name():
    assert clean_string("../..") not in ("", ".", "..")


def test_browser_cookies_keep_their_domain():
    """``session.cookies.set(name, value)`` creates a domain-less cookie, which
    requests sends to *every* host -- so one redirect to an attacker-controlled
    server handed over the user's Teachable session."""
    from tests.conftest import make_browser

    browser = make_browser(cookies=[
        {"name": "_teachable_session", "value": "SECRET",
         "domain": ".teachable.com", "path": "/", "secure": True},
    ])
    cookie = browser.cookies_for_requests()[0]
    assert cookie["domain"] == ".teachable.com"
    assert cookie["path"] == "/"


def test_a_scoped_session_does_not_leak_cookies_to_a_third_party():
    """A domain-less cookie is sent to every host, so a redirect to an
    attacker-controlled server handed over the user's Teachable session.

    Uses the stdlib cookie jar's own domain matching rather than a hand-rolled
    fake, so it tests the behaviour requests actually relies on.
    """
    from urllib.request import Request

    import requests

    session = requests.Session()
    session.cookies.set("_teachable_session", "SECRET", domain=".teachable.com", path="/")

    ours = Request("https://school.teachable.com/x")
    session.cookies.add_cookie_header(ours)
    assert ours.get_header("Cookie") == "_teachable_session=SECRET"

    theirs = Request("https://evil.example.com/steal")
    session.cookies.add_cookie_header(theirs)
    assert theirs.get_header("Cookie") is None


def test_the_old_domainless_form_really_did_leak():
    """Pins why the fix is necessary: without a domain the cookie goes anywhere."""
    from urllib.request import Request

    import requests

    session = requests.Session()
    session.cookies.set("_teachable_session", "SECRET")

    theirs = Request("https://evil.example.com/steal")
    session.cookies.add_cookie_header(theirs)
    assert theirs.get_header("Cookie") == "_teachable_session=SECRET"
