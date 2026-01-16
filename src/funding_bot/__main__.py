"""
Entry point for running funding_bot as a module.

Usage:
    python -m funding_bot [command] [options]

Commands:
    run         Start the bot (default)
    doctor      Run preflight checks
    reconcile   Reconcile DB with exchange positions
    close-all   Emergency close all positions

Options:
    --env ENV           Environment (development/production)
    --mode MODE         Override mode (paper/live)
    --confirm-live      Confirm live trading (required for live mode)
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def main() -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Funding Arbitrage Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "doctor", "reconcile", "close-all"],
        help="Command to execute (default: run)",
    )
    parser.add_argument(
        "--env",
        default="production",
        help="Environment (development/production)",
    )
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default=None,
        help="Override trading mode",
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Confirm live trading mode",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Alias for --mode paper",
    )
    parser.add_argument(
        "--live-trading",
        action="store_true",
        help="Alias for --mode live --confirm-live",
    )

    args = parser.parse_args()

    # Handle aliases
    mode = args.mode
    confirm_live = args.confirm_live

    if args.dry_run:
        mode = "paper"
    if args.live_trading:
        mode = "live"
        confirm_live = True

    # Import here to avoid slow startup for --help
    from funding_bot.app.run import (
        run_bot,
        run_close_all,
        run_doctor,
        run_reconcile,
    )

    try:
        if args.command == "run":
            return asyncio.run(run_bot(
                env=args.env,
                mode_override=mode,
                confirm_live=confirm_live,
            ))
        elif args.command == "doctor":
            return asyncio.run(run_doctor())
        elif args.command == "reconcile":
            return asyncio.run(run_reconcile())
        elif args.command == "close-all":
            return asyncio.run(run_close_all())
        else:
            parser.print_help()
            return 1
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        return 130
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
