#!/usr/bin/python3
"""Seeds a throwaway HOME with fake Claude Code transcripts, so the tray's
Search and Insight windows can be screenshotted with realistic data and zero
real sessions. Stdlib only.

Usage:
    python3 demo/seed_demo.py --run       # generate + launch the tray
    python3 demo/seed_demo.py --verify    # generate + run headless checks
    python3 demo/seed_demo.py             # generate + print launch commands

See demo/README.md for details.
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TRAY_SRC = REPO_ROOT / "tray" / "tray.py"
ICON_SRC = REPO_ROOT / "assets" / "claude-fallback.svg"

DEFAULT_BASE_DIR = os.environ.get("CLAUDE_TRAY_DEMO_DIR", "/tmp/claude-tray-demo")
ENTRYPOINT = "claude-vscode"  # anything else (e.g. "sdk-cli") is excluded from search
MODELS = ("claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5", "claude-fable-5")

# One fictional world: folder -> list of transcripts. Each transcript is a
# realistic dev conversation with distinctive keywords, so search has
# something to actually match on.
PROJECTS = {
    "webapp": [
        {
            "branch": "feat/checkout",
            "title": "Add Stripe checkout flow",
            "model": "claude-opus-4-8",
            "day_offset": 0,
            "start_hour": 9,
            "messages": [
                ("Let's wire up Stripe Checkout for the pro plan upgrade. "
                 "Users click upgrade, we create a checkout session server-side, "
                 "and redirect.",
                 "I'll add a create_checkout_session endpoint that builds a Stripe "
                 "Checkout Session with the price ID and success/cancel URLs, then "
                 "return the session URL for the redirect."),
                ("Also need to handle the webhook once payment completes.",
                 "Added a /webhooks/stripe endpoint. It verifies the Stripe webhook "
                 "signature using the raw request body and Stripe-Signature header "
                 "before processing checkout.session.completed."),
                ("How do we test the signature verification locally without a live "
                 "Stripe account?",
                 "Use the Stripe CLI: `stripe listen --forward-to "
                 "localhost:8000/webhooks/stripe` — it signs events with a local "
                 "webhook secret you set in .env."),
                ("Getting a 400 'No signatures found matching the expected "
                 "signature for payload' when I test with curl directly.",
                 "That's expected — curl won't compute the HMAC. The Stripe CLI "
                 "forwards events with a valid signature; a raw curl POST always "
                 "fails verification unless you replicate the HMAC yourself."),
                ("Ok, tested through the CLI, checkout session completes and the "
                 "webhook fires. Let's ship it.",
                 "Merged the webhook handler and checkout route. Idempotency: "
                 "recording the Stripe event id so a retried webhook delivery "
                 "doesn't double-grant the upgrade."),
            ],
        },
        {
            "branch": "main",
            "title": "Fix Stripe webhook verification",
            "model": "claude-sonnet-5",
            "day_offset": 2,
            "start_hour": 15,
            "messages": [
                ("Production webhook handler is rejecting valid events with a "
                 "signature mismatch, only in prod.",
                 "Likely the load balancer is altering the request body before it "
                 "reaches the app (re-encoding, trailing newline). Stripe's HMAC is "
                 "computed over the exact raw bytes."),
                ("Confirmed — the proxy was gzip-decompressing and re-serializing "
                 "the body. Disabled that for the webhook route.",
                 "Good catch. Also check the timestamp tolerance — Stripe rejects "
                 "signatures older than 5 minutes by default, so body rewriting "
                 "that delays delivery can trip that too."),
                ("Signature check passes now. We should log the event id on "
                 "verification failure for debugging.",
                 "Added structured logging on SignatureVerificationError including "
                 "the raw Stripe-Signature header and event id when parseable, "
                 "without logging the webhook secret."),
            ],
        },
    ],
    "api-server": [
        {
            "branch": "fix/rate-limit",
            "title": "Add cursor-based pagination to /users",
            "model": "claude-sonnet-5",
            "day_offset": 0,
            "start_hour": 10,
            "messages": [
                ("The /users list endpoint uses offset pagination and gets slow "
                 "past 50k rows. Switch it to cursor-based pagination.",
                 "I'll use the (created_at, id) tuple as the cursor, "
                 "base64-encoded in next_cursor, and switch the query to "
                 "WHERE (created_at, id) > (cursor_created_at, cursor_id) "
                 "ORDER BY created_at, id LIMIT n."),
                ("What happens to clients still passing ?offset=?",
                 "Kept offset working as a deprecated fallback for one release, "
                 "with a Deprecation response header pointing at the new cursor "
                 "param."),
                ("Also add rate limiting on this endpoint, we've seen scraping "
                 "abuse.",
                 "Added a token-bucket rate limit keyed on API key, 60 "
                 "requests/minute, returning 429 with Retry-After when exceeded."),
                ("Cursor pagination + rate limit both live, load test looks good "
                 "— p99 dropped from 800ms to 40ms on the users list.",
                 "Nice. I also added an index on (created_at, id) to back the "
                 "cursor query — without it the ORDER BY would still scan."),
            ],
        },
        {
            "branch": "main",
            "title": "Debug alembic migration duplicate column",
            "model": "claude-opus-4-8",
            "day_offset": 1,
            "start_hour": 11,
            "messages": [
                ("`alembic upgrade head` fails with 'column \"status\" already "
                 "exists' on staging but not locally.",
                 "Sounds like staging's migration history is out of sync — "
                 "someone probably ran a manual ALTER TABLE or an old migration "
                 "partially applied. Let's check alembic_version on staging."),
                ("alembic_version shows revision abc123, but the migration adding "
                 "status is def456, which comes after.",
                 "So def456 never got recorded as applied even though the column "
                 "exists — likely a migration that added the column then crashed "
                 "on a later statement before committing the version bump."),
                ("Right, there's a NOT NULL backfill after the ADD COLUMN that "
                 "timed out.",
                 "I'll split that migration: add the column nullable, backfill in "
                 "a data migration, then add the NOT NULL constraint in a third "
                 "revision, so a partial re-run can't duplicate the column."),
                ("Ran the split migrations on staging, alembic_version is now "
                 "correct and the column is populated.",
                 "Confirmed on staging. I'll also add a test that stamps a stale "
                 "alembic_version and asserts the split migrations are "
                 "idempotent."),
            ],
        },
    ],
    "dotfiles": [
        {
            "branch": "main",
            "title": "Migrate dotfiles to GNU stow",
            "model": "claude-haiku-4-5",
            "day_offset": 4,
            "start_hour": 20,
            "messages": [
                ("My dotfiles repo is a pile of symlink scripts. Let's migrate to "
                 "GNU stow.",
                 "Restructured the repo into per-package directories (zsh/, "
                 "nvim/, git/), each mirroring the target $HOME layout, so "
                 "`stow zsh nvim git` from the repo root creates the symlinks."),
                ("What about files that already exist at the target, like an "
                 "existing .zshrc?",
                 "stow refuses to overwrite by default ('existing target is "
                 "neither a link nor a directory'); back up the conflicting file "
                 "first, or use `stow --adopt` to pull it into the package."),
                ("Used --adopt for .gitconfig, worked. Add a bootstrap script for "
                 "a fresh machine.",
                 "Added bootstrap.sh: checks for GNU stow, installs it via the OS "
                 "package manager if missing, then runs `stow */` from the repo "
                 "root."),
            ],
        },
    ],
    "ml-pipeline": [
        {
            "branch": "experiment/lr-schedule",
            "title": "Tune LR schedule",
            "model": "claude-fable-5",
            "day_offset": 0,
            "start_hour": 16,
            "messages": [
                ("Training loss plateaus around epoch 30 with the current "
                 "step-decay LR schedule. Let's compare against cosine "
                 "annealing.",
                 "Added a cosine option to the scheduler config alongside step, "
                 "with a linear warmup for the first 500 steps in both cases so "
                 "the comparison isn't confounded by warmup differences."),
                ("Results: step decay final val loss 0.412, cosine 0.389. Cosine "
                 "wins but trains slightly less stable early on.",
                 "The instability is likely cosine's higher LR in the "
                 "mid-training region compared to step decay's fixed drops. Try a "
                 "slightly longer warmup (1000 steps) to smooth the early "
                 "phase."),
                ("1000-step warmup with cosine: val loss 0.381, and the loss "
                 "curve is smooth now.",
                 "Good, I'll set cosine + 1000-step warmup as the new default "
                 "and keep step decay available behind --lr-schedule step for "
                 "reproducing older runs."),
            ],
        },
    ],
    "infra": [
        {
            "branch": "main",
            "title": "Investigate terraform plan drift",
            "model": "claude-sonnet-5",
            "day_offset": 6,
            "start_hour": 9,
            "messages": [
                ("`terraform plan` wants to recreate the prod RDS instance even "
                 "though nothing in the .tf files changed.",
                 "That's usually state drift — someone likely changed the "
                 "instance via the AWS console outside Terraform. Let's diff "
                 "`terraform show` against the actual AWS config."),
                ("Found it: the parameter group was changed manually last week "
                 "for an emergency fix.",
                 "I'll import that parameter group change back into the "
                 "Terraform config so the plan matches reality, then run "
                 "`terraform plan` again to confirm no more diff before anyone "
                 "applies."),
                ("Plan is clean now. Should we lock down console access to "
                 "prevent this?",
                 "Recommend an IAM policy that denies RDS modify actions outside "
                 "the CI role, with a documented break-glass process for real "
                 "emergencies."),
            ],
        },
    ],
    "notes": [
        {
            "branch": "main",
            "title": "Debug flaky CI test",
            "model": "claude-haiku-4-5",
            "day_offset": 0,
            "start_hour": 13,
            "messages": [
                ("test_concurrent_writes fails about 1 in 20 runs on CI, always "
                 "passes locally. Flaky CI test.",
                 "Classic race condition — the test spawns two writer threads and "
                 "asserts on final state without synchronizing; CI's "
                 "slower/shared CPU changes the interleaving. Let's add an "
                 "explicit barrier before the assertion."),
                ("Added a threading.Event barrier, ran the CI job 50 times in a "
                 "loop, zero failures.",
                 "Good. I'd still avoid adding pytest-rerunfailures as a band-aid "
                 "here — the barrier fixes the actual race, retries would just "
                 "hide it again next time timing shifts."),
                ("Agreed, no retries. Noting this pattern in the testing guide.",
                 "Added a short note to the testing guide: any test asserting on "
                 "shared mutable state across threads needs an explicit "
                 "synchronization point, not a sleep()."),
            ],
        },
    ],
}


def _fake_usage(rng):
    """Cache-heavy token counts in the ballpark of real Claude Code turns."""
    return {
        "input_tokens": rng.randint(1_000, 5_000),
        "cache_read_input_tokens": rng.randint(50_000, 400_000),
        "cache_creation": {
            "ephemeral_5m_input_tokens": rng.randint(5_000, 30_000),
            "ephemeral_1h_input_tokens": rng.choice([0, 0, rng.randint(1_000, 8_000)]),
        },
        "output_tokens": rng.randint(1_000, 8_000),
    }


def _local_ts(day_offset, hour, minute, second=0):
    """Local wall-clock (today - day_offset, hour:minute) -> UTC ISO 'Z'
    string. Anchored on local time so the event lands on the intended
    *local* day regardless of the machine's timezone."""
    now_local = datetime.now().astimezone()
    target_date = now_local.date() - timedelta(days=day_offset)
    local_dt = datetime.combine(
        target_date, time(hour, minute, second), tzinfo=now_local.tzinfo
    )
    if local_dt > now_local:  # keep today's events in the past (no negative "When")
        local_dt = now_local - timedelta(minutes=1)
    utc_dt = local_dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc_dt.microsecond // 1000:03d}Z"


