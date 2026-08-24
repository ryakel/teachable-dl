"""Offline viewing -- upstream #63 (dead player) and #58 (broken navigation)."""

import os

import pytest

from teachable_dl.offline import (
    apply_offline_rewrite,
    build_video_block,
    is_player_iframe,
    read_manifest,
    relative_url,
    render_index,
    render_lecture_page,
    replace_player_iframes,
    rewrite_lecture_links,
    write_manifest,
)

COURSE_ROOT = "/courses/Demo"
ID_TO_PATH = {
    "111": "01-Chapter/01-Intro.html",
    "222": "01-Chapter/02-Second.html",
    "333": "02-Chapter/01-Third.html",
}


# --------------------------------------------------------------------- links

def test_absolute_lecture_links_become_relative_paths():
    """#58: '/courses/x/lectures/222' goes nowhere from a file:// page."""
    html = '<a href="/courses/demo/lectures/222">Next</a>'
    out = rewrite_lecture_links(html, "01-Chapter", ID_TO_PATH, COURSE_ROOT)
    assert 'href="02-Second.html"' in out


def test_links_across_chapters_walk_up_a_directory():
    html = '<a href="/courses/demo/lectures/333">Other chapter</a>'
    out = rewrite_lecture_links(html, "01-Chapter", ID_TO_PATH, COURSE_ROOT)
    assert 'href="../02-Chapter/01-Third.html"' in out


def test_single_quoted_hrefs_are_rewritten_too():
    html = "<a href='https://school.teachable.com/courses/demo/lectures/111'>Intro</a>"
    out = rewrite_lecture_links(html, "01-Chapter", ID_TO_PATH, COURSE_ROOT)
    assert "href='01-Intro.html'" in out


def test_unrelated_links_are_left_alone():
    html = '<a href="/pages/about">About</a><a href="https://example.com">Ext</a>'
    assert rewrite_lecture_links(html, "01-Chapter", ID_TO_PATH, COURSE_ROOT) == html


def test_lectures_that_were_not_downloaded_keep_their_original_link():
    html = '<a href="/courses/demo/lectures/999">Not downloaded</a>'
    assert rewrite_lecture_links(html, "01-Chapter", ID_TO_PATH, COURSE_ROOT) == html


def test_relative_url_percent_encodes_spaces():
    assert "%20" in relative_url("", "01 Chapter/a b.html", COURSE_ROOT)


# ------------------------------------------------------------------- players

@pytest.mark.parametrize(
    "tag",
    [
        '<iframe data-testid="embed-player-0" src="https://x/e"></iframe>',
        '<iframe src="https://player.hotmart.com/embed/abc"></iframe>',
        '<iframe src="https://fast.wistia.net/embed/iframe/x"></iframe>',
        '<iframe src="https://player.vimeo.com/video/1"></iframe>',
        '<iframe src="https://www.youtube.com/embed/x"></iframe>',
    ],
)
def test_player_iframes_are_recognised(tag):
    assert is_player_iframe(tag)


def test_non_player_iframes_are_not_touched():
    """An embedded form or PDF viewer must survive the rewrite."""
    html = '<iframe src="https://docs.example.com/form"></iframe>'
    assert not is_player_iframe(html)
    assert replace_player_iframes(html, []) == html


def test_player_iframe_is_replaced_by_a_local_video_element():
    """#63: the remote embed renders 'Your video is processing' from disk."""
    html = '<iframe data-testid="embed-player-0" src="https://player.hotmart.com/e"></iframe>'
    block = build_video_block("01-Intro.mp4", [])
    out = replace_player_iframes(html, [block])
    assert "<iframe" not in out
    assert "<video" in out and 'src="01-Intro.mp4"' in out


def test_a_player_without_a_downloaded_video_gets_an_explanatory_note():
    html = '<iframe data-testid="embed-player-0" src="https://player.hotmart.com/e"></iframe>'
    out = replace_player_iframes(html, [])
    assert "<iframe" not in out
    assert "No local video was downloaded" in out


def test_multiple_players_map_to_their_own_videos_in_order():
    html = (
        '<iframe data-testid="embed-player-0" src="https://player.hotmart.com/a"></iframe>'
        '<iframe data-testid="embed-player-1" src="https://player.hotmart.com/b"></iframe>'
    )
    out = replace_player_iframes(
        html, [build_video_block("one.mp4", []), build_video_block("two.mp4", [])]
    )
    assert out.index("one.mp4") < out.index("two.mp4")


def test_self_closing_iframes_are_handled():
    html = '<iframe data-testid="embed-player-0" src="https://player.hotmart.com/e" />'
    out = replace_player_iframes(html, [build_video_block("v.mp4", [])])
    assert "<iframe" not in out


def test_video_block_adds_a_track_for_each_subtitle():
    block = build_video_block(
        "v.mp4",
        [("en", "v.en.vtt", "v.en.vtt", True), ("es", "v.es.vtt", "v.es.vtt", False)],
    )
    assert 'srclang="en"' in block and "default" in block
    assert 'srclang="es"' in block


