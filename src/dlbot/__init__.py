"""dlbot — Discord video download bot (yt-dlp + SQLite queue).

Commands:
    uv run dlbot            run worker (subprocess) + bot together
    uv run dlbot bot        run only the Discord bot
    uv run dlbot worker     run only the queue worker
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from .config import load_settings


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dlbot",
        description="Discord bot that queues YouTube/Instagram downloads via yt-dlp.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("bot", help="run only the Discord bot")
    subparsers.add_parser("worker", help="run only the queue worker")
    return parser.parse_args(argv)


def _spawn_worker() -> subprocess.Popen:
    """Start the worker as a separate process using the same interpreter."""
    env = os.environ.copy()
    return subprocess.Popen(
        [sys.executable, "-m", "dlbot", "worker"],
        env=env,
    )


def run_command() -> int:
    """Run worker + bot; the worker is a child process terminated on exit."""
    from .bot import run_bot

    worker = _spawn_worker()
    print(f"[dlbot] worker started as pid {worker.pid}", file=sys.stderr, flush=True)
    try:
        return run_bot(load_settings())
    finally:
        print("[dlbot] stopping worker...", file=sys.stderr, flush=True)
        worker.terminate()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print("[dlbot] worker did not stop, killing it", file=sys.stderr, flush=True)
            worker.kill()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "worker":
        from .worker import run_worker

        return run_worker(load_settings())
    if args.command == "bot":
        from .bot import run_bot

        return run_bot(load_settings())
    return run_command()


if __name__ == "__main__":
    raise SystemExit(main())
