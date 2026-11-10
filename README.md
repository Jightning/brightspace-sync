# Brightspace Sync

Do you want easy, local, and permanent access all your Brightspace data (including grades, lectures, announcements, etc.)?

Unfortunately many organizations like Purdue don't allow easy/public access to the Brightspace API, so this exists as an easy and safe alternative (read-only and no passwords or credentials collected/stored).

This takes your cookies from a current Brightspace session, creates an authenticated session, and requests your chosen course data from Brightspace, so you can do whatever you'd like with it, locally on your machine.

A manifest is also kept, so that if you run again, it'll just pull what's been changed since the last run.

---
[rel]: https://github.com/Jightning/brightspace-sync/releases/latest

## Install

### Option A: download (using GUI)

Get the file for your OS from the [latest release][rel]:

| OS | file |
| ---- | ------ |
| macOS (Apple Silicon) | `brightspace-sync-macos-arm64` |
| Windows | `brightspace-sync-windows-x64.exe` |
| Linux | `brightspace-sync-linux-x64` |

Put it in a folder of its own and **double-click** to run.
Follow the steps shown in the wizard and run.

On an Intel Mac, use Option B below.

Running the downloaded file from a terminal via a command (as shown in all the uses below) uses the CLI.

### Option B: with Python (3.9+)

```bash
uv tool install git+https://github.com/Jightning/brightspace-sync
# or: 
pipx install git+https://github.com/Jightning/brightspace-sync
```

---

### Development

```bash
uv sync
uv run brightspace-sync
uv run pytest
```

Prefix every `brightspace-sync` command below with `uv run` in development

## Setup

Run the wizard (writes to `config.yaml`):

```bash
brightspace-sync setup
```

Or copy `config.example.yaml` to `config.yaml` and edit from there (options commented)

Outputs will go to `bs_data/` (.gitignored)

```bash
brightspace-sync whoami   # prints your name — confirms auth works
brightspace-sync doctor   # full diagnosis if it doesn't
```

> If automatically reading the cookies fails, a webview with the Brightspace
> login opens, and the cookies are read from there. Set `browser: webview`
> to always use the window.

## Config

`config.yaml` will have comments next to its options describing what they do.

### Select the courses you want to retrieve

```bash
brightspace-sync list
```

This will print `- match:` lines for each course.
Drop the ones you want to retrieve under `courses.allow` in `config.yaml` if you use `mode: allowlist`

### Folder names & merging (`courses.map`)

`courses.map` allows you to select the folder name for each retrieved course.
A `match` is used to find the most similar course name
(Eg if you set match to 27000, it will pick up any course with either 27000 in the name or id. If multiple get matched, then they get merged so careful)

```yaml
courses:
  map:
    - name: "ECE 270 - Digital System Design" # The folder name you want
      match: "ECE 27000"

    - name: "ECE 200 - Linear Circuits"       # Merge two Brightspace sections 
                                              # (each get their own subfolder within `name`)
      match: ["ECE 20001", "ECE 20007"]       # The course matches to use
      subfolders:                             # Select subfolder names
        "ECE 20001": "Lecture"
        "ECE 20007": "Lab"
```

Renaming a mapped folder later within the config is fine.

## Usage

```bash
brightspace-sync sync  # main retrieval

--course 123456       # Only sync one course (using Brightspace org-unit id)
--only content,grades # only sync these
--force               # re-download even if unchanged
--prune               # deletes local files removed in Brightspace (`prune: true` in config)
--dry-run             # just shows, writes nothing
--skip-videos         # don't download videos this run (overrides include.videos)
--transcribe          # write a .md transcript next to each lecture video
```

If you want an alias (so you just run with `bssync sync`):

```bash
echo 'alias bssync="~/Desktop/Projects/bs_search/.venv/bin/python -m brightspace_sync"' >> ~/.zshrc
```

### Considerations

**The browser session expires mid-sync:** The current course is finished up to
that point, its manifest is saved, and the run stops with a "sign back in"
message and exit code 2. Re-run after opening Brightspace; already-downloaded
files are skipped, so it picks up where it left off.

