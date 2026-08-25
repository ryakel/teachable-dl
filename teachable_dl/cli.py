"""Command line entry point."""

import argparse
import getpass
import logging
import os
import sys

from . import __version__, offline
from .config import DEFAULT_USER_AGENT, Settings

logger = logging.getLogger(__name__)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="teachable-dl",
        description="Download courses you are enrolled in from Teachable.",
    )
    parser.add_argument("--version", action="version", version=f"teachable-dl {__version__}")

    source = parser.add_argument_group("course selection")
    source.add_argument("--url", help="URL of the course to download")
    source.add_argument("-f", "--file", help="Path to a text file with one course URL per line")

    login = parser.add_argument_group("authentication")
    login.add_argument("-e", "--email", help="Account email (or set TEACHABLE_EMAIL)")
    login.add_argument(
        "-p",
        "--password",
        help="Account password. Prefer TEACHABLE_PASSWORD, or omit to be prompted, "
        "so the password does not end up in your shell history.",
    )
    login.add_argument(
        "--totp-secret",
        help="Base32 TOTP secret for accounts with two-factor authentication "
        "(or set TEACHABLE_TOTP_SECRET). Without it you will be prompted for the code.",
    )
    login.add_argument("--login_url", "--login-url", dest="login_url",
                       help="Direct URL of the sign-in page, if it cannot be found automatically")
    login.add_argument("--man_login_url", "--man-login-url", dest="man_login_url",
                       help="Log in by hand; downloading starts once this URL is reached")

    output = parser.add_argument_group("output")
    output.add_argument("-o", "--output-dir", default=os.path.join(os.getcwd(), "courses"),
                        help="Where courses are written (default: ./courses)")
    output.add_argument("--ascii-filenames", action="store_true",
                        help="Transliterate non-ASCII titles instead of keeping them as-is")
    output.add_argument("--save-pdf", action="store_true",
                        help="Also save each lecture page as a PDF")
    output.add_argument("--save-screenshot", action="store_true",
                        help="Also save a screenshot of each lecture page")
    output.add_argument("--no-attachments", action="store_true",
                        help="Do not download lecture attachments")
    output.add_argument("--no-subtitles", action="store_true",
                        help="Do not download subtitles")
    output.add_argument("--no-offline-rewrite", action="store_true",
                        help="Keep the saved HTML exactly as served, without local "
                             "navigation or an embedded local video player")
    output.add_argument("--rewrite-only", metavar="COURSE_DIR",
                        help="Re-run the offline HTML rewrite over an already downloaded "
                             "course directory and exit. Does not open a browser.")

    browser = parser.add_argument_group("browser")
    browser.add_argument("--no-stealth", action="store_true",
                         help="Use a plain Chrome driver instead of SeleniumBase's "
                              "undetected (UC) mode. UC mode is what gets past a "
                              "Cloudflare challenge, but it runs the x86_64 "
                              "chromedriver on macOS, so Apple Silicon needs "
                              "Rosetta 2. If your school has no Cloudflare "
                              "interstitial, this avoids that requirement.")
    browser.add_argument("--headless", action="store_true",
                         help="Run the browser headless so it stops stealing focus")
    browser.add_argument("--user-agent", default=DEFAULT_USER_AGENT,
                         help="User agent for the browser and media requests")
    browser.add_argument("-t", "--timeout", type=int, default=10,
                         help="Seconds to wait for page elements (default: 10)")
    browser.add_argument("--max-session-restarts", type=int, default=3,
                         help="How many times to restart the browser if it crashes "
                              "(default: 3)")

    download = parser.add_argument_group("downloading")
    download.add_argument("--concurrent-fragments", type=int, default=16,
                          help="Parallel HLS fragment downloads (default: 16). "
                               "Lower this if the server rate-limits you.")
    download.add_argument("--retries", type=int, default=10,
                          help="yt-dlp retries per download (default: 10)")
    download.add_argument("--fragment-retries", type=int, default=25,
                          help="yt-dlp retries per fragment (default: 25)")
    download.add_argument("--link-refresh-attempts", type=int, default=3,
                          help="How many times to re-extract an expired stream URL and "
                               "resume a long download (default: 3)")
    download.add_argument("--no-resume", action="store_true",
                          help="Re-download files that already exist")
    download.add_argument("--max-file-size", type=float, default=8.0, metavar="GIB",
                          help="Refuse any single attachment larger than this many "
                               "GiB (default: 8)")
    download.add_argument("--allow-private-hosts", action="store_true",
                          help="Allow downloads from private, loopback and link-local "
                               "addresses. Off by default: course pages are remote "
                               "input, and following them to an internal address is a "
                               "server-side request forgery risk. Only enable this for "
                               "a school you host yourself.")

    misc = parser.add_argument_group("misc")
    misc.add_argument("--dry-run", action="store_true",
                      help="Log in and print the curriculum this would download, "
                           "then stop. Writes nothing. Use this first: it exercises "
                           "login, Cloudflare and template detection in seconds "
                           "without downloading anything.")
    misc.add_argument("--limit", type=int, metavar="N",
                      help="Download at most N lectures per course. Use --limit 1 to "
                           "smoke-test the full pipeline on a single lecture.")
    misc.add_argument("--complete-lecture", action="store_true",
                      help="Mark each lecture complete after downloading it")
    misc.add_argument("-v", "--verbose", action="count", default=0,
                      help="Increase verbosity (repeat for debug output)")

    return parser


