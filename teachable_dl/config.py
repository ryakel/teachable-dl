"""Runtime configuration shared by every component of the downloader."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

#: Kept only as a documented fallback; not used unless the browser refuses to
#: report its own user agent.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class Settings:
    """Everything the downloader needs to know, resolved from the CLI."""

    # Credentials / entry points
    email: str | None = None
    password: str | None = None
    totp_secret: str | None = None
    login_url: str | None = None
    man_login_url: str | None = None
    #: How long to wait for a person to finish signing in by hand.
    manual_login_timeout: float = 600.0
    #: Drive your real Chrome profile rather than the throwaway one that
    #: undetected mode creates. A fresh profile has no cookies and no login,
    #: which is why an automated browser lands on a school's public pages while
    #: your own browser is signed in.
    user_data_dir: str | None = None
    profile_directory: str | None = None
    #: Drive the real profile in place rather than a copy. Requires Chrome to
    #: be fully quit and touches the person's actual browser data.
    use_live_profile: bool = False

    #: Reuse an existing browser session instead of logging in at all.
    cookies_file: str | None = None
    cookies_from_browser: str | None = None

    # Output
    output_dir: str = field(default_factory=lambda: os.path.join(os.getcwd(), "courses"))
    ascii_filenames: bool = False

    # Browser
    #: SeleniumBase's undetected ("UC") mode evades bot detection and is what
    #: gets past a Cloudflare interstitial. It also runs the x86_64 chromedriver
    #: on macOS, which is why Apple Silicon needs Rosetta 2. Turn it off to use
    #: a plain driver where the school has no such challenge.
    stealth: bool = True
    #: Seconds to leave chromedriver detached while a page loads in UC mode.
    #: Longer gives Cloudflare's check more time to settle before we reattach.
    uc_reconnect_time: float = 4.0
    headless: bool = False
    #: ``None`` means "whatever this Chrome really is". Forcing a fixed string
    #: is actively harmful: a current Chrome announcing an old version is a bot
    #: signal in itself, and Cloudflare binds its clearance cookie to the exact
    #: user agent that earned it, so an override invalidates a session imported
    #: from a real browser. Set one only when you have a specific reason.
    user_agent: str | None = None
    timeout: int = 10
    max_session_restarts: int = 3

    # Safety limits on remote-controlled downloads
    allow_private_hosts: bool = False
    max_file_bytes: int = 8 * 1024 * 1024 * 1024  # 8 GiB

    # Downloading
    concurrent_fragments: int = 16
    retries: int = 10
    fragment_retries: int = 25
    link_refresh_attempts: int = 3
    resume: bool = True

    # Testing / triage
    dry_run: bool = False
    limit: int | None = None

    # Extras
    complete_lecture: bool = False
    save_pdf: bool = False
    save_screenshot: bool = False
    download_attachments: bool = True
    download_subtitles: bool = True
    offline_rewrite: bool = True

    verbose: bool = False

    def course_root(self, course_title: str) -> str:
        return os.path.join(self.output_dir, course_title)
