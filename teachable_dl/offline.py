"""Turning the saved pages into a course you can actually browse offline.

Two upstream issues live here.

#63 (*"The video does not play back from the ripped HTML page"*)
    The saved lecture HTML still contains the remote ``<iframe>`` player.  Opened
    from disk it has no session, so Hotmart renders its "Your video is
    processing" placeholder and there is nothing to click.  We swap each player
    iframe for a plain ``<video>`` element pointing at the file we just
    downloaded.

#58 (*"Navigation in downloaded html pages should link to the local file
system"*)
    Links between lectures point at absolute site paths such as
    ``/courses/name/lectures/36979557``, which go nowhere from a ``file://``
    page.  We rewrite them to relative paths into the directory tree the
    downloader has already built.

Everything here works on HTML strings and a manifest, so it is unit-testable and
can be re-run over an existing download without touching the network.
"""

import html
import json
import logging
import os
import re
from urllib.parse import quote

logger = logging.getLogger(__name__)

MANIFEST_NAME = "teachable-dl-manifest.json"

#: ``<a href="/courses/foo/lectures/123">`` in either quote style.
_HREF_RE = re.compile(r"""(?P<attr>\bhref\s*=\s*)(?P<quote>["'])(?P<url>[^"']*)(?P=quote)""",
                      re.IGNORECASE)
_LECTURE_PATH_RE = re.compile(r"/lectures/(\d+)")

#: A whole ``<iframe>`` element, self-closing or not. Iframes cannot nest, so a
#: non-greedy match is safe.
_IFRAME_RE = re.compile(r"<iframe\b[^>]*?/>|<iframe\b[^>]*>.*?</iframe>",
                        re.IGNORECASE | re.DOTALL)

#: Markers identifying an iframe as a video player rather than, say, an embedded PDF.
_PLAYER_MARKERS = (
    "embed-player",
    "player.hotmart.com",
    "fast.wistia.net",
    "player.vimeo.com",
    "youtube.com/embed",
    "youtube-nocookie.com/embed",
)

_STYLE_BLOCK = """
<style id="teachable-dl-offline-style">
.teachable-dl-player{margin:1.5rem 0;font-family:system-ui,-apple-system,sans-serif}
.teachable-dl-player video{width:100%;max-width:960px;background:#000;border-radius:6px}
.teachable-dl-player .teachable-dl-files{font-size:.85rem;margin:.5rem 0 0}
.teachable-dl-player .teachable-dl-files a{margin-right:1rem}
.teachable-dl-missing{padding:1rem;border:1px dashed #b7b7b7;border-radius:6px;color:#555;
  background:#fafafa;max-width:960px}
.teachable-dl-nav{display:flex;gap:1rem;flex-wrap:wrap;align-items:center;padding:.75rem 1rem;
  margin:0 0 1rem;background:#f4f4f5;border-bottom:1px solid #ddd;
  font-family:system-ui,-apple-system,sans-serif;font-size:.9rem}
.teachable-dl-nav a{color:#1668dc;text-decoration:none}
.teachable-dl-nav a:hover{text-decoration:underline}
</style>
"""


def _to_url(path):
    """Turn a relative filesystem path into something safe for an href/src."""
    return quote(path.replace(os.sep, "/"))


def relative_url(from_dir, to_path, course_root):
    """Relative URL from the directory of one saved page to another course file."""
    from_abs = os.path.join(course_root, from_dir) if from_dir else course_root
    to_abs = os.path.join(course_root, to_path)
    return _to_url(os.path.relpath(to_abs, start=from_abs))


def rewrite_lecture_links(page_html, from_dir, id_to_path, course_root):
    """Point every ``/lectures/<id>`` link at the matching local file (#58)."""

    def replace(match):
        url = match.group("url")
        found = _LECTURE_PATH_RE.search(url)
        if not found:
            return match.group(0)
        target = id_to_path.get(found.group(1))
        if not target:
            return match.group(0)
        local = relative_url(from_dir, target, course_root)
        return f'{match.group("attr")}{match.group("quote")}{local}{match.group("quote")}'

    return _HREF_RE.sub(replace, page_html)


