"""
VideoForge -- SQLite Tracker (Task #19).

Tracks pipeline runs: statuses, cost per model, YouTube URLs, Transcriber source.

Schema:
  videos -- one row per video pipeline run
  costs  -- one row per API call / billable step

CLI:
    python utils/db.py --list                  # list recent videos
    python utils/db.py --video-id 3            # detailed view with cost breakdown
    python utils/db.py --stats                 # aggregate stats by channel/preset
    python utils/db.py --channel history.json  # filter by channel

    # Custom DB path (default: data/videoforge.db)
    python utils/db.py --db /path/to.db --list
"""

import argparse
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

DEFAULT_DB_PATH = ROOT / "data" / "videoforge.db"

# ─── Video statuses ───────────────────────────────────────────────────────────

STATUS_PENDING  = "pending"
STATUS_RUNNING  = "running"
STATUS_DONE     = "done"
STATUS_FAILED   = "failed"
STATUS_SKIPPED  = "skipped"

ALL_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED)

# ─── SQL definitions ──────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS videos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_dir       TEXT NOT NULL,
    source_title     TEXT,
    channel          TEXT NOT NULL,
    quality_preset   TEXT NOT NULL DEFAULT 'max',
    template         TEXT DEFAULT 'auto',
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    elapsed_seconds  REAL,
    from_step        INTEGER DEFAULT 1,
    project_dir      TEXT,
    script_path      TEXT,
    video_path       TEXT,
    thumbnail_path   TEXT,
    error_message    TEXT,
    youtube_url      TEXT,
    youtube_video_id TEXT
);

CREATE TABLE IF NOT EXISTS costs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id     INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    step         TEXT NOT NULL,
    model        TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    units        REAL DEFAULT 0.0,
    unit_label   TEXT DEFAULT '',
    cost_usd     REAL NOT NULL DEFAULT 0.0,
    recorded_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_videos_status     ON videos (status);
