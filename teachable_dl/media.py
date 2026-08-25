"""Video and subtitle downloading via yt-dlp.

Four upstream issues are addressed here.

#59 / #51 (*"Download Speed is very slow"*)
    The old options dict passed ``"concurrentfragments": 15``.  That is not a
    yt-dlp option -- the real name is ``concurrent_fragment_downloads`` -- and
    yt-dlp silently ignores unknown keys.  Every HLS stream was therefore
    fetched one fragment at a time, which is exactly the 100-500 KB/s the
    reporters saw on a 200 Mb/s line.

#41 (*"Downloaded mp4 video will not play"*)
    ``FFmpegVideoConvertor`` was applied unconditionally while the format
    selector asked for separate video+audio streams.  Without ffmpeg on PATH
    neither the merge nor the conversion happens, and yt-dlp leaves behind a
    video-only (or fragment) file that still carries an ``.mp4`` name.  We now
    detect ffmpeg, pick a single-file format when it is missing, and validate
    the result.

#44 (*403 Forbidden on some videos*) and #55 (*videos over ~100 minutes fail*)
    Hotmart hands out time-limited signed playlist URLs, and the request headers
    were hardcoded to ``player.hotmart.com`` regardless of the actual embed
    host.  A three-hour video simply outlives its token.  Downloads are now
    retried against a freshly extracted link, resuming from the fragments
    already on disk.
"""

import logging
import os
import shutil
from urllib.parse import urljoin, urlparse

import requests
import yt_dlp

from .netutil import TEXT_MAX_BYTES, UnsafeUrlError, read_capped, safe_get
from .utils import iter_directory, split_stem_suffix

logger = logging.getLogger(__name__)

_FFMPEG_CHECKED = None


def ffmpeg_available():
    """Is ffmpeg on PATH? Cached, because we ask once per lecture."""
    global _FFMPEG_CHECKED
    if _FFMPEG_CHECKED is None:
        _FFMPEG_CHECKED = shutil.which("ffmpeg") is not None
        if not _FFMPEG_CHECKED:
            logger.warning(
                "ffmpeg was not found on PATH. Falling back to single-file formats: "
                "quality may be lower and some streams cannot be downloaded at all. "
                "Install ffmpeg for the best results."
            )
    return _FFMPEG_CHECKED


def headers_for_embed(embed_url, user_agent):
    """Build Origin/Referer from the embed that actually served the stream.

    Hardcoding ``player.hotmart.com`` makes every non-Hotmart embed 403 (#44).
    """
    headers = {"User-Agent": user_agent}
    if embed_url:
        parsed = urlparse(embed_url)
        if parsed.scheme and parsed.netloc:
            origin = f"{parsed.scheme}://{parsed.netloc}"
            headers["Origin"] = origin
            headers["Referer"] = origin + "/"
    return headers


def build_ydl_opts(settings, output_template, headers, want_subtitles=False):
    """Assemble yt-dlp options. Kept pure so the option names stay under test."""
    has_ffmpeg = ffmpeg_available()

    if has_ffmpeg:
        # Let yt-dlp merge the best video and audio it can find; the container
        # is decided by merge_output_format, so no extra conversion pass.
        video_format = "bestvideo*+bestaudio/best"
    else:
        # No merger available, so only ever ask for a stream that is already
        # muxed. Anything else produces the unplayable file reported in #41.
        video_format = "best[ext=mp4]/best"

    opts = {
        "format": video_format,
        "http_headers": dict(headers),
        "outtmpl": output_template,
        "verbose": settings.verbose,
        "quiet": not settings.verbose,
        "noprogress": False,
        "ignoreerrors": False,
        # -- #59: the actual, correctly spelled parallelism knob.
        "concurrent_fragment_downloads": max(1, int(settings.concurrent_fragments)),
        # -- #55: survive a flaky or expiring CDN for the length of a long video.
        "retries": settings.retries,
        "fragment_retries": settings.fragment_retries,
        "file_access_retries": 5,
        "extractor_retries": 3,
        "retry_sleep_functions": {"http": lambda n: min(2 ** n, 30)},
        "skip_unavailable_fragments": False,
        "continuedl": settings.resume,
        "keepvideo": False,
        "overwrites": False,
    }

    if has_ffmpeg:
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [{"key": "FFmpegMetadata"}]

    if want_subtitles:
        opts.update(
            {
                "writesubtitles": True,
                # "allsubtitles" is yt-dlp's hidden --all-subs alias; the
                # supported spelling is subtitleslangs.
                "subtitleslangs": ["all"],
                "skip_download": True,
            }
        )

    return opts