def configure_logging(verbosity):
    # The old default was WARNING, which hid all download progress. INFO is a
    # better default for a tool whose whole job is a long-running download.
    level = logging.DEBUG if verbosity >= 2 else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")
    # yt-dlp and selenium are chatty at debug level; keep them at warning unless -vv.
    if verbosity < 2:
        logging.getLogger("selenium").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    return level


def read_urls_from_file(path):
    try:
        with open(path, encoding="utf-8") as handle:
            urls = [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    except OSError as exc:
        logger.error("Could not read %s: %s", path, exc)
        return []

    if urls:
        logger.info("Read %s URL(s) from %s", len(urls), path)
    else:
        logger.warning("No URLs found in %s", path)
    return urls


def resolve_credentials(args):
    """Take credentials from the flags, then the environment, then a prompt."""
    email = args.email or os.environ.get("TEACHABLE_EMAIL")
    password = args.password or os.environ.get("TEACHABLE_PASSWORD")
    totp_secret = args.totp_secret or os.environ.get("TEACHABLE_TOTP_SECRET")

    if args.man_login_url:
        return email, password, totp_secret

    if email and not password and sys.stdin.isatty():
        password = getpass.getpass(f"Password for {email}: ")

    return email, password, totp_secret


def settings_from_args(args, email, password, totp_secret):
    return Settings(
        email=email,
        password=password,
        totp_secret=totp_secret,
        login_url=args.login_url,
        man_login_url=args.man_login_url,
        output_dir=os.path.abspath(args.output_dir),
        ascii_filenames=args.ascii_filenames,
        stealth=not args.no_stealth,
        headless=args.headless,
        user_agent=args.user_agent,
        timeout=args.timeout,
        max_session_restarts=args.max_session_restarts,
        concurrent_fragments=args.concurrent_fragments,
        retries=args.retries,
        fragment_retries=args.fragment_retries,
        link_refresh_attempts=args.link_refresh_attempts,
        resume=not args.no_resume,
        allow_private_hosts=args.allow_private_hosts,
        max_file_bytes=int(args.max_file_size * 1024 * 1024 * 1024),
        dry_run=args.dry_run,
        limit=args.limit,
        complete_lecture=args.complete_lecture,
        save_pdf=args.save_pdf,
        save_screenshot=args.save_screenshot,
        download_attachments=not args.no_attachments,
        download_subtitles=not args.no_subtitles,
        offline_rewrite=not args.no_offline_rewrite,
        verbose=args.verbose >= 2,
    )


def run_rewrite_only(course_dir):
    """Regenerate local navigation and players for an existing download."""
    course_dir = os.path.abspath(course_dir)
    manifest = offline.read_manifest(course_dir)
    if manifest is None:
        logger.error(
            "No %s in %s. Only courses downloaded with this version have one; "
            "re-download the course to generate it.",
            offline.MANIFEST_NAME,
            course_dir,
        )
        return 1
    offline.apply_offline_rewrite(course_dir, manifest)
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)

    if args.rewrite_only:
        return run_rewrite_only(args.rewrite_only)

    if args.file:
        urls = read_urls_from_file(args.file)
    elif args.url:
        urls = [args.url]
    else:
        logger.error("Nothing to download: pass --url or --file")
        return 1

    if not urls:
        return 1

    email, password, totp_secret = resolve_credentials(args)
    if not args.man_login_url and not (email and password):
        logger.error(
            "Credentials are missing. Pass --email and --password (or set "
            "TEACHABLE_EMAIL / TEACHABLE_PASSWORD), or use --man_login_url to log in by hand."
        )
        return 1

    settings = settings_from_args(args, email, password, totp_secret)

    # Imported here so that --help and --rewrite-only work without a browser stack.
    from .browser import BrowserNotFoundError, MissingRosettaError
    from .downloader import CourseDownloader

    downloader = None
    try:
        downloader = CourseDownloader(settings)
        return downloader.run(urls)
    except KeyboardInterrupt:
        logger.error("Interrupted")
        return 130
    except (BrowserNotFoundError, MissingRosettaError) as exc:
        # A missing browser is a setup problem, not a crash: no stack trace.
        logger.error("%s", exc)
        return 1
    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=settings.verbose)
        return 1
    finally:
        if downloader is not None:
            downloader.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
