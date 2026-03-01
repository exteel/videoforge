"""
VideoForge Backend — WebSocket progress endpoint.

WS /ws/{job_id}

On connect: sends current job status + all buffered logs.
Then streams events in real-time until the job finishes or the client disconnects.

Event types pushed to clients:
  {"type": "status",     "status": "running"}
  {"type": "step_start", "step": 2, "name": "Images + Voices"}
  {"type": "step_done",  "step": 2, "elapsed": 42.1}
  {"type": "log",        "message": "..."}
  {"type": "done",       "elapsed": 142.5}
  {"type": "error",      "message": "..."}
  {"type": "cancelled"}
  {"type": "ping"}        (heartbeat every 30s if no other events)
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.job_manager import manager

router = APIRouter(tags=["websocket"])

_HEARTBEAT_INTERVAL = 25  # seconds


@router.websocket("/ws/{job_id}")
async def ws_progress(ws: WebSocket, job_id: str) -> None:
    """Stream real-time progress events for a job."""
    await ws.accept()

    job = manager.get(job_id)
    if not job:
        await ws.send_json({"type": "error", "message": f"Job not found: {job_id}"})
        await ws.close(code=4004)
        return

    # Send current state immediately on connect
    await ws.send_json({
        "type": "status",
        "status": job.status,
        "step": job.step,
        "step_name": job.step_name,
        "review_stage": job.review_stage,
    })
    for msg in list(job.logs):
        await ws.send_json({"type": "log", "message": msg})

    # If waiting for review, re-send the review_required event so late joiners see it
    if job.status == "waiting_review" and job.review_stage:
        await ws.send_json({"type": "review_required", "stage": job.review_stage, "data": {}})

    # If job already finished, close immediately
    if job.status in ("done", "failed", "cancelled"):
        await ws.send_json({"type": job.status})
        await ws.close()
        return

    # Subscribe to live events
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    job.subscribers.append(q)

    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_INTERVAL)
                await ws.send_json(event)
                # Close connection cleanly when job terminates
                if event.get("type") in ("done", "error", "cancelled"):
                    break
            except asyncio.TimeoutError:
                # Heartbeat so the connection doesn't time out
                await ws.send_json({"type": "ping"})
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        try:
            job.subscribers.remove(q)
        except ValueError:
            pass
        try:
            await ws.close()
        except Exception:
            pass
