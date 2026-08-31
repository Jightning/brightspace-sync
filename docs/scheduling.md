# Running on a schedule (optional)

`brightspace-sync` is built to be run **on demand** — you run it when you want a
refresh. It has no daemon and stores no credentials.

You *can* still put it on a timer, but understand the limitation: it only works
while the browser you point it at still holds a live Brightspace session. Those
time out, and single sign-on won't renew without you. So a scheduled run will
succeed for a while, then start failing until you next open Brightspace. With
`--notify-on-auth-failure` it exits cleanly and shows one desktop notification
when that happens (no password prompts, no retry loop).

The sign-in window only opens for runs you start yourself in a terminal or the
GUI — a scheduled run never pops a window, it just fails as above. If you want a
scheduled job to survive a bit longer, point `browser: cookies_txt` at a
cookies.txt you export by hand; it still expires, but on your terms.

Assumes `brightspace-sync` is on your `PATH` (installed via `pipx`/`uv`, or the
downloaded binary placed somewhere on `PATH`) and a `config.yaml` exists in the
directory you run from — adjust the `cd` / `--config` path as needed.

The downloaded binary opens the GUI when double-clicked, but still runs as the
CLI when you give it a command — so `brightspace-sync sync …` in a scheduled job
works exactly as shown below.

## macOS — launchd

Save as `~/Library/LaunchAgents/com.user.brightspace-sync.plist`, edit the paths,
then `launchctl load ~/Library/LaunchAgents/com.user.brightspace-sync.plist`
(`launchctl unload …` to stop).

```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key><string>com.user.brightspace-sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>cd "$HOME/brightspace" &amp;&amp; brightspace-sync sync --notify-on-auth-failure</string>
    </array>
    <!-- 07:30 and 18:30 daily -->
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>30</integer></dict>
        <dict><key>Hour</key><integer>18</integer><key>Minute</key><integer>30</integer></dict>
    </array>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>/tmp/brightspace-sync.out.log</string>
    <key>StandardErrorPath</key><string>/tmp/brightspace-sync.err.log</string>
    <key>ProcessType</key><string>Background</string>
</dict>
</plist>
```

## Linux — cron

`crontab -e`, then:

```cron
30 7,18 * * *  cd "$HOME/brightspace" && brightspace-sync sync --notify-on-auth-failure >> "$HOME/brightspace/bs_data/logs/cron.log" 2>&1
```

## Windows — Task Scheduler

```powershell
$action  = New-ScheduledTaskAction -Execute "brightspace-sync" -Argument "sync --notify-on-auth-failure" -WorkingDirectory "$HOME\brightspace"
$trigger = @(
  New-ScheduledTaskTrigger -Daily -At 7:30am
  New-ScheduledTaskTrigger -Daily -At 6:30pm
)
Register-ScheduledTask -TaskName "brightspace-sync" -Action $action -Trigger $trigger -Description "Refresh Brightspace course files"
```
