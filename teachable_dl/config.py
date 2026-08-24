"""Runtime configuration shared by every component of the downloader."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

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

    # Output
    output_dir: str = field(default_factory=lambda: os.path.join(os.getcwd(), "courses"))
    ascii_filenames: bool = False

    # Browser
    headless: bool = False
    user_agent: str = DEFAULT_USER_AGENT
    timeout: int = 10
    max_session_restarts: int = 3

    # Downloading
    concurrent_fragments: int = 16
    retries: int = 10
    fragment_retries: int = 25
    link_refresh_attempts: int = 3
    resume: bool = True

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
