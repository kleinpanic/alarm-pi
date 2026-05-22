#!/usr/bin/env python3
"""
Alarm Daemon Entry Point

Runs the alarm clock as a background daemon process.
Monitors configured alarms and triggers notifications.

Usage:
    python alarm_daemon.py
    
Or with systemd:
    systemctl --user start alarm-daemon
"""

import sys
import logging
import argparse
from pathlib import Path

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from alarm.daemon_core import run_daemon
from alarm.config import ensure_config_dir, load_settings, load_alarms


def setup_logging(verbose: bool = False, log_file: str = None) -> None:
    """Configure logging for the daemon."""
    level = logging.DEBUG if verbose else logging.INFO
    
    handlers = [logging.StreamHandler(sys.stdout)]
    
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers
    )


def main():
    parser = argparse.ArgumentParser(
        description="Alarm Clock Daemon for Raspberry Pi",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python alarm_daemon.py              # Run daemon
    python alarm_daemon.py -v           # Run with verbose logging
    python alarm_daemon.py --log alarm.log  # Log to file
        """
    )
    
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose/debug logging"
    )
    
    parser.add_argument(
        "--log",
        metavar="FILE",
        help="Log to file in addition to stdout"
    )
    
    args = parser.parse_args()
    
    # Setup logging
    setup_logging(verbose=args.verbose, log_file=args.log)
    logger = logging.getLogger(__name__)
    
    # Ensure config exists
    ensure_config_dir()
    
    # Quick sanity check
    settings = load_settings()
    alarms = load_alarms()
    
    logger.info(f"Starting with {len(alarms)} configured alarm(s)")
    logger.info(f"Audio player: {settings.audio_player}")
    logger.info(f"Default sound: {settings.default_sound}")
    
    # Run the daemon
    try:
        run_daemon()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
