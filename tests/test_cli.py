"""Argument handling and the flags added for the feature requests."""

import os

import pytest

from teachable_dl.cli import (
    build_parser,
    read_urls_from_file,
    resolve_credentials,
    run_rewrite_only,
    settings_from_args,
)


def parse(*argv):
    return build_parser().parse_args(list(argv))


def settings_for(*argv):
    args = parse(*argv)
    return settings_from_args(args, "e@example.com", "pw", None)


# ----------------------------------------------------- backwards compatibility

def test_the_original_invocation_still_parses():
    """The README's command must keep working for existing users."""
    args = parse("--url", "https://s.teachable.com/courses/1",
                 "--email", "a@b.c", "--password", "secret")
    assert args.url == "https://s.teachable.com/courses/1"
    assert args.email == "a@b.c" and args.password == "secret"


@pytest.mark.parametrize(
    "argv",
    [
        ("-v",),
        ("-vv",),
        ("--complete-lecture",),
        ("--login_url", "https://x/sign_in"),
        ("--man_login_url", "https://x/courses"),
        ("-t", "30"),
        ("-f", "urls.txt"),
        ("--user-agent", "UA"),
    ],
)
def test_legacy_flags_are_all_still_accepted(argv):
    parse(*argv)


def test_underscore_and_dash_spellings_both_work():
    assert parse("--login-url", "u").login_url == "u"
    assert parse("--login_url", "u").login_url == "u"


# ------------------------------------------------------------------ new flags

def test_headless_flag_reaches_the_settings():
    """#45: keep the browser from stealing focus."""
    assert settings_for("--headless").headless is True
    assert settings_for().headless is False


def test_page_export_flags_reach_the_settings():
    """#39: save pages as PDF or image."""
    settings = settings_for("--save-pdf", "--save-screenshot")
    assert settings.save_pdf and settings.save_screenshot


def test_download_toggles_default_to_on():
    settings = settings_for()
    assert settings.download_attachments
    assert settings.download_subtitles
    assert settings.offline_rewrite
    assert settings.resume


def test_download_toggles_can_be_turned_off():
    settings = settings_for("--no-attachments", "--no-subtitles",
                            "--no-offline-rewrite", "--no-resume")
    assert not settings.download_attachments
    assert not settings.download_subtitles
    assert not settings.offline_rewrite
    assert not settings.resume


def test_concurrency_default_enables_parallel_fragments():
    """#59: the default must actually be greater than one."""
    assert settings_for().concurrent_fragments > 1


def test_output_directory_is_absolute():
    assert os.path.isabs(settings_for("-o", "out").output_dir)


def test_verbose_only_turns_on_stack_traces_at_two_vs():
    assert settings_for("-v").verbose is False
    assert settings_for("-vv").verbose is True


# ---------------------------------------------------------------- credentials

def test_environment_variables_are_used_when_flags_are_absent(monkeypatch):
    monkeypatch.setenv("TEACHABLE_EMAIL", "env@example.com")
    monkeypatch.setenv("TEACHABLE_PASSWORD", "envpw")
    monkeypatch.setenv("TEACHABLE_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    email, password, secret = resolve_credentials(parse("--url", "u"))
    assert (email, password, secret) == ("env@example.com", "envpw", "JBSWY3DPEHPK3PXP")


def test_explicit_flags_beat_the_environment(monkeypatch):
    monkeypatch.setenv("TEACHABLE_EMAIL", "env@example.com")
    email, _, _ = resolve_credentials(parse("--url", "u", "--email", "flag@example.com",
                                            "--password", "p"))
    assert email == "flag@example.com"


def test_manual_login_does_not_prompt_for_a_password(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _, password, _ = resolve_credentials(parse("--man_login_url", "https://x"))
    assert password is None


# ---------------------------------------------------------------- url loading

def test_urls_are_read_one_per_line(tmp_path):
    path = tmp_path / "urls.txt"
    path.write_text("https://a/courses/1\n\nhttps://b/courses/2\n", encoding="utf-8")
    assert read_urls_from_file(str(path)) == ["https://a/courses/1", "https://b/courses/2"]


def test_comment_lines_are_skipped(tmp_path):
    path = tmp_path / "urls.txt"
    path.write_text("# a note\nhttps://a/courses/1\n", encoding="utf-8")
    assert read_urls_from_file(str(path)) == ["https://a/courses/1"]


def test_a_missing_url_file_is_reported_not_raised(tmp_path):
    assert read_urls_from_file(str(tmp_path / "nope.txt")) == []


# --------------------------------------------------------------- rewrite only

def test_rewrite_only_works_without_a_browser(tmp_path):
    """Lets existing downloads get local navigation and players retroactively."""
    from teachable_dl.offline import write_manifest

    course = tmp_path / "Course"
    (course / "01-Ch").mkdir(parents=True)
    (course / "01-Ch" / "01-Intro.html").write_text(
        '<html><body><iframe data-testid="embed-player-0"></iframe></body></html>',
        encoding="utf-8",
    )
    write_manifest(
        str(course),
        {
            "title": "Course",
            "lectures": [
                {
                    "lecture_id": "1",
                    "title": "Intro",
                    "chapter": "01-Ch",
                    "html": "01-Ch/01-Intro.html",
                    "videos": [{"path": "01-Ch/01-Intro.mp4", "subtitles": []}],
                }
            ],
        },
    )

    assert run_rewrite_only(str(course)) == 0
    assert "<video" in (course / "01-Ch" / "01-Intro.html").read_text(encoding="utf-8")


def test_rewrite_only_reports_a_missing_manifest(tmp_path):
    assert run_rewrite_only(str(tmp_path)) == 1


def test_stealth_defaults_on_and_can_be_disabled():
    """--no-stealth trades Cloudflare handling for not needing Rosetta 2."""
    assert settings_for().stealth is True
    assert settings_for("--no-stealth").stealth is False


def test_a_real_browser_profile_counts_as_authentication():
    """--chrome-profile *is* the credential: the profile already holds the
    session, so demanding an email and password on top of it blocked the one
    route that needs neither."""
    for extra in (["--chrome-profile"], ["--chrome-profile", "/tmp/p"]):
        args = parse("--url", "https://x.teachable.com/courses/enrolled/1", *extra)
        have_session = (
            args.man_login_url or args.cookies_file
            or args.cookies_from_browser or args.chrome_profile
        )
        assert have_session, f"{extra} should satisfy the credential check"


def test_no_authentication_at_all_is_still_refused():
    args = parse("--url", "https://x.teachable.com/courses/enrolled/1")
    have_session = (
        args.man_login_url or args.cookies_file
        or args.cookies_from_browser or args.chrome_profile
    )
    assert not have_session