def is_player_iframe(tag_html):
    lowered = tag_html.lower()
    return any(marker in lowered for marker in _PLAYER_MARKERS)


def build_video_block(video_url, tracks):
    """The ``<video>`` element that replaces a dead remote player (#63)."""
    track_tags = []
    for lang, track_url, is_default in tracks:
        default_attr = " default" if is_default else ""
        track_tags.append(
            f'<track kind="subtitles" src="{html.escape(track_url, quote=True)}" '
            f'srclang="{html.escape(lang, quote=True)}" '
            f'label="{html.escape(lang, quote=True)}"{default_attr}>'
        )

    links = [f'<a href="{html.escape(video_url, quote=True)}">Open video file</a>']
    for lang, track_url, _ in tracks:
        links.append(
            f'<a href="{html.escape(track_url, quote=True)}">Subtitles ({html.escape(lang)})</a>'
        )

    return (
        '<div class="teachable-dl-player">'
        f'<video controls preload="metadata" playsinline '
        f'src="{html.escape(video_url, quote=True)}">'
        + "".join(track_tags)
        + "Your browser cannot play this file; use the link below."
        "</video>"
        f'<p class="teachable-dl-files">{" ".join(links)}</p>'
        "</div>"
    )


def build_missing_block(title=""):
    label = f" for &ldquo;{html.escape(title)}&rdquo;" if title else ""
    return (
        '<div class="teachable-dl-missing">'
        f"No local video was downloaded{label}. "
        "The original embed was removed because it cannot play offline."
        "</div>"
    )


def replace_player_iframes(page_html, video_blocks):
    """Swap each player iframe for the n-th local video block.

    Non-player iframes (embedded PDFs, forms) are left exactly as they were.
    """
    counter = {"index": 0}

    def replace(match):
        tag = match.group(0)
        if not is_player_iframe(tag):
            return tag
        index = counter["index"]
        counter["index"] += 1
        if index < len(video_blocks):
            return video_blocks[index]
        return build_missing_block()

    return _IFRAME_RE.sub(replace, page_html)


def build_nav_bar(entry, previous_entry, next_entry, course_root):
    """A small prev / index / next strip pinned to the top of each lecture page."""
    from_dir = os.path.dirname(entry["html"])
    parts = []

    # A neighbour whose page failed to save has no file to link to.
    if previous_entry and previous_entry.get("html"):
        url = relative_url(from_dir, previous_entry["html"], course_root)
        parts.append(f'<a href="{html.escape(url, quote=True)}">&larr; Previous</a>')

    index_url = relative_url(from_dir, "index.html", course_root)
    parts.append(f'<a href="{html.escape(index_url, quote=True)}">Course index</a>')

    if next_entry and next_entry.get("html"):
        url = relative_url(from_dir, next_entry["html"], course_root)
        parts.append(f'<a href="{html.escape(url, quote=True)}">Next &rarr;</a>')

    parts.append(
        f'<span>{html.escape(entry.get("chapter", ""))} &middot; '
        f'{html.escape(entry.get("title", ""))}</span>'
    )
    return '<nav class="teachable-dl-nav">' + "".join(parts) + "</nav>"


def _inject(page_html, snippet, tag):
    """Insert ``snippet`` right after the opening ``tag``, or prepend if absent."""
    match = re.search(rf"<{tag}\b[^>]*>", page_html, re.IGNORECASE)
    if not match:
        return snippet + page_html
    position = match.end()
    return page_html[:position] + snippet + page_html[position:]


def render_lecture_page(page_html, entry, previous_entry, next_entry,
                        id_to_path, course_root):
    """Apply every offline transform to one saved lecture page."""
    from_dir = os.path.dirname(entry["html"])

    blocks = []
    for video in entry.get("videos", []):
        video_url = relative_url(from_dir, video["path"], course_root)
        tracks = [
            (
                subtitle["lang"],
                relative_url(from_dir, subtitle["path"], course_root),
                position == 0,
            )
            for position, subtitle in enumerate(video.get("subtitles", []))
            # Only WebVTT renders in a <track>; other formats stay as plain links.
            if subtitle["path"].lower().endswith(".vtt")
        ]
        blocks.append(build_video_block(video_url, tracks))

    page_html = replace_player_iframes(page_html, blocks)
    page_html = rewrite_lecture_links(page_html, from_dir, id_to_path, course_root)
    page_html = _inject(page_html, _STYLE_BLOCK, "head")
    page_html = _inject(
        page_html, build_nav_bar(entry, previous_entry, next_entry, course_root), "body"
    )
    return page_html