def escape_output_template(basename):
    """Make a lecture title safe to embed in a yt-dlp output template.

    yt-dlp treats ``%`` as the start of a field. A lecture called
    "String %(title)s formatting" would otherwise have the field substituted --
    writing the file under a name we never look for -- and an incomplete key
    like "%(x" raises ``ValueError: incomplete format key`` and fails the
    download outright.
    """
    return basename.replace("%", "%%")


def _looks_complete(path):
    """A finished download exists, is non-empty, and left no partial siblings."""
    if not os.path.isfile(path):
        return False
    if os.path.getsize(path) == 0:
        return False
    if os.path.exists(path + ".part") or os.path.exists(path + ".ytdl"):
        return False
    return True


#: Sidecars that share a lecture's stem but are not the video itself.
_NON_VIDEO_SUFFIXES = (
    ".vtt", ".srt", ".ass", ".ssa", ".json", ".html", ".pdf", ".png", ".jpg",
    ".jpeg", ".webp", ".txt", ".part", ".ytdl", ".temp",
)
#: Preferred order when more than one candidate exists.
_VIDEO_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov", ".ts", ".flv", ".avi", ".m4v")


def find_existing_video(output_path, basename):
    """Return an already-downloaded video for this lecture, if there is one.

    A fixed extension list missed formats yt-dlp legitimately produces (``.ts``
    and ``.flv`` turn up in the no-ffmpeg fallback path), so a finished download
    looked absent and was fetched again. Globbing the stem finds whatever
    actually landed, while the sidecar list keeps a subtitle or a saved page
    from being mistaken for the video.
    """
    for extension in _VIDEO_EXTENSIONS:
        candidate = os.path.join(output_path, basename + extension)
        if _looks_complete(candidate):
            return candidate

    # Scan the directory rather than globbing: a glob pattern is matched against
    # os.listdir output as plain strings, so on a filesystem that stores names
    # decomposed (macOS HFS+) an accented title never matches.
    for normalized, real_name in iter_directory(output_path):
        remainder = split_stem_suffix(normalized, basename)
        if not remainder or not remainder.startswith("."):
            continue
        # "01-Intro.en.vtt" shares the stem but is a sidecar, not a video.
        if remainder.count(".") > 1:
            continue
        if normalized.lower().endswith(_NON_VIDEO_SUFFIXES):
            continue
        candidate = os.path.join(output_path, real_name)
        if _looks_complete(candidate):
            return candidate
    return None