CREATE INDEX IF NOT EXISTS idx_videos_channel    ON videos (channel);
CREATE INDEX IF NOT EXISTS idx_videos_created_at ON videos (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_costs_video_id    ON costs (video_id);
CREATE INDEX IF NOT EXISTS idx_costs_model       ON costs (model);
CREATE INDEX IF NOT EXISTS idx_costs_vid_step    ON costs (video_id, step);

CREATE TABLE IF NOT EXISTS transcription_cache (
    video_id   TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    output_dir TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _now() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt_dur(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# ─── VideoTracker ─────────────────────────────────────────────────────────────

class VideoTracker:
    """
    SQLite-backed tracker for VideoForge pipeline runs.

    Usage:
        tracker = VideoTracker()

        # Create a record when pipeline starts
        vid_id = tracker.create_video(
            source_dir="D:/output/Rome Fall",
            channel="history",
            quality_preset="max",
        )

        # Update status
        tracker.set_running(vid_id)

        # Record an API cost
        tracker.record_cost(vid_id, step="Script", model="claude-opus-4-6",
                            input_tokens=2500, output_tokens=3000, cost_usd=0.2625)

        # Mark done
        tracker.set_done(vid_id,
                         video_path="projects/Rome Fall/output/final.mp4",
                         elapsed_seconds=142.3)
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        """Yield a context-managed SQLite connection with row_factory."""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        with self._conn() as conn:
            conn.executescript(_DDL)

    # ── Create ────────────────────────────────────────────────────────────────

    def create_video(
        self,
        source_dir: str | Path,
        channel: str,
        *,
        quality_preset: str = "max",
        template: str = "auto",
        from_step: int = 1,
        source_title: str | None = None,
        project_dir: str | Path | None = None,
    ) -> int:
        """
        Insert a new video record with status=pending.

        Returns:
            The new video row id.
        """
        now = _now()
        # Try to read title from source_dir/title.txt if not provided
        if source_title is None:
            title_file = Path(source_dir) / "title.txt"
            if title_file.exists():
                try:
                    source_title = title_file.read_text(encoding="utf-8").strip()[:200]
                except Exception:
                    pass

        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO videos
                  (source_dir, source_title, channel, quality_preset, template,
                   status, created_at, updated_at, from_step, project_dir)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(source_dir),
                    source_title,
                    str(channel),
                    quality_preset,
                    template,
                    STATUS_PENDING,
                    now,
                    now,
                    from_step,
                    str(project_dir) if project_dir else None,
                ),
            )
            return cur.lastrowid  # type: ignore[return-value]

    # ── Status updates ────────────────────────────────────────────────────────

    def set_running(self, video_id: int) -> None:
        """Mark video as running (sets started_at)."""
        now = _now()
        with self._conn() as conn:
            conn.execute(
                "UPDATE videos SET status=?, started_at=?, updated_at=? WHERE id=?",
                (STATUS_RUNNING, now, now, video_id),
            )

    def set_done(
        self,
        video_id: int,
        *,
        video_path: str | Path | None = None,
        thumbnail_path: str | Path | None = None,
        script_path: str | Path | None = None,
        elapsed_seconds: float | None = None,
    ) -> None:
        """Mark video as done."""
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE videos SET
                    status=?, finished_at=?, updated_at=?,
                    video_path=?, thumbnail_path=?, script_path=?,
                    elapsed_seconds=?
                WHERE id=?
                """,
                (
                    STATUS_DONE, now, now,
                    str(video_path) if video_path else None,
                    str(thumbnail_path) if thumbnail_path else None,
                    str(script_path) if script_path else None,
                    elapsed_seconds,
                    video_id,
                ),
            )

    def set_failed(self, video_id: int, error: str, elapsed_seconds: float | None = None) -> None:
        """Mark video as failed with error message."""
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE videos SET
                    status=?, finished_at=?, updated_at=?,
                    error_message=?, elapsed_seconds=?
                WHERE id=?
                """,
                (STATUS_FAILED, now, now, error[:1000], elapsed_seconds, video_id),
            )

    def set_skipped(self, video_id: int, reason: str = "already done") -> None:
        """Mark video as skipped."""
        now = _now()
        with self._conn() as conn:
            conn.execute(
                "UPDATE videos SET status=?, updated_at=?, error_message=? WHERE id=?",
                (STATUS_SKIPPED, now, reason, video_id),
            )

    def cancel_orphaned_jobs(self) -> int:
        """On backend startup, mark any 'running'/'waiting_review'/'queued' rows as
        cancelled — they are orphaned because the previous backend process was killed
        while they were in-flight and the in-memory job dict is now empty.
        Returns the number of rows affected.
        """
        now = _now()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE videos
                SET status='cancelled', updated_at=?,
                    error_message='Cancelled: backend restarted while job was in-flight'
                WHERE status IN ('running', 'waiting_review', 'queued')
                """,
                (now,),
            )
            return cur.rowcount

    def set_youtube_url(
        self,
        video_id: int,
        youtube_url: str,
        youtube_video_id: str = "",
    ) -> None:
        """Record YouTube upload URL and video ID."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE videos SET youtube_url=?, youtube_video_id=?, updated_at=? WHERE id=?",
                (youtube_url, youtube_video_id, _now(), video_id),
            )

    # ── Cost recording ────────────────────────────────────────────────────────

    def record_cost(
        self,
        video_id: int,
        step: str,
        model: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        units: float = 0.0,
        unit_label: str = "",
        cost_usd: float,
    ) -> None:
        """Record a single API call cost entry."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO costs
                  (video_id, step, model, input_tokens, output_tokens,
                   units, unit_label, cost_usd, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    video_id, step, model,
                    input_tokens, output_tokens,
                    units, unit_label,
                    cost_usd, _now(),
                ),
            )

    def record_costs_from_tracker(self, video_id: int, tracker: Any) -> None:
        """
        Bulk-record all entries from a CostTracker (utils/cost_tracker.py).

        Args:
            video_id: Target video row id.
            tracker: CostTracker instance with .entries list.
        """
        for entry in getattr(tracker, "entries", []):
            self.record_cost(
                video_id,
                step=entry.module,
                model=entry.model,
                input_tokens=entry.input_tokens,
                output_tokens=entry.output_tokens,
                units=entry.units,
                unit_label=entry.unit_label,
                cost_usd=entry.cost,
            )

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_video(self, video_id: int) -> dict[str, Any] | None:
        """Return a video row as a dict, or None if not found."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        return dict(row) if row else None

    def list_videos(
        self,
        *,
        channel: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List video records, newest first."""
        clauses: list[str] = []
        params: list[Any] = []
        if channel:
            clauses.append("channel LIKE ?")
            params.append(f"%{channel}%")
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM videos {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def get_costs(self, video_id: int) -> list[dict[str, Any]]:
        """Return all cost entries for a video."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM costs WHERE video_id=? ORDER BY id",
                (video_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def video_total_cost(self, video_id: int) -> float:
        """Return total cost in USD for a video."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT SUM(cost_usd) FROM costs WHERE video_id=?",
                (video_id,),
            ).fetchone()
        return float(row[0] or 0.0)

    def session_stats(self) -> dict[str, Any]:
        """Return aggregate stats across all videos."""
        with self._conn() as conn:
            totals = conn.execute(
                """
                SELECT
                    COUNT(*)                                    AS total_videos,
                    SUM(CASE WHEN status='done'   THEN 1 END)  AS done,
                    SUM(CASE WHEN status='failed' THEN 1 END)  AS failed,
                    SUM(CASE WHEN status='running'THEN 1 END)  AS running,
                    AVG(CASE WHEN status='done' THEN elapsed_seconds END) AS avg_elapsed
                FROM videos
                """
            ).fetchone()

            cost_total = conn.execute("SELECT SUM(cost_usd) FROM costs").fetchone()[0]

            by_model = conn.execute(
                """
                SELECT model, SUM(cost_usd) AS total, COUNT(*) AS calls
                FROM costs GROUP BY model ORDER BY total DESC
                """
            ).fetchall()

            by_preset = conn.execute(
                """
                SELECT quality_preset, COUNT(*) AS total,
                       SUM(CASE WHEN status='done' THEN 1 END) AS done
                FROM videos GROUP BY quality_preset ORDER BY total DESC
                """
            ).fetchall()

        return {
            "total_videos": totals[0] or 0,
            "done":         totals[1] or 0,
            "failed":       totals[2] or 0,
            "running":      totals[3] or 0,
            "avg_elapsed":  totals[4],
            "cost_total_usd": float(cost_total or 0.0),
            "by_model": [dict(r) for r in by_model],
            "by_preset": [dict(r) for r in by_preset],
        }


    # ── Transcription cache ───────────────────────────────────────────────────

    def get_cached_transcription(self, video_id: str) -> str | None:
        """Return cached output_dir for a video_id, or None if not cached or dir doesn't exist."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT output_dir FROM transcription_cache WHERE video_id = ?", (video_id,)
            ).fetchone()
        if row and Path(row[0]).is_dir():
            return row[0]
        return None

    def cache_transcription(self, video_id: str, url: str, title: str, output_dir: str) -> None:
        """Cache a transcription result. Upsert by video_id."""
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO transcription_cache (video_id, url, title, output_dir) VALUES (?, ?, ?, ?)",
                (video_id, url, title, str(output_dir)),
            )


# ─── CLI display helpers ───────────────────────────────────────────────────────

_STATUS_ICON = {
    STATUS_PENDING:  "·",
    STATUS_RUNNING:  "~",
    STATUS_DONE:     "✓",
    STATUS_FAILED:   "✗",
    STATUS_SKIPPED:  "-",
}


def _print_video_list(videos: list[dict[str, Any]]) -> None:
    if not videos:
        print("  (no videos)")
        return
    header = f"  {'ID':>4}  {'Status':<8}  {'Preset':<8}  {'Elapsed':>7}  {'Title / Source'}"
    print(header)
    print("  " + "-" * 72)
    for v in videos:
        icon   = _STATUS_ICON.get(v["status"], "?")
        title  = (v.get("source_title") or Path(v["source_dir"]).name)[:40]
        elapsed = _fmt_dur(v.get("elapsed_seconds"))
        print(
            f"  {v['id']:>4}  {icon} {v['status']:<7}  "
            f"{v['quality_preset']:<8}  {elapsed:>7}  {title}"
        )


def _print_video_detail(tracker: VideoTracker, video_id: int) -> None:
    v = tracker.get_video(video_id)
    if not v:
        print(f"  Video {video_id} not found.")
        return

    costs = tracker.get_costs(video_id)
    total = sum(c["cost_usd"] for c in costs)

    print()
    print("=" * 70)
    print(f"  Video #{video_id} — {_STATUS_ICON.get(v['status'], '?')} {v['status'].upper()}")
    print("=" * 70)
    print(f"  Source   : {v['source_dir']}")
    if v.get("source_title"):
        print(f"  Title    : {v['source_title']}")
    print(f"  Channel  : {v['channel']}")
    print(f"  Preset   : {v['quality_preset']}")
    print(f"  Template : {v.get('template', 'auto')}")
    print(f"  Created  : {v['created_at']}")
    if v.get("started_at"):
        print(f"  Started  : {v['started_at']}")
    if v.get("finished_at"):
        print(f"  Finished : {v['finished_at']}")
        print(f"  Elapsed  : {_fmt_dur(v.get('elapsed_seconds'))}")
    if v.get("video_path"):
        print(f"  Video    : {v['video_path']}")
    if v.get("youtube_url"):
        print(f"  YouTube  : {v['youtube_url']}")
    if v.get("error_message"):
        print(f"  Error    : {v['error_message']}")
    print()

    if costs:
        print(f"  {'Step':<20} {'Model':<26} {'In':>6} {'Out':>6}  {'Cost':>8}")
        print("  " + "-" * 72)
        for c in costs:
            units = f"{c['units']:.0f} {c['unit_label']}" if c["unit_label"] else ""
            in_t  = str(c["input_tokens"])  if c["input_tokens"]  else units or "-"
            out_t = str(c["output_tokens"]) if c["output_tokens"] else "-"
            print(
                f"  {c['step']:<20} {c['model']:<26} "
                f"{in_t:>6} {out_t:>6}  ${c['cost_usd']:>7.4f}"
            )
        print("  " + "-" * 72)
        print(f"  {'TOTAL':<48}  ${total:>7.4f}")
    else:
        print("  (no cost entries recorded)")
    print()


def _print_stats(stats: dict[str, Any]) -> None:
    print()
    print("=" * 70)
    print("  VideoForge — Session Stats")
    print("=" * 70)
    print(f"  Total videos : {stats['total_videos']}")
    print(f"  Done         : {stats['done']}")
    print(f"  Failed       : {stats['failed']}")
    print(f"  Running      : {stats['running']}")
    if stats["avg_elapsed"]:
        print(f"  Avg elapsed  : {_fmt_dur(stats['avg_elapsed'])}")
    print(f"  Total cost   : ${stats['cost_total_usd']:.4f}")

    if stats["by_model"]:
        print()
        print(f"  {'Model':<30} {'Calls':>6}  {'Cost':>9}")
        print("  " + "-" * 50)
        for m in stats["by_model"]:
            print(f"  {m['model']:<30} {m['calls']:>6}  ${m['total']:>8.4f}")

    if stats["by_preset"]:
        print()
        print(f"  {'Preset':<12} {'Total':>6}  {'Done':>6}")
        print("  " + "-" * 28)
        for p in stats["by_preset"]:
            done = p["done"] or 0
            print(f"  {p['quality_preset']:<12} {p['total']:>6}  {done:>6}")
    print()


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="db",
        description="VideoForge SQLite Tracker -- view pipeline run history",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--db", metavar="PATH", help=f"DB path (default: {DEFAULT_DB_PATH})")
    parser.add_argument("--list",     action="store_true", help="List recent video runs")
    parser.add_argument("--video-id", type=int, metavar="N", help="Show detail for video ID N")
    parser.add_argument("--stats",    action="store_true", help="Show aggregate stats")
    parser.add_argument("--channel",  metavar="NAME", help="Filter by channel name")
    parser.add_argument("--status",   choices=list(ALL_STATUSES), help="Filter by status")
    parser.add_argument("--limit",    type=int, default=20, help="Max rows for --list (default: 20)")

    args = parser.parse_args()

    tracker = VideoTracker(db_path=args.db)

    if args.video_id:
        _print_video_detail(tracker, args.video_id)
    elif args.stats:
        _print_stats(tracker.session_stats())
    else:
        # Default: list
        videos = tracker.list_videos(
            channel=args.channel,
            status=args.status,
            limit=args.limit,
        )
        print()
        print(f"  VideoForge DB: {tracker.db_path}")
        print()
        _print_video_list(videos)
        print()


if __name__ == "__main__":
    main()