def _epoch_of(iso_ts):
    return datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).timestamp()


def build_events(spec, cwd, rng):
    """One transcript's worth of user/assistant events, oldest first.
    Metadata is repeated on every event since the tray only reads whichever
    ones land in its tail read window."""
    events = []
    hour, minute = spec["start_hour"], 0
    last_prompt = ""
    for user_text, assistant_text in spec["messages"]:
        last_prompt = user_text
        ts_user = _local_ts(spec["day_offset"], hour, minute, rng.randint(0, 59))
        meta = {
            "aiTitle": spec["title"],
            "gitBranch": spec["branch"],
            "lastPrompt": last_prompt,
            "cwd": cwd,
            "entrypoint": ENTRYPOINT,
        }
        events.append({
            "type": "user",
            "message": {"role": "user", "content": user_text},
            "timestamp": ts_user,
            **meta,
        })
        minute += rng.randint(0, 2)  # reply lands moments after the prompt
        ts_assistant = _local_ts(spec["day_offset"], hour, minute, rng.randint(0, 59))
        events.append({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_text}],
                "model": spec["model"],
                "stop_reason": "end_turn",
                "usage": _fake_usage(rng),
            },
            "timestamp": ts_assistant,
            **meta,
        })
        minute += rng.randint(3, 9)  # gap before the next prompt
        if minute >= 60:
            hour, minute = hour + minute // 60, minute % 60
    return events