**Ctrl-C mid-sync:** Same idea. Downloads are written to a `.part` file and
renamed into place only when complete, and the `.part` is deleted on interrupt —
so you never get a truncated file. The manifest for the in-progress course is
saved on the way out; the next run resumes.

**Something changed in Brightspace since last run:** A changed file (new
last-modified or size) is re-downloaded and overwritten. A new file is added. A
file *removed* from Brightspace is left in place by default; run `sync --prune`
to delete local files that are no longer in Brightspace (prune only touches the
sections you actually synced that run).

## Data

```
bs_data/courses/
  index.json                        # you + all enrollments + folder for each
  ECE 270 - Digital System Design/  # name from courses.map (else auto)
    course.json  grades.json  grades.csv
    Content/
      index.html                    # open in a browser: saved files + online tools
      content_index.json            # every topic + its path (relative), parse this
      content_index.md              # readable outline
      Course overview/              # overview text (.md/.html) + attached syllabus/file
      Course links/*.url            # navigation tools, including external Syllabus
      <Module>/<file(s)>, <link>.url
    Announcements/*.md
    Assignments/<name>/instructions.md + attachments
    .bs_manifest.json
  ECE 200 - Linear Circuits/        # a merged entry: one folder, one subfolder per course
    Lecture/   course.json  Content/ ...  .bs_manifest.json
    Lab/       course.json  Content/ ...  .bs_manifest.json
```

Open a course's `Content/index.html` to browse your download. Saved files open
offline; tools requiring the website are marked **online**. Simple Syllabus's
Brightspace sign-in handoff is supported: its syllabus PDF is saved under
`Content/Course links/Online downloads/`, and the index opens that local PDF.
Other supported documents linked from Content are saved in the corresponding
module's `Online downloads/` folder. The original online shortcut is retained.
Unavailable or unsupported sources remain online links with a warning; a login
page is never saved as a syllabus. Authentication tokens and signed storage URLs
are kept only in memory.

These additions run with the existing `content` section; there is no new config
switch. An overview attachment is downloaded when Brightspace provides one.
Since overviews have no modification timestamp, their text and attachments are
checked again on each run. A course without an overview can still have a Syllabus
link. This does not export interactive tools such as quizzes or discussions.

External downloads use exact HTTPS host names in `external_downloads.allowed_hosts`
(default: `purdue.simplesyllabus.com`). Add trusted document hosts to fetch direct
PDF, TXT, MD, CSV, DOCX, XLSX or PPTX links, including download URLs without an
extension when the response identifies the document type. Arbitrary scripts,
archives and installers are not downloaded. Every redirect is validated; private
network destinations are blocked. Brightspace cookies are not sent to document
hosts. Simple Syllabus can authorize only an exact temporary PDF URL in its own
syllabus storage path. Set `external_downloads.enabled: false` to disable these
additional downloads while keeping existing copies.

File, run and storage limits default to **25 MiB / 250 MiB / 1 GiB**, configured
under `download_limits` in `config.example.yaml`. They also cover existing file
and attachment downloads, description images, videos, captions and temporary
audio. The CLI or app pauses and asks whether to exceed the relevant limit.
**No on a file limit skips only that file** and continues syncing. It will ask
again on the next sync; the rejection is not saved. No on a run or storage limit
stops the sync, preserves completed files and saves the manifest. Unattended runs
skip files exceeding the file limit and stop on run or storage limits. Approvals apply only to the current run (file
limits are approved per file); they do not rewrite your settings.

Storage accounting includes existing files under `data_dir` plus incoming file
bytes, conservatively retaining space for temporary replacements and video
merges. It does not delete older files to make room. Downloads preserve 64 MiB of
free disk space. yt-dlp runs in-process with limit hooks; unsupported unmetered
media protocols stop explicitly. With capped sync, speech recognition requires
the selected model to be cached beforehand, so an automatic model installation
cannot bypass the limits. Small API responses, generated indexes and logs are
not part of the per-run file-transfer quota.

### Videos

Lecture videos in Brightspace are **embedded players**

Each video topic is turned into a `<name>.url` shortcut that opens the Brightspace page,
and its `<name>.html` is rewritten into a small page with a `<base href>` + direct links
so it opens in a browser.

