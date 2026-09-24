# Roadmap

Ideas captured so they are not forgotten. Nothing here is committed to a release.

## Site skills to add (`skills/<host>.md`)

One short skill per site makes stateful flows reliable. Planned:

- `www.linkedin.com.md` — feed search, profile, Message overlay/composer, Easy Apply.
- `mail.google.com.md` — inbox, compose, send, search.
- `x.com.md` — search, compose post, reply, messages.
- `web.whatsapp.com.md` — already added: open chat, send, @mentions.
- `github.com.md` — repo search, issues, PRs.
- `www.google.com.md` — search, filters, result clicks.
- `www.youtube.com.md` — search, open, subscribe.
- `www.amazon.com.md` — search, filters, cart (never buy without confirmation).
- `www.reddit.com.md` — search, subreddit, post/comment.
- `www.instagram.com.md`, `www.facebook.com.md` — feed, search, message.
- Job ATS: `boards.greenhouse.io.md`, `jobs.lever.co.md`, `jobs.ashbyhq.com.md`,
  `*.myworkdayjobs.com.md` — apply flows, uploads, review, submit.

## Agent improvements

- **Context-stay rule** — after a panel/overlay opens (LinkedIn Message), prefer
  controls inside it; do not fall back to the global search box.
- **Replan on BLOCKED** — re-run the planner once with the failure reason.
- **Verification before DONE** — check the plan's `done_when` on the page.
- **Snapshot coverage** — iframes, in-page overlays/portals, shadow DOM, new tabs.
- **Deterministic guards** — never refill a field with its current value; never
  repeat an identical operation.
- **Per-site login notes** — record where a one-time login is expected.

## Safety

- Default to compose-only; sending/submitting requires an explicit opt-in.
- Daily caps, delays, and a kill switch for any batch mode.
