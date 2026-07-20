# Demo harness

Seeds a throwaway `$HOME` with fake Claude Code transcripts, so the tray's Search and Insight windows can be screenshotted with realistic data — no real sessions, no real prompts, nothing to redact from a screenshot.

## Usage

```bash
python3 demo/seed_demo.py --run
```

One command: generates the fake world, then execs `tray.py` with `$HOME` pointed at the scratch tree. The real tray code runs unmodified — Search and Insight read from the seeded transcripts because `$HOME` is fake, not because anything in the tray was patched for demo mode.

Without `--run` or `--verify`, the script just generates the data and prints the launch commands:

```bash
python3 demo/seed_demo.py
```

## What gets seeded

- A scratch tree under `/tmp/claude-tray-demo` (override with `--dir` or `$CLAUDE_TRAY_DEMO_DIR`), wiped and rebuilt on every run.
- Six fake projects (`webapp`, `api-server`, `dotfiles`, `ml-pipeline`, `infra`, `notes`), each a real directory on disk, spread across several git branches.
- Eight fake transcripts, each a multi-turn dev conversation with distinctive keywords (Stripe webhook signature, cursor-based pagination, alembic migration, GNU stow, cosine vs step decay, terraform plan drift, flaky CI) for Search to match against.
- Token usage across 4 models, spread over today and the past week, so Insight's "Today" and "Last 7 days" views are populated.
- A copy of `tray/tray.py` and the neutral `assets/claude-fallback.svg` icon, run from the scratch tree so `history.db` never touches the real one.

## Flags

- `--demo-notify`: with `--run`, also passes `--demo-notify` to `tray.py` (fires one sample "ready" desktop notification, for that screenshot).
- `--verify`: instead of launching the GTK tray, runs the headless checks (`--reindex`, `--insight`, `--search pagination`) against the seeded data and prints their output. Useful with no display.
- `--seed N`: RNG seed for the fake token counts (default is fixed, so output is reproducible run to run).

## Cleanup

```bash
rm -rf /tmp/claude-tray-demo
```
