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

Prefer clicking? `brightspace-sync gui` (or just double-click the downloaded
file) opens a window for setup + run. Everything below is the command-line path.

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

> Mac will show a "wants to use your keychain" / "Chrome Safe Storage" prompt.
> It's just for the OS to allow the script to read the browser's cookie file,
> click **Allow** for the script to work.

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
      content_index.json            # every topic + its path (relative), parse this
      content_index.md              # readable outline
      <Module>/<file(s)>, <link>.url
    Announcements/*.md
    Assignments/<name>/instructions.md + attachments
    .bs_manifest.json
  ECE 200 - Linear Circuits/        # a merged entry: one folder, one subfolder per course
    Lecture/   course.json  Content/ ...  .bs_manifest.json
    Lab/       course.json  Content/ ...  .bs_manifest.json
```

### Videos

Lecture videos in Brightspace are **embedded players**

Each video topic is turned into a `<name>.url` shortcut that opens the Brightspace page,
and its `<name>.html` is rewritten into a small page with a `<base href>` + direct links
so it opens in a browser.

If you set `include.videos: true`, the actual `<name>.mp4` is downloaded next to the `.html`.

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