def _encode_project_dir(cwd):
    """Mirrors Claude Code's convention of turning a cwd into a directory
    name (slashes and dots collapsed to dashes); the exact shape doesn't
    matter, the tray only globs PROJECTS/*/*.jsonl and reads cwd from
    the transcript content."""
    return cwd.replace("/", "-").replace(".", "-")


def wipe(base):
    if base.exists():
        shutil.rmtree(base)


def build_tree(base):
    home = base / "home"
    run_dir = base / "run"
    icons_dir = run_dir / "icons"
    projects_root = home / ".claude" / "projects"
    for d in (home, run_dir, icons_dir, projects_root):
        d.mkdir(parents=True, exist_ok=True)
    tray_dst = run_dir / "tray.py"
    shutil.copy2(TRAY_SRC, tray_dst)
    shutil.copy2(ICON_SRC, icons_dir / "claude.svg")
    return {
        "base": base, "home": home, "run_dir": run_dir,
        "tray": tray_dst, "projects_root": projects_root,
    }


def seed_world(paths, rng):
    """-> (n_transcripts, n_projects, today_tokens_by_model)."""
    projects_dir = paths["home"] / "projects"
    n_transcripts = 0
    today_tokens = {}

    for folder, transcripts in PROJECTS.items():
        cwd_path = projects_dir / folder
        cwd_path.mkdir(parents=True, exist_ok=True)
        cwd = str(cwd_path)
        session_dir = paths["projects_root"] / _encode_project_dir(cwd)
        session_dir.mkdir(parents=True, exist_ok=True)

        for i, spec in enumerate(transcripts):
            session_id = f"demo-{folder}-{i}-{rng.getrandbits(32):08x}"
            events = build_events(spec, cwd, rng)
            transcript_path = session_dir / f"{session_id}.jsonl"
            with transcript_path.open("w", encoding="utf-8") as fh:
                for event in events:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            last_epoch = max(_epoch_of(e["timestamp"]) for e in events)
            os.utime(transcript_path, (last_epoch, last_epoch))
            n_transcripts += 1

            if spec["day_offset"] == 0:
                for e in events:
                    if e["type"] != "assistant":
                        continue
                    usage = e["message"]["usage"]
                    cc = usage["cache_creation"]
                    total = (usage["input_tokens"] + usage["cache_read_input_tokens"]
                             + cc["ephemeral_5m_input_tokens"] + cc["ephemeral_1h_input_tokens"]
                             + usage["output_tokens"])
                    today_tokens[spec["model"]] = today_tokens.get(spec["model"], 0) + total

    return n_transcripts, len(PROJECTS), today_tokens


