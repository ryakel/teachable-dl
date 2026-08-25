"""Reuse a browser session instead of logging in.

Automated login cannot work for every school. Teachable's own single sign-on
adds a password page on another host, some schools sign people in with emailed
one-time codes, and any of it can sit behind a bot check. All of that is a lot
of machinery to reproduce badly.

Signing in once in a normal browser and handing the resulting cookies over
sidesteps the whole problem: no credentials are typed anywhere, no challenge is
triggered, and the session is exactly the one the school already trusts.

Treat an exported cookie file the way you would treat the password -- it *is* a
live session for as long as it lasts.
"""

import logging
import os
import sys
from http.cookiejar import Cookie
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class CookieError(RuntimeError):
    """The requested cookies could not be loaded."""


def cookie_matches_host(cookie_domain, host):
    """Does a cookie's domain cover ``host``?

    ``.teachable.com`` covers ``flightinsight.teachable.com``; the school's own
    host covers only itself.
    """
    if not cookie_domain or not host:
        return False
    domain = cookie_domain.lower().lstrip(".")
    host = host.lower()
    return host == domain or host.endswith("." + domain)


def load_from_file(path):
    """Load a Netscape/Mozilla ``cookies.txt``, as browser extensions export."""
    from yt_dlp.cookies import YoutubeDLCookieJar

    if not os.path.isfile(path):
        raise CookieError(f"No cookie file at {path!r}")

    jar = YoutubeDLCookieJar(path)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        raise CookieError(
            f"Could not read {path!r} as a Netscape cookie file: {exc}"
        ) from exc
    return jar


def load_from_browser(spec):
    """Read cookies straight out of an installed browser.

    ``spec`` is ``browser`` or ``browser:profile``, matching yt-dlp's own
    ``--cookies-from-browser`` syntax.
    """
    from yt_dlp.cookies import SUPPORTED_BROWSERS, extract_cookies_from_browser

    name, _, profile = spec.partition(":")
    name = name.strip().lower()
    if name not in SUPPORTED_BROWSERS:
        raise CookieError(
            f"Unknown browser {name!r}. Supported: {', '.join(sorted(SUPPORTED_BROWSERS))}"
        )

    try:
        return extract_cookies_from_browser(name, profile.strip() or None)
    except Exception as exc:
        raise CookieError(_explain_browser_failure(name, exc)) from exc


_EXPORT_ADVICE = [
    "  Alternatively, export a cookies.txt with a browser extension (search",
    "  your extension store for \"Get cookies.txt\") while signed in to the",
    "  school, and pass --cookies path/to/cookies.txt. That needs no special",
    "  permission and works with any browser.",
]


def _explain_browser_failure(name, exc):
    """Say what actually went wrong, rather than guessing one cause for all."""
    message = str(exc)
    lowered = message.lower()
    lines = [f"Could not read cookies from {name}: {message}", ""]

    denied = isinstance(exc, PermissionError) or "not permitted" in lowered or (
        "permission denied" in lowered
    )
    missing = "could not find" in lowered or "no such file" in lowered

    if denied:
        # The browser is there; macOS is refusing us the file.
        lines += [
            "  That is a macOS privacy restriction, not a missing browser: the",
            "  cookie store exists but your terminal is not allowed to read it.",
            "",
            "  Open System Settings > Privacy & Security > Full Disk Access,",
            "  add the terminal app you are running this from, then restart the",
            "  terminal completely and try again.",
            "",
        ]
    elif missing:
        lines += [
            "  That browser has no cookie store on this machine, which usually",
            "  means it has never been signed in to -- a browser installed just",
            "  to run this tool has stored nothing. Use the one you actually",
            "  logged in with.",
            f"  Browsers with a cookie store here: {describe_available_browsers()}",
            "",
            "  A non-default profile is given as browser:profile, for example",
            "  --cookies-from-browser 'chrome:Profile 1'.",
            "",
        ]
    else:
        lines += [
            f"  Browsers with a cookie store here: {describe_available_browsers()}",
            "  Reading an encrypted store may prompt for Keychain access and",
            "  usually needs the browser closed.",
            "",
        ]

    return "\n".join(lines + _EXPORT_ADVICE)