class MediaDownloader:
    def __init__(self, settings):
        self.settings = settings

    # ----------------------------------------------------------------- video

    def download_video(self, link, basename, output_path, embed_url=None, refresh_link=None):
        """Download one lecture video, refreshing the signed URL if it expires.

        ``refresh_link`` is a zero-argument callable that re-extracts the stream
        URL from the still-open lecture page.  Signed Hotmart URLs are only valid
        for a while, so a three-hour download (#55) or a slow connection (#44)
        will outlive the token it started with.
        """
        if self.settings.resume:
            existing = find_existing_video(output_path, basename)
            if existing:
                logger.info("Skipping already downloaded video: %s", os.path.basename(existing))
                return existing

        os.makedirs(output_path, exist_ok=True)
        template = os.path.join(
            output_path, escape_output_template(basename) + ".%(ext)s"
        )
        attempts = max(1, self.settings.link_refresh_attempts)
        current_link = link
        last_error = None

        for attempt in range(1, attempts + 1):
            headers = headers_for_embed(embed_url, self.settings.user_agent)
            opts = build_ydl_opts(self.settings, template, headers)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([current_link])
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Video download failed (attempt %s/%s) for %s: %s",
                    attempt,
                    attempts,
                    basename,
                    exc,
                )
                if attempt < attempts and refresh_link is not None:
                    # The most common cause is an expired signed URL, so ask the
                    # page for a new one and resume from the fragments on disk.
                    logger.info("Refreshing the stream URL and resuming")
                    try:
                        refreshed = refresh_link()
                    except Exception as refresh_exc:
                        logger.warning("Could not refresh the stream URL: %s", refresh_exc)
                        refreshed = None
                    if refreshed:
                        current_link = refreshed
                    continue
                if attempt < attempts:
                    continue
                break

            downloaded = find_existing_video(output_path, basename)
            if downloaded:
                logger.info("Downloaded video: %s", os.path.basename(downloaded))
                return downloaded

            last_error = RuntimeError(
                "yt-dlp reported success but produced no usable file "
                "(a leftover .part usually means ffmpeg could not merge the streams)"
            )
            logger.warning("%s", last_error)

        logger.error("Could not download video %s: %s", basename, last_error)
        return None

    # ------------------------------------------------------------- subtitles

    def download_subtitles(self, link, basename, output_path, embed_url=None):
        """Fetch every subtitle track for a lecture.

        yt-dlp cannot write these itself for Hotmart, so we resolve the playlist
        by hand. The previous implementation used ``info_json`` even when
        ``extract_info`` had raised (a guaranteed ``NameError``) and pulled the
        media URL from a fixed line number of the m3u8.
        """
        headers = headers_for_embed(embed_url, self.settings.user_agent)
        opts = build_ydl_opts(self.settings, os.path.join(output_path, basename), headers,
                              want_subtitles=True)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.sanitize_info(ydl.extract_info(link, download=False))
        except Exception as exc:
            logger.warning("Could not list subtitles for %s: %s", basename, exc)
            return []

        requested = (info or {}).get("requested_subtitles") or {}
        if not requested:
            logger.debug("No subtitles offered for %s", basename)
            return []

        written = []
        for lang, sub_info in requested.items():
            url = sub_info.get("url")
            extension = sub_info.get("ext", "vtt")
            if not url:
                continue

            filename = f"{basename}.{lang}.{extension}"
            file_path = os.path.join(output_path, filename)
            # Every other skip check consults settings.resume; this one did not,
            # so --no-resume re-fetched the video but silently kept a stale
            # subtitle sitting next to it.
            if (
                self.settings.resume
                and os.path.isfile(file_path)
                and os.path.getsize(file_path) > 0
            ):
                logger.info("Skipping existing subtitle: %s", filename)
                written.append(file_path)
                continue

            content = self._fetch_subtitle(url, headers)
            if content is None:
                continue
            try:
                with open(file_path, "wb") as handle:
                    handle.write(content)
            except OSError as exc:
                logger.warning("Could not write subtitle %s: %s", filename, exc)
                continue
            logger.info("Downloaded subtitle: %s", filename)
            written.append(file_path)

        return written

    def _fetch_subtitle(self, url, headers, _depth=0):
        """Resolve a subtitle URL, following and joining an m3u8 playlist.

        Two bugs lived here. The old code took the *first* non-comment line of
        the playlist and saved that single segment as the whole track, so a
        three-hour lecture got subtitles covering only its opening minutes. And
        the segment URL came straight from a school-controlled playlist into
        ``requests.get`` with no validation, which is an SSRF primitive.
        """
        if _depth > 2:
            logger.warning("Subtitle playlist nests too deeply, giving up")
            return None

        session = requests.Session()
        try:
            response = safe_get(
                session,
                url,
                allow_private=self.settings.allow_private_hosts,
                stream=True,
                timeout=30,
                headers=headers,
            )
            response.raise_for_status()
            body = read_capped(response, TEXT_MAX_BYTES)
        except UnsafeUrlError as exc:
            logger.warning("Refusing subtitle URL: %s", exc)
            return None
        except Exception as exc:
            logger.warning("Could not fetch subtitle: %s", exc)
            return None
        finally:
            session.close()

        text = body.decode("utf-8", "replace")
        if not text.lstrip().startswith("#EXTM3U"):
            return body

        segments = [
            urljoin(url, line.strip())
            for line in text.splitlines()
            if line.strip() and not line.startswith("#")
        ]
        if not segments:
            logger.warning("Subtitle playlist contained no segments")
            return None

        # A master playlist points at another playlist; a media playlist points
        # at the cue files themselves. Recurse once for the former.
        if len(segments) == 1:
            nested = self._fetch_subtitle(segments[0], headers, _depth + 1)
            return nested

        parts = []
        for segment in segments:
            chunk = self._fetch_subtitle(segment, headers, _depth + 1)
            if chunk:
                parts.append(chunk)

        if not parts:
            return None
        return _merge_webvtt(parts)


def _merge_webvtt(parts):
    """Join WebVTT segments into one track, keeping a single WEBVTT header."""
    merged = [b"WEBVTT\n"]
    for part in parts:
        text = part.decode("utf-8", "replace").lstrip("\ufeff")
        lines = text.splitlines()
        # Drop the per-segment header and the blank line after it.
        if lines and lines[0].strip().startswith("WEBVTT"):
            lines = lines[1:]
            while lines and not lines[0].strip():
                lines = lines[1:]
        body = "\n".join(lines).strip()
        if body:
            merged.append(b"\n" + body.encode("utf-8") + b"\n")
    return b"".join(merged)
