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

import base64
import html
import json
import logging
import os
import re
from urllib.parse import quote

logger = logging.getLogger(__name__)

MANIFEST_NAME = "teachable-dl-manifest.json"

#: ``<a href="/courses/foo/lectures/123">`` in either quote style.
#:
#: The leading lookbehind is load-bearing. A plain ``\bhref`` also matches
#: ``data-href=`` and ``xlink:href=``, so the rewriter used to mutate attributes
#: that the page's own JavaScript and SVG read back.
_HREF_RE = re.compile(
    r"""(?<![\w:-])(?P<attr>href\s*=\s*)(?P<quote>["'])(?P<url>[^"']*)(?P=quote)""",
    re.IGNORECASE,
)
_LECTURE_PATH_RE = re.compile(r"/lectures/(\d+)")

#: Tag attributes, respecting quoted values. Matching ``[^>]*`` instead means a
#: ``>`` or ``/>`` inside an attribute (``title="Lesson a/>b"``) truncates the
#: match and corrupts the document.
_ATTRS = r"""(?:[^>"']|"[^"]*"|'[^']*')*"""

#: A whole ``<iframe>`` element. The first alternative refuses to swallow a
#: following iframe when this one is self-closing.
_IFRAME_RE = re.compile(
    rf"<iframe\b{_ATTRS}>(?:(?!<iframe\b).)*?</iframe>|<iframe\b{_ATTRS}>",
    re.IGNORECASE | re.DOTALL,
)

_SRC_RE = re.compile(rf"""\bsrc\s*=\s*(["'])(?P<src>.*?)\1""", re.IGNORECASE | re.DOTALL)

#: Regions whose contents are not page markup and must never be rewritten.
#:
#: Teachable is a Next.js app: every page carries a ``__NEXT_DATA__`` script
#: holding a large JSON blob that routinely contains escaped ``<iframe>`` markup
#: and ``/lectures/<id>`` URLs. Rewriting inside it corrupted the JSON *and*
#: consumed the video block meant for the real, visible player -- which then
#: rendered as "no local video was downloaded". Comments and ``<noscript>``
#: fallbacks caused the same theft.
_OPAQUE_RE = re.compile(
    r"<!--.*?-->"
    r"|<script\b[^>]*>.*?</script>"
    r"|<style\b[^>]*>.*?</style>"
    r"|<noscript\b[^>]*>.*?</noscript>",
    re.IGNORECASE | re.DOTALL,
)

_PLACEHOLDER_RE = re.compile("\x00TDL(\\d+)\x00")

#: Inlining subtitles above this size would bloat the page; link them instead.
_MAX_INLINE_SUBTITLE_BYTES = 512 * 1024


def _mask_opaque(page_html):
    """Hide scripts, styles, comments and noscript blocks from the rewriters."""
    stored = []

    def keep(match):
        stored.append(match.group(0))
        return f"\x00TDL{len(stored) - 1}\x00"

    return _OPAQUE_RE.sub(keep, page_html), stored


def _unmask_opaque(page_html, stored):
    return _PLACEHOLDER_RE.sub(lambda m: stored[int(m.group(1))], page_html)


def iframe_src(tag_html):
    """The ``src`` of an iframe tag, with HTML entities resolved."""
    match = _SRC_RE.search(tag_html or "")
    return html.unescape(match.group("src").strip()) if match else ""

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

    masked, stored = _mask_opaque(page_html)
    return _unmask_opaque(_HREF_RE.sub(replace, masked), stored)


def is_player_iframe(tag_html):
    lowered = tag_html.lower()
    return any(marker in lowered for marker in _PLAYER_MARKERS)


def subtitle_track_src(course_root, relative_path, url_fallback):
    """Inline a WebVTT track as a data: URI so it loads from ``file://``.

    Chromium treats a ``file://`` document as an opaque origin and refuses to
    load a ``<track>`` from a sibling file unless the browser is started with
    ``--allow-file-access-from-files``. Embedding the cues in the tag itself
    sidesteps the origin check entirely, so captions work in both Chromium and
    Firefox with no flags.
    """
    absolute = os.path.join(course_root, relative_path)
    try:
        size = os.path.getsize(absolute)
        if size > _MAX_INLINE_SUBTITLE_BYTES:
            logger.debug("Subtitle %s is too large to inline (%s bytes)", relative_path, size)
            return url_fallback
        with open(absolute, "rb") as handle:
            payload = handle.read()
    except OSError as exc:
        logger.debug("Could not inline subtitle %s: %s", relative_path, exc)
        return url_fallback

    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:text/vtt;base64,{encoded}"