If you set `include.videos: true`, the actual `<name>.mp4` is downloaded next to the `.html`.

### Transcripts

`brightspace-sync sync --transcribe` (or `include.transcripts: true`) writes a
`<name> - transcript.md` next to each video topic: a short header saying where
the text came from, then timestamped paragraphs.

Two sources are tried, cheapest first:

1. **The video's own captions.** Kaltura/BoilerCast, YouTube and Panopto
   usually serve a caption track for the entry, and yt-dlp can fetch it without
   downloading the video. This is the normal case: fast, free, and as accurate
   as whoever wrote the captions.
2. **Local speech recognition**, for videos with no caption track. This needs
   the optional extra:

   ```bash
   pip install "brightspace-sync[transcribe]"
   ```

   It runs [faster-whisper](https://github.com/SYSTRAN/faster-whisper) on your
   own machine — nothing is uploaded. The audio comes from the `.mp4` if
   `include.videos` already saved one, otherwise an audio-only copy is fetched
   to a temp folder and deleted afterwards. Set `transcribe_model` in the config
   to trade speed for accuracy. Without the extra installed, videos that have
   captions still get transcripts; the rest are skipped with a warning.

## Debugging

### `whoami` fails with "HTTP 403 / cookies are stale"

Run `./.venv/bin/python -m brightspace_sync doctor`. It prints which cookies were found
and does a live check. Common causes:

* **Only `d2lSessionVal` was found, not `d2lSecureSessionVal`.** Brightspace
  needs both. Fully sign in at `https://purdue.brightspace.com/d2l/home` and
  re-run. (The tool now queries the cookie DB by `brightspace.com` so the
  secure cookie — which is scoped to `.brightspace.com` — is no longer filtered
  out; make sure you're on the fixed version.)
* **Wrong Chrome profile.** `doctor` lists the profiles it sees. Set
  `browser: chrome` and `chrome_profile: "Profile 1"` in `config.yaml`, or pass
  `--profile "Profile 1"`.
* **Cookie value came back empty** = the browser cookie store couldn't be
  decrypted. Fully quit the browser and re-run, allow the macOS Keychain prompt,
  or try another browser (sign in to Brightspace there, then set `browser:` to
  it). Cookie reading uses `rookiepy`, which handles Chrome 127+'s app-bound
  encryption on Windows.
* **`doctor` says the web dashboard loads but the API still 403s** = this
  Brightspace instance blocks cookie-based API access. Use the manual method:

  ```
  # config.yaml
  browser: cookies_txt
  ```
  Then export cookies for `purdue.brightspace.com` (a "Get cookies.txt" browser
  extension, Netscape format) to `bs_data/cookies.txt`, or hand-write it:
  ```
  # Netscape HTTP Cookie File
  .brightspace.com	TRUE	/	TRUE	9999999999	d2lSecureSessionVal	<value>
  purdue.brightspace.com	FALSE	/	TRUE	9999999999	d2lSessionVal	<value>
  ```
  Values come from DevTools → Application → Cookies. `chmod 600 bs_data/cookies.txt`.

## Scheduling

Default use is manual. `scripts/com.user.brightspace-sync.plist.example` is a launchd job
that runs `scripts/run-sync.sh` twice a day — but it only works while your
browser still holds a live Brightspace session, and on expiry it exits cleanly
with one macOS notification (no password prompts). Logs go to
`bs_data/logs/`.

```bash
cp scripts/com.user.brightspace-sync.plist.example ~/Library/LaunchAgents/com.user.brightspace-sync.plist
launchctl load   ~/Library/LaunchAgents/com.user.brightspace-sync.plist
launchctl unload ~/Library/LaunchAgents/com.user.brightspace-sync.plist   # to stop
```

---

## Is this allowed?

You're downloading your own course materials — ones you can already open in a
browser — through the same interface the browser uses, when you ask it to. Not
legal advice: still skim your university's IT acceptable-use policy and any "no
automated access" line in a syllabus (a professor can set stricter rules for
their course — just exclude it in `config.yaml`). Don't point it at an account
that isn't yours, and don't lower the rate limit.