def _python_bin(require_gtk):
    """/usr/bin/python3 has the system PyGObject bindings tray.py needs for
    its GTK/AppIndicator path (--run). The headless argparse branches
    (--reindex/--insight/--search) exit before importing gi, so --verify can
    fall back to whatever python3 is on PATH."""
    system_python = "/usr/bin/python3"
    if os.path.exists(system_python):
        return system_python
    if require_gtk:
        print(f"warning: {system_python} not found, GTK bindings may be missing", file=sys.stderr)
    return sys.executable


def launch_commands(paths, demo_notify_variant=True):
    prefix = f"HOME={paths['home']} CLAUDE_TRAY_FAKE=1"
    cmds = [f"{prefix} /usr/bin/python3 {paths['tray']}"]
    if demo_notify_variant:
        cmds.append(f"{prefix} /usr/bin/python3 {paths['tray']} --demo-notify")
    return cmds


def print_summary(paths, n_transcripts, n_projects, today_tokens):
    total_today = sum(today_tokens.values())
    print(f"Seeded {n_transcripts} transcripts across {n_projects} projects under {paths['base']}")
    if today_tokens:
        by_model = ", ".join(f"{m.replace('claude-', '')}={t:,}" for m, t in today_tokens.items())
        print(f"Today's tokens: {total_today:,} ({by_model})")
    print("Launch:")
    for cmd in launch_commands(paths):
        print(f"  {cmd}")