def test_video_urls_are_escaped_for_html():
    block = build_video_block("a&b\"c.mp4", [])
    assert "&amp;" in block and '"c.mp4"' not in block


# ------------------------------------------------------------- full document

def test_only_vtt_subtitles_become_track_elements():
    """Browsers cannot render .srt in a <track>, so link it instead."""
    entry = {
        "html": "01-Chapter/01-Intro.html",
        "title": "Intro",
        "chapter": "01-Chapter",
        "videos": [
            {
                "path": "01-Chapter/01-Intro.mp4",
                "subtitles": [
                    {"lang": "en", "path": "01-Chapter/01-Intro.en.vtt"},
                    {"lang": "es", "path": "01-Chapter/01-Intro.es.srt"},
                ],
            }
        ],
    }
    html = '<html><head></head><body><iframe data-testid="embed-player-0"></iframe></body></html>'
    out = render_lecture_page(html, entry, None, None, ID_TO_PATH, COURSE_ROOT)
    assert "01-Intro.en.vtt" in out
    assert '<track kind="subtitles" src="01-Intro.es.srt"' not in out


def test_rendered_page_gets_navigation_and_styles():
    entry = {"html": "01-Chapter/02-Second.html", "title": "Second",
             "chapter": "01-Chapter", "videos": []}
    previous = {"html": "01-Chapter/01-Intro.html", "title": "Intro", "chapter": "01-Chapter"}
    out = render_lecture_page(
        "<html><head></head><body>x</body></html>", entry, previous, None,
        ID_TO_PATH, COURSE_ROOT,
    )
    assert "teachable-dl-nav" in out
    assert "teachable-dl-offline-style" in out
    assert "Previous" in out and "Course index" in out
    assert "Next" not in out  # last lecture


def test_rendering_a_page_with_no_head_or_body_still_works():
    entry = {"html": "a.html", "title": "t", "chapter": "c", "videos": []}
    out = render_lecture_page("just text", entry, None, None, {}, COURSE_ROOT)
    assert "teachable-dl-nav" in out


def test_index_groups_lectures_by_chapter():
    entries = [
        {"html": "01-A/01-One.html", "title": "One", "chapter": "01-A", "videos": [{}]},
        {"html": "01-A/02-Two.html", "title": "Two", "chapter": "01-A"},
        {"html": "02-B/01-Three.html", "title": "Three", "chapter": "02-B"},
    ]
    out = render_index("My Course", entries)
    assert out.count("<h2>") == 2
    assert out.index("01-A") < out.index("02-B")
    assert "1 video(s)" in out


def test_index_escapes_titles():
    out = render_index("A & B", [{"html": "a.html", "title": "<script>", "chapter": "c"}])
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


# ------------------------------------------------------------ end to end I/O

def test_rewrite_runs_over_a_real_directory_tree(tmp_path):
    course_root = tmp_path / "Course"
    chapter = course_root / "01-Chapter"
    chapter.mkdir(parents=True)

    (chapter / "01-Intro.html").write_text(
        '<html><head></head><body>'
        '<a href="/courses/demo/lectures/222">Next</a>'
        '<iframe data-testid="embed-player-0" src="https://player.hotmart.com/e"></iframe>'
        "</body></html>",
        encoding="utf-8",
    )
    (chapter / "02-Second.html").write_text(
        "<html><head></head><body>second</body></html>", encoding="utf-8"
    )
    (course_root / "course.html").write_text(
        '<a href="/courses/demo/lectures/111">Intro</a>', encoding="utf-8"
    )

    manifest = {
        "title": "Course",
        "lectures": [
            {
                "lecture_id": "111",
                "title": "Intro",
                "chapter": "01-Chapter",
                "html": "01-Chapter/01-Intro.html",
                "videos": [{"path": "01-Chapter/01-Intro.mp4", "subtitles": []}],
            },
            {
                "lecture_id": "222",
                "title": "Second",
                "chapter": "01-Chapter",
                "html": "01-Chapter/02-Second.html",
                "videos": [],
            },
        ],
    }

    assert apply_offline_rewrite(str(course_root), manifest) == 2

    intro = (chapter / "01-Intro.html").read_text(encoding="utf-8")
    assert 'href="02-Second.html"' in intro
    assert "<iframe" not in intro
    assert 'src="01-Intro.mp4"' in intro

    # The curriculum page is rewritten from the course root, not a chapter dir.
    assert 'href="01-Chapter/01-Intro.html"' in (course_root / "course.html").read_text()
    assert (course_root / "index.html").is_file()


def test_rewrite_skips_pages_that_were_never_saved(tmp_path):
    course_root = tmp_path / "Course"
    course_root.mkdir()
    manifest = {"title": "C", "lectures": [{"lecture_id": "1", "html": "missing.html"}]}
    assert apply_offline_rewrite(str(course_root), manifest) == 0


def test_manifest_round_trips_unicode(tmp_path):
    manifest = {"title": "Курс", "lectures": [{"title": "日本語"}]}
    write_manifest(str(tmp_path), manifest)
    assert read_manifest(str(tmp_path)) == manifest


def test_reading_a_missing_manifest_returns_none(tmp_path):
    assert read_manifest(str(tmp_path)) is None