#: Where each browser keeps its profiles on this platform, for a better error.
def _browser_data_dirs():
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        support = os.path.join(home, "Library", "Application Support")
        return {
            "chrome": os.path.join(support, "Google", "Chrome"),
            "chromium": os.path.join(support, "Chromium"),
            "brave": os.path.join(support, "BraveSoftware", "Brave-Browser"),
            "edge": os.path.join(support, "Microsoft Edge"),
            "vivaldi": os.path.join(support, "Vivaldi"),
            "opera": os.path.join(support, "com.operasoftware.Opera"),
            "firefox": os.path.join(support, "Firefox"),
            "safari": os.path.join(home, "Library", "Cookies"),
        }
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA", "")
        roaming = os.environ.get("APPDATA", "")
        return {
            "chrome": os.path.join(local, "Google", "Chrome", "User Data"),
            "chromium": os.path.join(local, "Chromium", "User Data"),
            "brave": os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data"),
            "edge": os.path.join(local, "Microsoft", "Edge", "User Data"),
            "firefox": os.path.join(roaming, "Mozilla", "Firefox"),
        }
    config = os.path.join(home, ".config")
    return {
        "chrome": os.path.join(config, "google-chrome"),
        "chromium": os.path.join(config, "chromium"),
        "brave": os.path.join(config, "BraveSoftware", "Brave-Browser"),
        "firefox": os.path.join(home, ".mozilla", "firefox"),
    }


def _has_cookie_store(name, root):
    """Is there an actual cookie database under ``root``?

    A directory alone is not enough: a freshly installed browser has one and no
    cookies, which is precisely the case that produced a misleading error.
    """
    if not os.path.isdir(root):
        return False
    if name == "safari":
        return True  # the container itself is the store
    candidates = ("Cookies", "cookies.sqlite", "Cookies.binarycookies")
    for current, _dirs, files in os.walk(root):
        if any(candidate in files for candidate in candidates):
            return True
        # Profiles live one or two levels down; do not walk the whole tree.
        if current.count(os.sep) - root.count(os.sep) >= 2:
            _dirs[:] = []
    return False


def available_browsers():
    """Browsers that actually have a cookie store on this machine."""
    return sorted(
        name for name, path in _browser_data_dirs().items() if _has_cookie_store(name, path)
    )


def describe_available_browsers():
    found = available_browsers()
    return ", ".join(found) if found else "none found"


def load_cookie_jar(settings):
    """Whichever cookie source was configured, or ``None`` if neither was."""
    if settings.cookies_file:
        logger.info("Loading cookies from %s", settings.cookies_file)
        return load_from_file(settings.cookies_file)
    if settings.cookies_from_browser:
        logger.info("Reading cookies from %s", settings.cookies_from_browser)
        return load_from_browser(settings.cookies_from_browser)
    return None


def to_selenium_cookie(cookie):
    """Convert a ``http.cookiejar`` cookie into what ``add_cookie`` expects."""
    payload = {
        "name": cookie.name,
        "value": cookie.value or "",
        "path": cookie.path or "/",
        "domain": cookie.domain,
        "secure": bool(cookie.secure),
    }
    if cookie.expires:
        payload["expiry"] = int(cookie.expires)
    return payload


def cookies_for_host(jar, host):
    """Every cookie in ``jar`` that applies to ``host``, oldest scope first."""
    return [c for c in jar if cookie_matches_host(c.domain, host)]


def apply_to_browser(browser, jar, course_url):
    """Install the cookies into the live browser session.

    A cookie can only be set while the browser is on a matching domain, so the
    school's origin is loaded first and the course page only afterwards.
    """
    parsed = urlparse(course_url)
    host = parsed.hostname
    if not host:
        raise CookieError(f"Could not read a host from {course_url!r}")

    applicable = cookies_for_host(jar, host)
    if not applicable:
        raise CookieError(
            f"The cookies contain nothing for {host}. Make sure you exported "
            "them while signed in to that school, not just to teachable.com."
        )

    origin = f"{parsed.scheme or 'https'}://{host}/"
    logger.info("Seeding %s cookie(s) for %s", len(applicable), host)
    browser.get(origin)

    added = 0
    for cookie in applicable:
        try:
            browser.driver.add_cookie(to_selenium_cookie(cookie))
            added += 1
        except Exception as exc:
            logger.debug("Could not set cookie %s: %s", cookie.name, exc)

    if not added:
        raise CookieError("The browser rejected every cookie")

    logger.info("Installed %s cookie(s); reloading as the signed-in user", added)
    browser.get(course_url)
    return added