def build_video_block(video_url, tracks):
    """The ``<video>`` element that replaces a dead remote player (#63).

    Each track is ``(lang, track_src, link_href, is_default)``: ``track_src`` may
    be an inlined ``data:`` URI so captions load from ``file://``, while
    ``link_href`` always points at the real file on disk for downloading.
    """
    track_tags = []
    for lang, track_src, _link, is_default in tracks:
        default_attr = " default" if is_default else ""
        track_tags.append(
            f'<track kind="subtitles" src="{html.escape(track_src, quote=True)}" '
            f'srclang="{html.escape(lang, quote=True)}" '
            f'label="{html.escape(lang, quote=True)}"{default_attr}>'
        )

    links = [f'<a href="{html.escape(video_url, quote=True)}">Open video file</a>']
    for lang, _src, link, _default in tracks:
        links.append(
            f'<a href="{html.escape(link, quote=True)}">Subtitles ({html.escape(lang)})</a>'
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
    """Swap each player iframe for the local video that came from it.

    ``video_blocks`` is a list of ``(embed_url, block_html)`` pairs; bare
    strings are accepted too and treated as having no embed URL.

    Blocks are matched to iframes by embed URL rather than by position. Position
    was wrong in both directions: the manifest lists videos in selector-group
    order while the document has its own order, and a video whose download
    failed is simply absent from the list -- so block *n* routinely landed under
    a different lecture's player, showing the wrong video with no error at all.

    Non-player iframes (embedded PDFs, forms) are left exactly as they were.
    """
    pairs = [
        (item if isinstance(item, tuple) else (None, item)) for item in video_blocks
    ]

    by_embed = {}
    unkeyed = []
    for embed, block in pairs:
        if embed:
            by_embed.setdefault(embed, []).append(block)
        else:
            unkeyed.append(block)

    def take(embed):
        queue = by_embed.get(embed)
        if queue:
            return queue.pop(0)
        # Deliberately NO fallback to another embed's block. Handing this iframe
        # a video keyed to a different embed is precisely the mix-up this
        # function exists to prevent -- it would show the wrong lecture's video
        # under a player, silently. Only blocks with no embed URL at all (older
        # manifests, which did not record one) are placed positionally.
        if unkeyed:
            return unkeyed.pop(0)
        return None

    def replace(match):
        tag = match.group(0)
        if not is_player_iframe(tag):
            return tag
        block = take(iframe_src(tag))
        return block if block is not None else build_missing_block()

    masked, stored = _mask_opaque(page_html)
    return _unmask_opaque(_IFRAME_RE.sub(replace, masked), stored)


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


#: Strips a nav bar and style block we injected on a previous run.
_OLD_NAV_RE = re.compile(r'<nav class="teachable-dl-nav">.*?</nav>', re.IGNORECASE | re.DOTALL)
_OLD_STYLE_RE = re.compile(
    r'<style id="teachable-dl-offline-style">.*?</style>', re.IGNORECASE | re.DOTALL
)


def strip_previous_rewrite(page_html):
    """Remove markup a previous rewrite added, so re-running is idempotent.

    ``--rewrite-only`` and a resumed download both re-run the rewriter over
    pages it has already touched. Without this, every run injected another nav
    strip and another copy of the stylesheet.
    """
    page_html = _OLD_NAV_RE.sub("", page_html)
    return _OLD_STYLE_RE.sub("", page_html)


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

    # Re-running over an already-rewritten page must not stack another nav bar.
    page_html = strip_previous_rewrite(page_html)

    blocks = []
    for video in entry.get("videos", []):
        video_url = relative_url(from_dir, video["path"], course_root)
        tracks = []
        for position, subtitle in enumerate(video.get("subtitles", [])):
            path = subtitle.get("path", "")
            # Only WebVTT renders in a <track>; other formats stay as links.
            if not path.lower().endswith(".vtt"):
                continue
            link = relative_url(from_dir, path, course_root)
            tracks.append(
                (
                    subtitle.get("lang", "und"),
                    subtitle_track_src(course_root, path, link),
                    link,
                    position == 0,
                )
            )
        blocks.append((video.get("embed_url"), build_video_block(video_url, tracks)))

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
        chapter = str(entry.get("chapter") or "")
        if chapter != current_chapter:
            if current_chapter is not None:
                # Every chapter opened a <ul>; only the last one was ever
                # closed, so each chapter's list nested inside the previous.
                rows.append("</ul>")
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
            f'{html.escape(str(entry.get("title") or url))}</a>{suffix}</li>'
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
        except Exception as exc:
            # One bad entry must not cost every other page its rewrite.
            logger.warning("Could not rewrite %s: %s", relative, exc)

    course_html = os.path.join(course_root, "course.html")
    if os.path.isfile(course_html):
        try:
            with open(course_html, encoding="utf-8", errors="replace") as handle:
                page_html = handle.read()
            page_html = rewrite_lecture_links(page_html, "", id_to_path, course_root)
            with open(course_html, "w", encoding="utf-8") as handle:
                handle.write(page_html)
        except Exception as exc:
            logger.warning("Could not rewrite course.html: %s", exc)

    index_path = os.path.join(course_root, "index.html")
    try:
        with open(index_path, "w", encoding="utf-8") as handle:
            handle.write(render_index(manifest.get("title", "Course"), entries))
    except Exception as exc:
        logger.warning("Could not write the course index: %s", exc)

    logger.info("Rewrote %s page(s) for offline use; index at %s", rewritten, index_path)
    return rewritten
