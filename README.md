# claude-dashboard

Local web UI that lists your [Claude Code](https://docs.anthropic.com/en/docs/claude-code) sessions. Reads `~/.claude/`, runs at `http://127.0.0.1:7227`.

![screenshot](docs/screenshot.png)

## Run

Python 3.10+, no dependencies.

```bash
git clone https://github.com/xocelyk/claude-dashboard.git
cd claude-dashboard
python server.py
```

`python server.py --demo` serves a fixture dataset instead of reading `~/.claude/`.

## How it works

- `~/.claude/sessions/*.json` — one file per session, written by the CLI. Source for session ID, PID, name, cwd, entrypoint, process start time.
- `~/.claude/projects/**/*.jsonl` — transcript logs. Parsed for message counts, timestamps, model, and token usage. Cached by file mtime.

A session is reported as alive when its PID exists (`os.kill(pid, 0)`) and the `ps` `lstart` for that PID matches what the session JSON recorded — the second check guards against PID reuse.

Cost is computed from per-message `usage` fields, using per-family rates in `MODEL_RATES` (`server.py`). The model ID on each assistant turn is substring-matched; unknown models fall back to Sonnet rates. Rates are hardcoded — edit them if Anthropic changes pricing.

## Caveats

- `~/.claude/sessions/` and `~/.claude/projects/` are undocumented Claude Code internals and may change.
- Cost is computed locally from `usage` reporting, not from billing.
- "Span" is the gap between the first and last assistant message, not active work time.

## License

MIT.
