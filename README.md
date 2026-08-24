<a name="readme-top"></a>

<!-- PROJECT SHIELDS -->

[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]

<!-- PROJECT LOGO -->
<br />
<div align="center">
  <a href="https://github.com/FallingLights/Teachable-Dl">
    <img src="images/logo.png" alt="Logo" width="80" height="80">
  </a>

<h3 align="center">Teachable-dl</h3>

  <p align="center">
    A downloader for downloading courses from the Teachable platforms.
    <br />
    <br />
    <a href="https://github.com/FallingLights/Teachable-Dl/issues">Report Bug</a>
    ·
    <a href="https://github.com/FallingLights/Teachable-Dl/issues">Request Feature</a>
  </p>
</div>

<!-- TABLE OF CONTENTS -->
<details>
  <summary>Table of Contents</summary>
  <ol>
    <li>
      <a href="#about-the-project">About The Project</a>
      <ul>
        <li><a href="#built-with">Built With</a></li>
      </ul>
    </li>
    <li>
      <a href="#getting-started">Getting Started</a>
      <ul>
        <li><a href="#prerequisites">Prerequisites</a></li>
        <li><a href="#installation">Installation</a></li>
      </ul>
    </li>
    <li><a href="#usage">Usage</a></li>
    <li><a href="#roadmap">Roadmap</a></li>
    <li><a href="#contributing">Contributing</a></li>
    <li><a href="#license">License</a></li>
    <li><a href="#contact">Contact</a></li>
    <li><a href="#acknowledgments">Acknowledgments</a></li>
  </ol>
</details>

<!-- ABOUT THE PROJECT -->

## About The Project