def render_index(course_title, entries):
    """A generated table of contents at the root of the course folder."""
    rows = []
    current_chapter = None
    for entry in entries:
        if not entry.get("html"):
            # The page was never saved, so there is nothing to link to.
            continue
        chapter = entry.get("chapter", "")
        if chapter != current_chapter:
            current_chapter = chapter
            rows.append(f"<h2>{html.escape(chapter)}</h2><ul>")
        url = _to_url(entry["html"])
        extras = []
        if entry.get("videos"):
            extras.append(f'{len(entry["videos"])} video(s)')
        if entry.get("attachments"):
            extras.append(f'{len(entry["attachments"])} attachment(s)')
        suffix = f' <small>({", ".join(extras)})</small>' if extras else ""
        rows.append(
            f'<li><a href="{html.escape(url, quote=True)}">'
            f'{html.escape(entry.get("title", url))}</a>{suffix}</li>'
        )

    body = "".join(rows)
    if current_chapter is not None:
        body += "</ul>"

    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(course_title)}</title>{_STYLE_BLOCK}"
        "<style>body{font-family:system-ui,-apple-system,sans-serif;max-width:52rem;"
        "margin:2rem auto;padding:0 1rem;line-height:1.5}"
        "h2{margin-top:2rem;font-size:1.1rem;color:#333}"
        "li{margin:.25rem 0}a{color:#1668dc;text-decoration:none}"
        "a:hover{text-decoration:underline}</style></head>"
        f"<body><h1>{html.escape(course_title)}</h1>{body}</body></html>"
    )


def write_manifest(course_root, manifest):
    path = os.path.join(course_root, MANIFEST_NAME)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
    return path


def read_manifest(course_root):
    path = os.path.join(course_root, MANIFEST_NAME)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def apply_offline_rewrite(course_root, manifest):
    """Rewrite every saved page in ``manifest`` and generate the course index."""
    entries = manifest.get("lectures", [])
    id_to_path = {
        entry["lecture_id"]: entry["html"]
        for entry in entries
        if entry.get("lecture_id") and entry.get("html")
    }

    rewritten = 0
    for position, entry in enumerate(entries):
        relative = entry.get("html")
        if not relative:
            continue
        absolute = os.path.join(course_root, relative)
        if not os.path.isfile(absolute):
            logger.debug("Skipping missing page: %s", relative)
            continue

        previous_entry = entries[position - 1] if position > 0 else None
        next_entry = entries[position + 1] if position + 1 < len(entries) else None

        try:
            with open(absolute, encoding="utf-8", errors="replace") as handle:
                page_html = handle.read()
            page_html = render_lecture_page(
                page_html, entry, previous_entry, next_entry, id_to_path, course_root
            )
            with open(absolute, "w", encoding="utf-8") as handle:
                handle.write(page_html)
            rewritten += 1
        except OSError as exc:
            logger.warning("Could not rewrite %s: %s", relative, exc)

    course_html = os.path.join(course_root, "course.html")
    if os.path.isfile(course_html):
        try:
            with open(course_html, encoding="utf-8", errors="replace") as handle:
                page_html = handle.read()
            page_html = rewrite_lecture_links(page_html, "", id_to_path, course_root)
            with open(course_html, "w", encoding="utf-8") as handle:
                handle.write(page_html)
        except OSError as exc:
            logger.warning("Could not rewrite course.html: %s", exc)

    index_path = os.path.join(course_root, "index.html")
    try:
        with open(index_path, "w", encoding="utf-8") as handle:
            handle.write(render_index(manifest.get("title", "Course"), entries))
    except OSError as exc:
        logger.warning("Could not write the course index: %s", exc)

    logger.info("Rewrote %s page(s) for offline use; index at %s", rewritten, index_path)
    return rewritten
