"""Filename sanitizing and small helpers.

Historically ``clean_string`` ran ``data.encode('ascii', 'ignore')`` which threw
away every non-ASCII character, so Cyrillic/Chinese/Japanese titles collapsed to
empty or mangled names (upstream issue #37).  Modern filesystems handle Unicode
fine, so we now keep the characters and only remove what the filesystem itself
cannot represent.
"""

import logging
import os
import re
import unicodedata

logger = logging.getLogger(__name__)

#: Characters that are illegal in a path component on Windows, and ``/`` which is
#: illegal everywhere.  Everything else is fair game.
_ILLEGAL_CHARS = r'<>:"/\\|?*'
_ILLEGAL_RE = re.compile("[" + re.escape(_ILLEGAL_CHARS) + "]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")
_DASH_RUN_RE = re.compile(r"-{2,}")

#: Device names Windows refuses to use as a file name, with or without extension.
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"}
_RESERVED_NAMES |= {f"COM{i}" for i in range(1, 10)}
_RESERVED_NAMES |= {f"LPT{i}" for i in range(1, 10)}

#: Conservative cap: most Linux filesystems allow 255 *bytes* per component,
#: HFS+/APFS and NTFS allow 255 *UTF-16 code units*.  We budget in bytes.
MAX_COMPONENT_BYTES = 200

#: Longest suffix yt-dlp may bolt onto a name while downloading, e.g.
#: ``.mp4.part-Frag0000.part``.  Titles are truncated to leave room for it.
_RESERVED_SUFFIX = ".mp4.part-Frag0000.part"


def clean_string(data, ascii_only=False):
    """Turn an arbitrary lecture/course title into a safe path component.

    Unicode is preserved by default; pass ``ascii_only`` (``--ascii-filenames``)
    to get the old transliterating behaviour for filesystems that need it.
    """
    if data is None:
        return "untitled"

    logger.debug("Cleaning string: %r", data)

    # Normalise so that composed and decomposed forms of the same title produce
    # the same directory name across macOS (NFD) and Linux/Windows (NFC).
    text = unicodedata.normalize("NFC", str(data))

    if ascii_only:
        text = _transliterate(text)

    # Whitespace first: tabs and newlines are control characters too, and
    # stripping them before this point would glue neighbouring words together.
    text = _WHITESPACE_RE.sub("-", text)
    text = _CONTROL_RE.sub("", text)
    text = _ILLEGAL_RE.sub("-", text)
    text = _DASH_RUN_RE.sub("-", text)

    # Windows silently strips trailing dots and spaces, which turns "foo." into
    # "foo" and breaks our "does this file already exist?" checks.
    text = text.strip("-. \t")

    if not text:
        return "untitled"

    if text.split(".")[0].upper() in _RESERVED_NAMES:
        text = "_" + text

    return text


def _transliterate(text):
    """Best-effort ASCII fold, keeping something readable rather than nothing."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = decomposed.encode("ascii", "ignore").decode("ascii")
    if stripped.strip(" -_"):
        return stripped
    # Nothing survived the fold (e.g. a purely CJK title). Keep the original so
    # the caller still gets a distinguishable name instead of "untitled".
    return text


def truncate_to_byte_budget(title, budget=MAX_COMPONENT_BYTES, encoding="utf-8"):
    """Shorten ``title`` so it fits in ``budget`` bytes without splitting a character."""
    encoded = title.encode(encoding)
    if len(encoded) <= budget:
        return title
    truncated = encoded[:budget].decode(encoding, "ignore").rstrip("-. ")
    logger.warning("Truncating title: %s", truncated)
    return truncated or "untitled"


def truncate_title_to_fit_file_name(title, max_file_name_length=MAX_COMPONENT_BYTES):
    """Leave room for the index prefix and the longest extension yt-dlp appends."""
    budget = max_file_name_length - len(_RESERVED_SUFFIX) - len("00-")
    return truncate_to_byte_budget(title, max(budget, 16))


def lecture_basename(index, title):
    """The ``01-Some-Lecture`` stem shared by a lecture's video, subs and html."""
    return "{:02d}-{}".format(index, title)


def create_folder(course_title, output_dir=None):
    root = output_dir or os.path.join(os.path.abspath(os.getcwd()), "courses")
    course_path = os.path.join(root, course_title)
    os.makedirs(course_path, exist_ok=True)
    return course_path


def parse_bool_env(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
