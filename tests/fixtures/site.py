"""A synthetic Teachable-like course site, served over HTTP for integration tests.

The browser-driven half of this project -- template detection, curriculum
scraping, video-source extraction -- cannot be covered by the unit suite, and
testing it against a real school means having an account, an enrolment, and the
patience to re-download a course every time. This module builds a small site
that reproduces the markup shapes the parsers care about, so that code can be
exercised against a real browser with no credentials involved.

The four course pages deliberately cover every branch of template detection,
including the fallback for a theme nobody has taught the parser about.
"""

import contextlib
import functools
import http.server
import socket
import threading

LECTURE_IDS = {
    "next": [(101, "Welcome"), (102, "Setting up"), (201, "Deep dive"), (202, "Wrap up")],
    "classic": [(301, "Kickoff"), (302, "The basics"), (401, "Going further")],
    "colossal": [(501, "Orientation"), (502, "First steps"), (601, "Advanced")],
    "unknown": [(701, "Alpha"), (702, "Beta"), (801, "Gamma")],
}


def _lecture_page(lecture_id, title, neighbours):
    """A lecture page with a player iframe, an attachment and sidebar links.

    The ``__NEXT_DATA__`` blob is not decoration: it reproduces the embedded
    JSON payload that made the offline rewriter corrupt real pages, so a
    regression there shows up in the browser-driven test too.
    """
    sidebar = "".join(
        f'<li><a href="/courses/demo/lectures/{other}">Lecture {other}</a></li>'
        for other in neighbours
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<script id="__NEXT_DATA__" type="application/json">
{{"props":{{"pageProps":{{"lectureId":{lecture_id},
"embed":"<iframe src='https://player.hotmart.com/embed/decoy'></iframe>",
"next":"/courses/demo/lectures/999"}}}}}}
</script>
</head>
<body>
  <nav><ul>{sidebar}</ul></nav>
  <h1>{title}</h1>
  <div class="lecture-attachment lecture-attachment-type-video">
    <a href="/files/{lecture_id}-video.mp4">Download the video</a>
  </div>
  <div class="lecture-attachment lecture-attachment-type-file">
    <a href="/files/{lecture_id}-workbook.pdf">Workbook.pdf</a>
  </div>
  <div class="lecture-attachment lecture-attachment-type-audio">
    <a href="/files/{lecture_id}-audio.mp3">Audio.mp3</a>
  </div>
  <iframe data-testid="embed-player-0"
          src="https://player.hotmart.com/embed/{lecture_id}"></iframe>
  <!-- <iframe src="https://player.vimeo.com/video/decoy"></iframe> -->
  <noscript><iframe src="https://www.youtube.com/embed/decoy"></iframe></noscript>
  <button id="lecture_complete_button">Complete</button>
</body></html>"""


def _next_course():
    items = LECTURE_IDS["next"]
    def bar(lecture_id, title):
        return (f'<div class="bar"><a class="text" '
                f'href="/courses/demo/lectures/{lecture_id}">{title}<span>4:20</span></a></div>')
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Next Course</title></head>
<body><div id="__next"><div class="wrap"><div class="heading">Next Course</div>
<div class="slim-section"><div class="heading">Getting Started</div>
  {bar(*items[0])}{bar(*items[1])}</div>
<div class="slim-section"><div class="heading">Going Deeper</div>
  {bar(*items[2])}{bar(*items[3])}</div>
</div></div></body></html>"""


def _classic_course():
    items = LECTURE_IDS["classic"]
    def item(lecture_id, title):
        return (f'<div class="section-item"><a class="item" '
                f'href="/courses/demo/lectures/{lecture_id}">'
                f'<span class="lecture-name">{title}</span></a></div>')
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Classic Course</title></head>
<body><section><div class="course-sidebar"><div><h2>Classic Course</h2></div></div>
<div class="course-mainbar">
<div class="course-section"><div class="section-title">Module One</div>
  {item(*items[0])}{item(*items[1])}</div>
<div class="course-section"><div class="section-title">Module Two</div>
  {item(*items[2])}</div>
</div></section></body></html>"""


def _colossal_course():
    items = LECTURE_IDS["colossal"]
    def item(lecture_id, title):
        return (f'<a class="block__curriculum__section__list__item__link" '
                f'href="/courses/demo/lectures/{lecture_id}">'
                f'<span class="block__curriculum__section__list__item__lecture-name">'
                f'{title}</span></a>')
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Colossal Course</title></head>
<body><h1 class="course__title">Colossal Course</h1>
<div class="block__curriculum">
<div class="block__curriculum__section">
  <div class="block__curriculum__section__title">Part One</div>
  {item(*items[0])}{item(*items[1])}</div>
<div class="block__curriculum__section">
  <div class="block__curriculum__section__title">Part Two</div>
  {item(*items[2])}</div>
</div></body></html>"""


def _unknown_course():
    """A theme the parser has never seen: flat siblings, a duration badge, an
    "unlocked" wrapper, and the same lecture linked twice."""
    items = LECTURE_IDS["unknown"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Mystery Course</title></head>
<body><h1>Mystery Course</h1>
<h2>Chapter One</h2>
<div class="unlocked">
  <a href="/courses/demo/lectures/{items[0][0]}">{items[0][1]}<span>3:11</span></a>
</div>
<a href="/courses/demo/lectures/{items[1][0]}">{items[1][1]}</a>
<a href="/courses/demo/lectures/{items[1][0]}?from=sidebar">{items[1][1]}</a>
<h2>Chapter Two</h2>
<a href="/courses/demo/lectures/{items[2][0]}">{items[2][1]}</a>
<div class="locked">
  <a href="/courses/demo/lectures/900">Members only</a>
</div>
</body></html>"""


def build_pages():
    """Every path this site serves, as ``{path: (content_type, bytes)}``."""
    pages = {
        "/courses/enrolled/next": ("text/html", _next_course()),
        "/courses/enrolled/classic": ("text/html", _classic_course()),
        "/courses/enrolled/colossal": ("text/html", _colossal_course()),
        "/courses/enrolled/unknown": ("text/html", _unknown_course()),
    }

    every_id = [i for group in LECTURE_IDS.values() for i, _ in group]
    for group in LECTURE_IDS.values():
        for lecture_id, title in group:
            neighbours = [other for other in every_id if other != lecture_id][:3]
            pages[f"/courses/demo/lectures/{lecture_id}"] = (
                "text/html", _lecture_page(lecture_id, title, neighbours)
            )
            pages[f"/files/{lecture_id}-workbook.pdf"] = (
                "application/pdf", "%PDF-1.4 fake workbook"
            )
            pages[f"/files/{lecture_id}-audio.mp3"] = ("audio/mpeg", "ID3 fake audio")
            pages[f"/files/{lecture_id}-video.mp4"] = ("video/mp4", "fake video bytes")

    return {path: (ctype, body.encode("utf-8")) for path, (ctype, body) in pages.items()}


class _Handler(http.server.BaseHTTPRequestHandler):
    def __init__(self, pages, *args, **kwargs):
        self.pages = pages
        super().__init__(*args, **kwargs)

    def do_GET(self):  # noqa: N802 - required name
        path = self.path.split("?")[0].rstrip("/") or "/"
        entry = self.pages.get(path)
        if entry is None:
            self.send_error(404)
            return
        content_type, body = entry
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep the test output readable


@contextlib.contextmanager
def serve():
    """Run the fixture site on a free port; yields its base URL."""
    pages = build_pages()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", port), functools.partial(_Handler, pages)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
