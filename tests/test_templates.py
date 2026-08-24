"""Curriculum parsing -- upstream #43, #49, #54 ("unsupported course template")."""

from teachable_dl.templates import (
    Course,
    build_course_from_entries,
    chapter_dir_name,
    lecture_id_from_url,
)


def entry(href, title="", section="", locked=False):
    return {"href": href, "title": title, "section": section, "locked": locked}


def test_lecture_id_is_extracted_from_the_url():
    assert lecture_id_from_url("https://x.teachable.com/courses/abc/lectures/36979557") == "36979557"
    assert lecture_id_from_url("https://x.teachable.com/courses/abc") is None
    assert lecture_id_from_url(None) is None


def test_chapter_directories_are_numbered_and_sanitised():
    assert chapter_dir_name(1, "Getting Started") == "01-Getting-Started"
    assert chapter_dir_name(12, "A/B") == "12-A-B"


def test_unknown_templates_still_yield_lectures():
    """#49/#54: the fallback keeps an unrecognised theme downloadable."""
    course = build_course_from_entries(
        Course(title="T"),
        [
            entry("https://x/courses/c/lectures/1", "Intro", "Start"),
            entry("https://x/courses/c/lectures/2", "Setup", "Start"),
            entry("https://x/courses/c/lectures/3", "Deep dive", "Advanced"),
        ],
    )
    assert len(course.lectures) == 3
    assert course.lectures[0].chapter == "01-Start"
    assert course.lectures[2].chapter == "02-Advanced"


def test_lecture_numbering_restarts_in_each_chapter():
    course = build_course_from_entries(
        Course(title="T"),
        [
            entry("https://x/lectures/1", "A", "One"),
            entry("https://x/lectures/2", "B", "One"),
            entry("https://x/lectures/3", "C", "Two"),
        ],
    )
    assert [l.basename for l in course.lectures] == ["01-A", "02-B", "01-C"]


def test_lectures_without_a_heading_land_in_a_single_group():
    course = build_course_from_entries(
        Course(title="T"),
        [entry("https://x/lectures/1", "A"), entry("https://x/lectures/2", "B")],
    )
    assert {l.chapter for l in course.lectures} == {"01-Lectures"}


def test_locked_lectures_are_skipped():
    course = build_course_from_entries(
        Course(title="T"),
        [
            entry("https://x/lectures/1", "Free", "S"),
            entry("https://x/lectures/2", "Drip locked", "S", locked=True),
        ],
    )
    assert [l.title for l in course.lectures] == ["Free"]


def test_untitled_lectures_fall_back_to_their_id():
    course = build_course_from_entries(Course(title="T"), [entry("https://x/lectures/42")])
    assert course.lectures[0].title == "lecture-42"


def test_entries_without_a_url_are_ignored():
    course = build_course_from_entries(Course(title="T"), [entry("", "No link", "S")])
    assert course.lectures == []


def test_non_latin_chapter_and_lecture_titles_survive():
    """#37 again, this time on the curriculum side."""
    course = build_course_from_entries(
        Course(title="T"), [entry("https://x/lectures/1", "Урок", "Глава")]
    )
    assert course.lectures[0].chapter == "01-Глава"
    assert course.lectures[0].title == "Урок"


def test_ascii_mode_applies_to_chapters_too():
    course = build_course_from_entries(
        Course(title="T"), [entry("https://x/lectures/1", "Café", "Módulo")], ascii_only=True
    )
    assert course.lectures[0].chapter == "01-Modulo"
    assert course.lectures[0].title == "Cafe"
