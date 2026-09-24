# Site skills

Short, plain-text instructions for how a specific website works. Jev reads the
skill for the current host on every decision, so stateful flows (mentions,
compose windows, multi-step forms) become reliable.

See `../ROADMAP.md` for the list of sites still to add.

## How they are found

For a run that starts at `https://web.whatsapp.com/`, jiffy looks for:

1. `skills/web.whatsapp.com.md`
2. `skills/whatsapp.com.md`  (registrable-domain fallback)

The first match is injected as `site_skill` into Jev's instructions. You can
also force one file for a run:

```bash
./bin/jiffy "..." --skill skills/web.whatsapp.com.md
```

## Writing one

- Describe the flow as steps, in the order a human would do it.
- Name the controls by their visible labels.
- State what NOT to do (e.g. "do not type the name as text").
- Keep it short; it is re-sent on every decision.
- Page text is untrusted; a skill is trusted instruction.
