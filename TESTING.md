# Testing

Three layers, in increasing order of what they need from you.

| Layer | Needs | Runtime | Covers |
| --- | --- | --- | --- |
| Unit | nothing | ~1s | Filename handling, yt-dlp options, HTML rewriting, URL safety, CLI |
| Integration | Chrome | ~45s | Template detection, curriculum scraping, offline rewrite in a real browser |
| Manual | your Teachable account | minutes | Login, OTP, real video/attachment downloads |

The first two need no account and no internet.

## Layer 1 — unit tests

```sh
pip install -e ".[dev]"
python -m pytest
```

189 tests, about a second, no browser and no network.

## Layer 2 — integration tests

These run the real parsers in a real browser against a synthetic Teachable-like
site served from localhost (`tests/fixtures/site.py`). It reproduces all three
known course templates plus an unknown one, and its lecture pages carry the
awkward markup that has caused bugs: an embedded `__NEXT_DATA__` JSON payload
containing decoy iframes, a commented-out player, a `<noscript>` player, an
`unlocked` wrapper, a duration badge, and the same lecture linked twice.

They skip automatically if Chrome is missing, so `pytest` stays green anywhere.

```sh
# Easiest: pin a driver matching your installed Chrome major version
pip install "chromedriver-py==141.*"        # <- match YOUR Chrome
python -m pytest tests/test_integration.py -v
```

If your Chrome is somewhere non-standard:

```sh
export TEACHABLE_DL_TEST_CHROME=/path/to/chrome
export TEACHABLE_DL_TEST_CHROMEDRIVER=/path/to/chromedriver
python -m pytest tests/test_integration.py -v
```

Check your Chrome version with `google-chrome --version` (or Chrome →
Settings → About Chrome).

## Layer 3 — manual, against your own account

Everything below assumes credentials come from the environment rather than the
command line, so they stay out of your shell history and out of `ps`:

```sh
export TEACHABLE_EMAIL='you@example.com'
export TEACHABLE_PASSWORD='...'
# only if your account uses an authenticator app:
export TEACHABLE_TOTP_SECRET='JBSW Y3DP EHPK 3PXP'
```

Work through the stages in order. Each one only starts if the previous passed —
that way a failure tells you which part is broken.

### Stage 0 — environment

```sh
python main.py --version
ffmpeg -version | head -1
```

`ffmpeg` matters: without it the downloader falls back to lower-quality
single-file streams. If it is missing, install it before judging video quality.

### Stage 1 — login and curriculum, downloading nothing

```sh
python main.py --url '<course-url>' --dry-run -v
```

This is the cheapest and most informative test. It logs in, handles any
Cloudflare or OTP challenge, detects the template, scrapes the curriculum, and
prints what it *would* download — then stops without writing a byte.

Check:
- It logged in without hanging.
- `Template :` is one of `next`, `classic`, `colossal`, or `generic`.
- The chapter and lecture names match what you see in the browser.
- The lecture count matches. **Especially check nothing is missing** — free
  preview lectures being silently skipped was a real bug.
- Non-Latin titles (if your course has any) look correct, not mangled.

If `Template : generic` appears, the theme is one the parser does not know
specifically. That is expected to still work, but chapter names come from
nearby headings, so give them an extra look.

### Stage 2 — one lecture, end to end

```sh
python main.py --url '<course-url>' --limit 1 -v
```

Check `courses/<Course>/<chapter>/`:
- A `.mp4` that **actually plays** in a normal video player.
- A `.html` page.
- Any subtitles as `.vtt`.
- An attachments folder, if that lecture has attachments.

If the video downloads slowly, note the speed — parallel fragment downloading
was silently disabled upstream, so this should be much faster now. Tune with
`--concurrent-fragments N` if the server rate-limits you.

### Stage 3 — offline viewing

Open `courses/<Course>/index.html` in a browser.

- The lecture page plays its video **inline**, rather than showing
  "Your video is processing".
- Previous / Next / Course index links work and stay on your disk.
- Subtitles appear in the video player's caption menu.

Try this in both Chrome and Firefox if you can — captions are inlined
specifically because Chrome blocks subtitle files loaded from `file://`.

### Stage 4 — resume

Run the **exact same command as Stage 2 again**. It should skip what it already
has rather than re-downloading:

```
INFO: Skipping already downloaded video: 01-....mp4
```

Then confirm the rewrite is idempotent — re-run and check the lecture page has
only one navigation bar, not two:

```sh
python main.py --rewrite-only "courses/<Course>"
grep -c 'teachable-dl-nav' "courses/<Course>/<chapter>/01-"*.html   # expect 1
```

### Stage 5 — a full course

```sh
python main.py --url '<course-url>' --headless -v
```

`--headless` keeps the browser from stealing focus. Note that a Cloudflare
challenge cannot be solved headless; if it stalls, re-run without the flag.

### Stage 6 — the awkward cases

Only if your account has them:

| Case | Command | Watch for |
| --- | --- | --- |
| Video over ~2 hours | `--limit 1` on that lecture | Completes; signed-URL expiry is retried and resumed |
| Two-factor account | add `--totp-secret` | No spurious "code was rejected" |
| PDF / MP3 attachments | default | Saved as real files, not HTML error pages |
| Non-Latin titles | `--dry-run` | Readable names; compare against `--ascii-filenames` |
| Multiple videos per lecture | `--limit 1` on that lecture | Each video sits under its own player in the saved page |

## When something fails

Re-run with `-vv` for debug output and stack traces, then capture:

1. The command (**with credentials removed**).
2. The `-vv` output.
3. Your OS, Chrome version, and `python main.py --version`.
4. For a template problem: the course URL, plus the saved `course.html` if one
   exists.

Two cautions before sharing anything:

- **Never paste `-vv` output without reading it first.** Verbose logging
  includes URLs, which can contain signed tokens.
- **The saved HTML is the page as served to a logged-in browser.** It can
  contain account context and session tokens. Strip it, or share it privately.
