# jiffy

A general Chrome agent. Give it one goal in plain language; it browses the real web and does the work.

## Quickstart

```bash
./bin/jiffy setup          # one time: prepare the jiffy browser profile
./bin/jiffy login          # opens the jiffy Chrome - log into sites once
./bin/jiffy "your goal"    # run anything, or: ./bin/jiffy run  (reads goal.txt)
```

No Allow dialogs, ever. The jiffy Chrome uses its own persistent profile (your
real Chrome app, not a bundled build) and a background daemon keeps one browser
alive so runs are fast. Close it and the next run relaunches it.

**Log in once.** Chrome ties cookies to the profile, so copied cookies do not
authenticate. Run `./bin/jiffy login`, sign into the sites you need there once,
and it persists (the profile is never re-synced after the first launch).

The CLI (no install needed):

```bash
./bin/jiffy "your goal"      # run a goal
./bin/jiffy run              # run the goal in goal.txt
./bin/jiffy setup            # prepare the profile
./bin/jiffy login            # open the jiffy Chrome to log in
./bin/jiffy doctor           # status
./bin/jiffy stop             # stop the daemon
```

Prefer editing a file? Put your goal in `goal.txt` (lines starting with `#` are
ignored), then just:

```bash
./bin/jiffy run
```

Manual install:

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # add TYPESAFE_API_KEY + TEXT_MODEL_API_KEY
python -m jiffy setup
python -m jiffy "your goal"
```

```
snapshot visible DOM → Jev picks operation + target (one call) → validated input → repeat
```

* **Jev** ([TypeSafe](https://typesafe.ai)) is the decider: `CLICK`, `TYPE_TEXT`, `SELECT`, `UPLOAD`, `SCROLL`, `WAIT`, `DONE`, `BLOCKED` with calibrated confidence.
* **A cheap OpenRouter model** (DeepSeek, Kimi, ...) writes text only when `TYPE_TEXT` is chosen.
* **Code** owns execution: model output never becomes selectors, coordinates, shell commands, or JS.
* **Human-in-the-loop by default**: low confidence, missing file, or `BLOCKED` stops and asks you.

This is a faithful port of [`browser-use/jev-ultrafast`](https://github.com/browser-use/jev-ultrafast) (MIT) onto Playwright, with `UPLOAD` added so it can attach a resume.

## Install

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # fill TYPESAFE_API_KEY + TEXT_MODEL_API_KEY
```

## Use

```bash
python -m jiffy "Find the Wikipedia article about Gödel's incompleteness theorems" \
  --url https://en.wikipedia.org/wiki/Main_Page
```

### Use your existing Chrome (logins stay)

This is what `browser-harness` / `jev-ultrafast` do: attach to the Chrome you already
have open, so every logged-in session is available. The agent opens a **new background
tab** and does not steal focus.

### One-time setup

```bash
python setup_chrome.py            # targets 127.0.0.1:9222
```

It finds (or launches) a Chrome debugging server, waits for you to **Allow** the
connection, then saves the link to `.env` as `JIFFY_CDP_URL`. After that, plain
runs attach automatically:

```bash
python -m jiffy "Apply to this job with my resume" \
  --url https://jobs.ashbyhq.com/example/123 \
  --resume ~/Documents/resume.pdf --profile profile.yaml
```

**Chrome 144+ asks for permission on every connection.** jiffy can click Allow for
you, but only if the app you launch it from has *System Settings > Privacy &
Security > Accessibility* permission (setup opens that pane for you). Otherwise
click Allow yourself while setup waits.

### The jiffy browser (recommended: no Allow dialogs, ever)

Chrome 136+/144+ always prompts for a CDP connection on your **default** profile,
and that prompt is not reliably clickable. So jiffy uses its **own** Chrome on a
non-standard profile — Chrome never prompts there.

```bash
./bin/jiffy setup      # prepare the profile
./bin/jiffy login      # opens the jiffy Chrome
```

What happens once:

1. A jiffy Chrome window opens (your real Chrome app). Cookies are copied but
   do **not** authenticate, so log into the sites you need **once** here. The
   profile persists and is not re-synced.
2. Run goals — no prompts, no extra windows:

```bash
./bin/jiffy "Open my Gmail and summarize the first email"
./bin/jiffy "Apply to https://jobs.ashbyhq.com/acme/123 with my resume" --resume ~/resume.pdf
```

A background **daemon** holds one connection to that Chrome, so runs are fast and
there is exactly one browser. If you close the jiffy Chrome, the next run
relaunches it and reconnects automatically.

Manage it:

```bash
python -m jiffy --doctor        # status
python -m jiffy --stop-daemon   # stop the daemon
python setup_chrome.py          # re-run to relaunch the browser
```

The older attach path is still available if you want it:

```bash
python setup_chrome.py --attach-existing   # use your real Chrome (click Allow)
```

`--tab-match <text>` attaches to an existing tab whose URL contains the text;
`--current-tab` drives the last tab instead of opening a new one. jiffy never
closes your browser; it only closes the tab it opened.

### Or use a dedicated persistent profile directly

```bash
python -m jiffy "..." --url ... --user-data-dir .chrome-profile
```


## When it's unsure

The agent stops instead of guessing. In an interactive terminal it asks:

```
needs user: Low confidence (0.60) on CLICK at 'https://web.whatsapp.com/'.
  c = continue anyway   g = give guidance   q = quit   (Enter = continue)
choice [c]:
```

- **c** (or Enter) — keep going with the confidence gate relaxed.
- **g** — type guidance (e.g. "click the first image"); it's added to the goal and the run continues.
- **q** — stop and leave the page as-is.

Non-interactive runs (`--yes`, or no TTY) just report `needs_user` and exit.

## How it works

| File | Job |
|---|---|
| `jiffy/snapshot.js` | Atomic DOM snapshot: visible controls, text, guards, page key |
| `jiffy/browser.py` | Playwright transport: `observe`, `fresh`, `act`; launch / persistent / CDP attach |
| `jiffy/model.py` | One TypeSafe call per cycle + the text helper |
| `jiffy/questions.py` | Policy instructions and step budget |
| `jiffy/agent.py` | The loop, confidence gate, verifier hook |
| `jiffy/profile.py` | Personal facts + tiny keyword retrieval for the text helper |
| `jiffy/profile_sync.py` | Copies cookies/theme into the jiffy profile (first launch; Chrome may not honor copied cookies) |
| `jiffy/daemon.py` | Persistent CDP connection daemon (`--serve`, one Allow per session) |
| `jiffy/approve.py` | macOS helper that clicks Chrome's "Allow remote debugging?" sheet |
| `jiffy/log.py` | Logging setup (`--log quiet\|normal\|verbose`) |
| `setup_chrome.py` | One-time: launch the jiffy Chrome and save the CDP link |

Every action is resolved from an observed node and re-checked for freshness, visibility, and occlusion immediately before input.

## Limits

* Chrome only. No Firefox/Safari.
* Shadow DOM, iframes, canvas, new tabs, and nested (in-element) scrolling are not handled yet.
* `DONE` is not proof: pass a verifier to check the outcome, otherwise review the result.
* CAPTCHA / OTP / 2FA always stop for a human.
* Respect each site's terms. Keep batches small.

## Env

See `.env.example`. Required: `TYPESAFE_API_KEY`, `TEXT_MODEL_API_KEY`.
Optional: `JIFFY_PROFILE`, `JIFFY_CDP_URL`, `JIFFY_USER_DATA_DIR`, `JIFFY_LOG`.

## Personal facts (profile)

The text helper must never invent personal information. `profile.yaml` supplies real
facts, and only the relevant ones are injected per field:

```bash
cp profile.example.yaml profile.yaml   # then edit
python -m jiffy "..." --url ... --profile profile.yaml
```

`job.resume` in the profile is used automatically for `UPLOAD` targets if `--resume`
is not passed.

## License

MIT. Portions ported from `browser-use/jev-ultrafast` (MIT).