def run_verify(paths):
    python_bin = _python_bin(require_gtk=False)
    env = dict(os.environ, HOME=str(paths["home"]), CLAUDE_TRAY_FAKE="1")
    checks = [["--reindex"], ["--insight"], ["--search", "pagination"]]
    for args in checks:
        label = " ".join(args)
        print(f"\n=== {label} ===")
        result = subprocess.run(
            [python_bin, str(paths["tray"]), *args],
            env=env, capture_output=True, text=True,
        )
        print(result.stdout, end="")
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr, end="")


def run_tray(paths, demo_notify):
    python_bin = _python_bin(require_gtk=True)
    env = dict(os.environ, HOME=str(paths["home"]), CLAUDE_TRAY_FAKE="1")
    cmd = [python_bin, str(paths["tray"])]
    if demo_notify:
        cmd.append("--demo-notify")
    os.execvpe(cmd[0], cmd, env)  # replaces this process


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=DEFAULT_BASE_DIR,
                         help="scratch base directory (default: %(default)s)")
    parser.add_argument("--clean", action="store_true",
                         help="wipe the scratch directory and exit, no regeneration")
    parser.add_argument("--run", action="store_true",
                         help="exec tray.py against the seeded HOME (replaces this process)")
    parser.add_argument("--demo-notify", action="store_true",
                         help="with --run, also pass --demo-notify to tray.py")
    parser.add_argument("--verify", action="store_true",
                         help="run headless checks (--reindex/--insight/--search) and print their output")
    parser.add_argument("--seed", type=int, default=1234,
                         help="RNG seed, for reproducible fake data (default: %(default)s)")
    args = parser.parse_args()

    base = Path(args.dir).resolve()
    if args.clean:
        wipe(base)
        print(f"Wiped {base}")
        return

    wipe(base)
    paths = build_tree(base)
    n_transcripts, n_projects, today_tokens = seed_world(paths, random.Random(args.seed))
    print_summary(paths, n_transcripts, n_projects, today_tokens)

    if args.verify:
        run_verify(paths)
    elif args.run:
        run_tray(paths, args.demo_notify)


if __name__ == "__main__":
    main()