<!--[![Product Name Screen Shot][product-screenshot]](https://example.com) -->

Teachable-dl is a Python-based downloader for downloading courses from the Teachable platform. It provides a command-line interface for easily downloading course materials such as videos, slides, and other resources, allowing users to access course content offline at their own pace. With Teachable-dl, users can conveniently download and organize all course materials in a single location, enabling easy access and review of course content without the need for an active internet connection.

⭐ `Star` this repository if you find it valuable and worth maintaining.

👁 `Watch` this repository to get notified about new releases, issues, etc.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

### Built With

- [![Python][Python.org]][Python-url]
- [![Selenium][Selenium.org]][Selenium-url]
- [![yt-dlp][yt-dlp.org]][yt-dlp-url]

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- GETTING STARTED -->

## Getting Started

To get the program up and running follow these simple steps.

### Prerequisites

> Requires **Python 3.10 or newer** (selenium, seleniumbase and yt-dlp all
> dropped support for older versions).

This is an example of how to list things you need to use the software and how to install them.
(You can also run this script on a windows machine)
#### For Linux users
- yt-dlp

```sh
python3 -m pip install -U yt-dlp
```

- ffmpeg (strongly recommended — without it, separate video and audio streams
  cannot be merged and you get an `.mp4` that will not play)

```sh
sudo apt install ffmpeg
```

- Chrome

```sh
# Ubuntu
sudo apt install chromium-browser

# Debian
sudo apt install chromium
```

#### For Windows users
- yt-dlp: install using pip. See [yt-dlp's official repo.](https://github.com/yt-dlp/yt-dlp/)

```sh
python3 -m pip install -U yt-dlp
```

- ffmpeg: Download and install from [ffmpeg's official website.](https://ffmpeg.org/download.html)
> Make sure to add ffmpeg to your PATH. Without ffmpeg the downloader falls back
> to lower-quality single-file streams so that the result is still playable.

- Chrome: Download and install from [Google Chrome's official website.](https://www.google.com/chrome/)

### Installation

1. Clone the repo

```sh
git clone https://github.com/ryakel/teachable-dl.git
```

2. Enter to the project

```sh
cd teachable-dl
```

3. Set up the environment

```sh
python3 -m venv env
```

4. Activate the environment

```sh
source env/bin/activate
```

5. Install the requirements

```sh
pip install -r requirements.txt
```

### Keeping dependencies current

Dependency floors are set to the versions this fork is tested against, and
Dependabot raises a weekly pull request to keep them moving.

`yt-dlp` is the one worth updating by hand between releases. When a video host
changes its player, an out-of-date `yt-dlp` stops extracting streams, and that
looks identical to the tool being broken:

```sh
python3 -m pip install -U yt-dlp
```

Several closed upstream issues ("ChromeDriver only supports Chrome version 112",
"Not working in Chrome 115", "Not Working in Chrome 119") were nothing more than
a stale `selenium` or `seleniumbase`, so `pip install -U -r requirements.txt` is
worth trying before filing a bug.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- USAGE EXAMPLES -->

## Usage

Activate the environment

```sh
source env/bin/activate
```

Run the program

```sh
python3 main.py --url <course_url> --email <email> --password <password>
```

or run with manual login

```sh
python3 main.py --url <course_url> --man_login_url <man_login_url> --verbose
```

> Make sure to navigate to the url within the first tab and check the console for an exact url match.

For a list of all available options and up-to-date parameters, use the --help command:
```shell
python main.py --help
```

### Keeping credentials out of your shell history

Anything you pass with `--password` is visible in your shell history and in
`ps`. Prefer the environment, or just omit the flag and be prompted:

```sh
export TEACHABLE_EMAIL=you@example.com
export TEACHABLE_PASSWORD='your-password'
python3 main.py --url <course_url>
```

### Two-factor authentication

If your account is protected by an authenticator app, pass the base32 secret and
the code is generated for you:

```sh
python3 main.py --url <course_url> --totp-secret 'JBSW Y3DP EHPK 3PXP'
```

Without `--totp-secret` you are prompted for the code, and emailed one-time
codes are prompted for too.

### Running in the background

By default the browser window is visible and takes focus. `--headless` keeps it
out of your way:

```sh
python3 main.py --url <course_url> --headless
```

Cloudflare challenges cannot be solved headless, so if a download stalls at the
challenge, re-run without the flag.

### Offline viewing

Downloads are laid out as:

```
courses/<Course Title>/
├── index.html                     generated table of contents
├── course.html                    the curriculum page
├── teachable-dl-manifest.json     what was downloaded, used by --rewrite-only
└── 01-Chapter Name/
    ├── 01-Lecture.html            lecture page, rewritten for local viewing
    ├── 01-Lecture.mp4
    ├── 01-Lecture.en.vtt
    └── 01-Lecture/                attachments for that lecture
        └── Workbook.pdf
```

Open `index.html` to browse the course offline. Each saved lecture page plays
its downloaded video inline and links to the previous and next lecture on disk.

Pass `--no-offline-rewrite` to keep the pages exactly as the server sent them.
To add local navigation and players to a course you downloaded earlier, without
downloading anything again:

```sh
python3 main.py --rewrite-only "courses/<Course Title>"
```

### Useful options

| Option | What it does |
| --- | --- |
| `--headless` | Run the browser without a visible window |
| `--totp-secret` | Generate two-factor codes automatically |
| `-o, --output-dir` | Where courses are written (default `./courses`) |
| `--concurrent-fragments` | Parallel fragment downloads (default 16); lower it if you get rate limited |
| `--save-pdf`, `--save-screenshot` | Also save each lecture page as PDF / PNG |
| `--ascii-filenames` | Transliterate non-Latin titles instead of keeping them |
| `--no-resume` | Re-download files that already exist |
| `--rewrite-only` | Rebuild offline navigation for an existing download |

Re-running the same command resumes: files already on disk are skipped.

<!-- SECURITY -->

## Security notes

A course page is remote input. Everything on it — attachment links, the course
image, subtitle playlists, and every title — is chosen by whoever runs the
school, so this fork treats those as untrusted:

- **Cookies are scoped to their domain.** The browser's session is reused for
  attachment downloads, but each cookie keeps the domain the browser gave it, so
  a redirect to a third-party host cannot carry your Teachable session with it.
- **Redirects are followed one hop at a time**, and every hop is re-checked
  before the request goes out.
- **Private, loopback and link-local addresses are refused.** A link to
  `http://169.254.169.254/…` or `http://127.0.0.1:6379/` is not fetched. If you
  self-host a school on your own network, `--allow-private-hosts` opts back in.
- **Downloads are capped and written atomically.** `--max-file-size` (8 GiB by
  default) bounds any single attachment, and files land in `.part` first so an
  interrupted transfer never leaves a truncated file that a later run mistakes
  for a finished one.
- **Titles are sanitized before they touch the filesystem**, so a lecture named
  `../../.ssh/authorized_keys` cannot escape the output directory.

One thing to be aware of: the saved lecture HTML is the page **as served to a
logged-in browser**. Teachable embeds account context in that markup, so the
course folder may contain tokens tied to your session. Treat a downloaded course
as private, and think twice before sharing the folder or syncing it somewhere
public.

Prefer `TEACHABLE_PASSWORD` or the interactive prompt over `--password`: a
password on the command line is visible in your shell history and to anyone who
can run `ps`.

<!-- CHANGES -->

## What this fork changes

This fork picks up the upstream project and works through the open bug reports
and feature requests.

**Fixes**

- **Downloads were extremely slow** ([#59], [#51]) — the yt-dlp option enabling
  parallel fragment downloads was misspelled (`concurrentfragments` instead of
  `concurrent_fragment_downloads`), so yt-dlp silently ignored it and every
  stream was fetched one fragment at a time.
- **Downloaded mp4 would not play** ([#41]) — a conversion pass ran
  unconditionally alongside a format selector that needed merging. Without
  ffmpeg neither happened, leaving an unplayable file with an `.mp4` name.
  ffmpeg is now detected, the redundant re-encode is gone, and output is
  validated before a download counts as finished.
- **Videos over ~100 minutes failed** ([#55]) and **403 Forbidden on some
  videos** ([#44]) — request headers were hardcoded to `player.hotmart.com`
  regardless of the real embed host, and signed URLs expire before a long
  download finishes. Headers now follow the actual embed, and an expired link is
  re-extracted so the download resumes.
- **`web view not found` / `invalid session id`** ([#53], [#51]) — the
  Cloudflare bypass closed tabs without checking the driver survived. Window
  handles are now verified after every navigation, and a crashed browser is
  restarted and re-authenticated instead of failing every remaining lecture.
- **Courses requiring an OTP could not be downloaded** ([#56]) — login only
  recognised one form layout, and credentials were injected by splicing them
  into a JavaScript string, which broke on any password containing a quote.
  Login now handles SSO layouts, types credentials as a user would, and supports
  automatic TOTP.
- **"Unsupported course template"** ([#54], [#49], [#43]) — only three themes
  were recognised. Unknown themes now fall back to a parser that finds lecture
  links directly, so a new template degrades to slightly worse chapter names
  rather than no download at all.
- **Video does not play from the saved page** ([#63]) — saved pages kept the
  remote player iframe, which shows "Your video is processing" offline. Player
  iframes are replaced with a local `<video>` element and subtitle tracks.
- **Navigation in saved pages went nowhere** ([#58]) — links between lectures
  are rewritten to relative paths in the downloaded folder structure, and a
  generated `index.html` lists the whole course.
- **Non-Latin titles were destroyed** ([#37]) — titles were stripped to ASCII,
  so Cyrillic, Chinese and Japanese names collapsed to nothing. Unicode is now
  preserved; `--ascii-filenames` restores the old behaviour.

**New**

- **Attachment downloads** ([#38]) — PDFs, MP3s, images and other lecture files
  are saved, using the authenticated browser session (the previous `wget` calls
  sent no cookies and often saved an error page).
- **Save pages as PDF or image** ([#39]) — `--save-pdf` and `--save-screenshot`.
- **Headless mode** ([#45]) — `--headless` stops the browser stealing focus.
- Resume support, `--rewrite-only`, credentials via environment or prompt, and a
  test suite.

[#37]: https://github.com/FallingLights/Teachable-dl/issues/37
[#38]: https://github.com/FallingLights/Teachable-dl/issues/38
[#39]: https://github.com/FallingLights/Teachable-dl/issues/39
[#41]: https://github.com/FallingLights/Teachable-dl/issues/41
[#43]: https://github.com/FallingLights/Teachable-dl/issues/43
[#44]: https://github.com/FallingLights/Teachable-dl/issues/44
[#45]: https://github.com/FallingLights/Teachable-dl/issues/45
[#49]: https://github.com/FallingLights/Teachable-dl/issues/49
[#51]: https://github.com/FallingLights/Teachable-dl/issues/51
[#53]: https://github.com/FallingLights/Teachable-dl/issues/53
[#54]: https://github.com/FallingLights/Teachable-dl/issues/54
[#55]: https://github.com/FallingLights/Teachable-dl/issues/55
[#56]: https://github.com/FallingLights/Teachable-dl/issues/56
[#58]: https://github.com/FallingLights/Teachable-dl/issues/58
[#59]: https://github.com/FallingLights/Teachable-dl/issues/59
[#63]: https://github.com/FallingLights/Teachable-dl/issues/63

<!-- DEVELOPMENT -->

## Development

The code lives in the `teachable_dl` package; `main.py` is a thin entry point
kept for backwards compatibility.

| Module | Responsibility |
| --- | --- |
| `cli.py` | Argument parsing and entry point |
| `browser.py` | Browser lifecycle, Cloudflare, session recovery |
| `auth.py` | Login, SSO layouts, OTP/TOTP |
| `templates.py` | Reading the curriculum, including the generic fallback |
| `media.py` | Video and subtitle downloading via yt-dlp |
| `attachments.py` | Lecture attachments |
| `offline.py` | Rewriting saved pages for offline viewing |
| `downloader.py` | Orchestration |

Run the tests with:

```sh
pip install -e ".[dev]"
python -m pytest
```

The unit suite covers the pure logic — filename sanitizing, yt-dlp options, HTML
rewriting, URL safety, curriculum grouping and argument handling — and needs no
browser or network access.

There is also a browser-driven integration suite that runs the real parsers
against a synthetic Teachable-like site served from localhost, so template
detection and curriculum scraping can be tested without a Teachable account. It
skips automatically when Chrome is unavailable.

See [TESTING.md](TESTING.md) for how to run every layer, including a staged
plan for testing against a real account.

<!-- ROADMAP -->

## Roadmap

See the [open issues](https://github.com/FallingLights/Teachable-Dl/issues) for a full list of proposed features (and known issues).

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- CONTRIBUTING -->

## Contributing

Contributions are what make the open source community such an amazing place to learn, inspire, and create. Any contributions you make are **greatly appreciated**.

If you have a suggestion that would make this better, please fork the repo and create a pull request. You can also simply open an issue with the tag "enhancement".
Don't forget to give the project a star! Thanks again!

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- LICENSE -->

## License

Distributed under the GNU LGPLv3 License. See `LICENSE.txt` for more information.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- CONTACT -->

## Contact

[@fallinglight_s](https://twitter.com/fallinglight_s)

Project Link: [https://github.com/FallingLights/Teachable-Dl](https://github.com/FallingLights/Teachable-Dl)

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- ACKNOWLEDGMENTS -->

## Acknowledgments

- [merberich](https://github.com/merberich)
- [Green0Photon](https://github.com/Green0Photon)

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- MARKDOWN LINKS & IMAGES -->
<!-- https://www.markdownguide.org/basic-syntax/#reference-style-links -->

[contributors-shield]: https://img.shields.io/github/contributors/FallingLights/Teachable-Dl.svg?style=for-the-badge
[contributors-url]: https://github.com/FallingLights/Teachable-Dl/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/FallingLights/Teachable-Dl.svg?style=for-the-badge
[forks-url]: https://github.com/FallingLights/Teachable-Dl/network/members
[stars-shield]: https://img.shields.io/github/stars/FallingLights/Teachable-Dl.svg?style=for-the-badge
[stars-url]: https://github.com/FallingLights/Teachable-Dl/stargazers
[issues-shield]: https://img.shields.io/github/issues/FallingLights/Teachable-Dl.svg?style=for-the-badge
[issues-url]: https://github.com/FallingLights/Teachable-Dl/issues
[license-shield]: https://img.shields.io/github/license/FallingLights/Teachable-Dl.svg?style=for-the-badge
[license-url]: https://github.com/FallingLights/Teachable-Dl/blob/master/LICENSE.txt
[product-screenshot]: images/screenshot.png
[Python.org]: https://img.shields.io/badge/Python-14354C?style=for-the-badge&logo=python&logoColor=white
[Python-url]: https://www.python.org
[Selenium.org]: https://img.shields.io/badge/Selenium-43B02A?style=for-the-badge&logo=selenium&logoColor=white
[Selenium-url]: https://www.selenium.dev
[yt-dlp.org]: https://img.shields.io/badge/yt--dlp-000000?style=for-the-badge&logo=github&logoColor=white
[yt-dlp-url]: https://github.com/yt-dlp/yt-dlp
