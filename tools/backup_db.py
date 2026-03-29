"""
VideoForge — SQLite database backup.

Creates timestamped backup copies of videoforge.db.
Keeps last N backups (default 10). Safe to run while backend is running
(uses SQLite online backup API).

Usage:
    python tools/backup_db.py                    # backup to data/backups/
    python tools/backup_db.py --keep 5           # keep only last 5
    python tools/backup_db.py --dest /path/to    # custom destination
"""
import argparse
import logging
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "videoforge.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] backup — %(message)s",
)
log = logging.getLogger("backup")


def backup_sqlite(src: Path, dest: Path) -> None:
    """Create a consistent backup using SQLite online backup API."""
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dst_conn)
        log.info("Backup created: %s (%.1f KB)", dest.name, dest.stat().st_size / 1024)
    finally:
        dst_conn.close()
        src_conn.close()


def cleanup_old_backups(backup_dir: Path, keep: int) -> None:
    """Remove oldest backups, keeping only the N most recent."""
    backups = sorted(backup_dir.glob("videoforge_*.db"), key=lambda p: p.stat().st_mtime)
    to_remove = backups[:-keep] if len(backups) > keep else []
    for old in to_remove:
        old.unlink()
        log.info("Removed old backup: %s", old.name)


def main() -> None:
    parser = argparse.ArgumentParser(description="VideoForge DB backup")
    parser.add_argument("--dest", type=Path, default=DEFAULT_BACKUP_DIR, help="Backup directory")
    parser.add_argument("--keep", type=int, default=10, help="Number of backups to keep")
    args = parser.parse_args()

    if not DB_PATH.exists():
        log.error("Database not found: %s", DB_PATH)
        sys.exit(1)

    args.dest.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = args.dest / f"videoforge_{timestamp}.db"

    t0 = time.monotonic()
    backup_sqlite(DB_PATH, backup_path)
    elapsed = time.monotonic() - t0

    cleanup_old_backups(args.dest, keep=args.keep)

    total = len(list(args.dest.glob("videoforge_*.db")))
    log.info("Done in %.1fs — %d backup(s) in %s", elapsed, total, args.dest)


if __name__ == "__main__":
    main()
