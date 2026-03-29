"""VideoForge — Prometheus-compatible metrics endpoint.

Exposes key operational metrics in Prometheus text exposition format.
No external dependencies — generates plain text directly.
"""
from __future__ import annotations

import time

from fastapi import APIRouter

router = APIRouter(tags=["metrics"])

# ─── Counters (persisted in memory for session lifetime) ─────────────────────

_counters: dict[str, int] = {
    "jobs_total": 0,
    "jobs_completed": 0,
    "jobs_failed": 0,
    "scripts_generated": 0,
    "images_generated": 0,
    "voice_blocks_generated": 0,
    "videos_compiled": 0,
}

_startup_time = time.monotonic()


def inc(name: str, value: int = 1) -> None:
    """Increment a counter. Called from job_manager/pipeline."""
    _counters[name] = _counters.get(name, 0) + value


from fastapi.responses import PlainTextResponse

@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    """Return metrics in Prometheus text exposition format."""
    from backend.job_manager import manager

    # Compute live gauges
    jobs = list(manager._jobs.values())
    running = sum(1 for j in jobs if j.status == "running")
    queued = sum(1 for j in jobs if j.status == "queued")
    waiting = sum(1 for j in jobs if j.status == "waiting_review")
    uptime = time.monotonic() - _startup_time

    lines = [
        "# HELP videoforge_uptime_seconds Time since backend started",
        "# TYPE videoforge_uptime_seconds gauge",
        f"videoforge_uptime_seconds {uptime:.1f}",
        "",
        "# HELP videoforge_jobs_running Current running jobs",
        "# TYPE videoforge_jobs_running gauge",
        f"videoforge_jobs_running {running}",
        "",
        "# HELP videoforge_jobs_queued Current queued jobs",
        "# TYPE videoforge_jobs_queued gauge",
        f"videoforge_jobs_queued {queued}",
        "",
        "# HELP videoforge_jobs_waiting_review Jobs waiting for review",
        "# TYPE videoforge_jobs_waiting_review gauge",
        f"videoforge_jobs_waiting_review {waiting}",
        "",
    ]

    # Counters
    for name, value in _counters.items():
        lines.extend([
            f"# HELP videoforge_{name} Counter",
            f"# TYPE videoforge_{name} counter",
            f"videoforge_{name} {value}",
            "",
        ])

    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
