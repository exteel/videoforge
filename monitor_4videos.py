"""
Monitor 4-video generation run.
- Auto-approves script + image review stages
- Starts batch-2 when batch-1 finishes
- Reports timing / credit usage at the end
"""
import sys, io, os, time, json, requests
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

BASE = "http://localhost:8000"
T0   = time.time()

# ── Pre-loaded from channel config ────────────────────────────────────────────
with open("D:/Project video generator/config/channels/history.json") as f:
    _cfg = json.load(f)
IMAGE_STYLE = _cfg["image_style"]

OUTPUT_BASE = r"D:\transscript batch\output\output"


def ts():
    return datetime.now().strftime('%H:%M:%S')

def mins():
    return (time.time() - T0) / 60

def get_job(jid):
    try:
        r = requests.get(f"{BASE}/api/jobs/{jid}", timeout=60)
        return r.json()
    except Exception as e:
        return {"_err": str(e)}

def approve(jid, stage):
    try:
        r = requests.post(f"{BASE}/api/jobs/{jid}/approve?stage={stage}", timeout=30)
        print(f"[{ts()} +{mins():.1f}m] AUTO-APPROVED {jid[:8]} stage={stage} → {r.status_code}", flush=True)
        return r.status_code == 200
    except Exception as e:
        print(f"[{ts()}] Approve error: {e}", flush=True)
        return False

def credits_now():
    try:
        os.chdir("D:/Project video generator")
        from dotenv import load_dotenv
        load_dotenv("D:/Project video generator/.env")
        key = os.environ.get("VOIDAI_API_KEY", "")
        r = requests.get("https://api.voidai.app/v1/credits",
                         headers={"Authorization": f"Bearer {key}"}, timeout=15)
        return r.json().get("credits", {}).get("remaining", 0)
    except:
        return -1

def voiceapi_balance():
    try:
        import asyncio
        sys.path.insert(0, "D:/Project video generator")
        os.chdir("D:/Project video generator")
        from clients.voiceapi_client import VoiceAPIClient
        async def _get():
            async with VoiceAPIClient() as c:
                return await c.get_balance()
        b = asyncio.run(_get())
        return b.get("balance", 0)
    except:
        return -1


COMMON_PARAMS = {
    "background_music": False,
    "no_ken_burns":     True,
    "burn_subtitles":   True,
    "duration_min":     25,
    "duration_max":     33,
    "image_backend":    "betatest",
    "quality":          "max",
    "skip_thumbnail":   False,
    "image_style":      IMAGE_STYLE,
}


def find_dir(keywords: list[str]) -> str:
    for d in os.listdir(OUTPUT_BASE):
        dl = d.lower()
        if all(k in dl for k in keywords):
            return os.path.join(OUTPUT_BASE, d)
    # fallback: any keyword
    for d in os.listdir(OUTPUT_BASE):
        dl = d.lower()
        if any(k in dl for k in keywords):
            return os.path.join(OUTPUT_BASE, d)
    return ""


def start_batch2() -> dict:
    print(f"\n[{ts()} +{mins():.1f}m] === STARTING BATCH 2 ===", flush=True)
    b2_specs = [
        (["past", "stuck"],   "Living in the Past"),
        (["anxiety"],         "Anxiety"),
    ]
    jobs = {}
    for keywords, label in b2_specs:
        path = find_dir(keywords)
        if not path:
            print(f"  {label}: DIR NOT FOUND for keywords={keywords}", flush=True)
            continue
        try:
            r = requests.post(f"{BASE}/api/pipeline/run",
                json={**COMMON_PARAMS, "source_dir": path}, timeout=30)
            d = r.json()
            jid = d.get("job_id", "?")
            jobs[jid] = label
            print(f"  {label}: job_id={jid} status={d.get('status','?')}", flush=True)
        except Exception as e:
            print(f"  {label}: ERR {e}", flush=True)
    return jobs


def monitor(batch_jobs: dict, batch_name: str) -> dict:
    """Monitor {job_id: label}. Auto-approve reviews. Return results dict."""
    done = {}
    prev = {}
    approved: dict[str, set] = {}

    print(f"\n[{ts()}] Monitoring {batch_name}: {list(batch_jobs.values())}", flush=True)

    while len(done) < len(batch_jobs):
        time.sleep(45)
        elapsed_m = mins()

        for jid, label in batch_jobs.items():
            if jid in done:
                continue

            d = get_job(jid)
            if "_err" in d:
                print(f"[{ts()} +{elapsed_m:.1f}m] {label}: ERR {d['_err'][:60]}", flush=True)
                continue

            status = d.get("status", "")
            step   = d.get("step", 0)
            sname  = d.get("step_name", "")
            pct    = d.get("pct", 0)
            stage  = d.get("review_stage") or ""
            logs   = d.get("logs") or []

            key = f"{status}:{step}:{int(pct // 5)}"
            if key != prev.get(jid):
                prev[jid] = key
                print(f"[{ts()} +{elapsed_m:.1f}m] {label}: {status} step={step} {sname} {pct:.0f}%", flush=True)
                if logs:
                    print(f"  > {logs[-1]}", flush=True)

            # Auto-approve any waiting review
            if status == "waiting_review" and stage:
                if stage not in approved.get(jid, set()):
                    print(f"[{ts()} +{elapsed_m:.1f}m] {label}: AUTO-APPROVING stage={stage}", flush=True)
                    if approve(jid, stage):
                        approved.setdefault(jid, set()).add(stage)

            if status in ("done", "failed", "cancelled"):
                elapsed_job = d.get("elapsed") or 0
                done[jid] = {"label": label, "status": status, "elapsed_s": elapsed_job}
                print(f"[{ts()} +{elapsed_m:.1f}m] {label} FINISHED: {status} in {elapsed_job/60:.1f} min", flush=True)
                if d.get("error"):
                    print(f"  ERR: {d['error']}", flush=True)

    return done


# ── Main ──────────────────────────────────────────────────────────────────────
CREDITS_START = 1413613
VOICE_START   = 236910

print(f"[{ts()}] === 4-VIDEO MONITOR (v2) ===", flush=True)
print(f"  VoidAI credits start : {CREDITS_START:,}", flush=True)
print(f"  VoiceAPI chars start : {VOICE_START:,}", flush=True)

BATCH1 = {
    "1d0e27d8": "Fear of Starting",
    "b946105c": "Susan David",
}

results_b1 = monitor(BATCH1, "Batch-1")

credits_mid = credits_now()
voice_mid   = voiceapi_balance()
print(f"\n[{ts()}] After Batch-1: VoidAI={credits_mid:,} (-{CREDITS_START-credits_mid:,})  VoiceAPI={voice_mid:,} chars (-{VOICE_START-voice_mid:,})", flush=True)

BATCH2 = start_batch2()
results_b2 = monitor(BATCH2, "Batch-2")

credits_end = credits_now()
voice_end   = voiceapi_balance()

all_results = {**results_b1, **results_b2}
n = len(all_results)

print(f"\n{'='*62}", flush=True)
print(f"=== FINAL REPORT ===", flush=True)
print(f"Total elapsed   : {mins():.1f} min", flush=True)
print(f"VoidAI used     : {CREDITS_START - credits_end:,} credits", flush=True)
print(f"VoiceAPI used   : {VOICE_START   - voice_end:,} chars", flush=True)
print(flush=True)
for jid, info in all_results.items():
    print(f"  {info['label']:<30} {info['status']}  {info['elapsed_s']/60:.1f} min", flush=True)

if n:
    cost_voidai   = (CREDITS_START - credits_end) * 0.00000027 / n
    cost_voiceapi = (VOICE_START   - voice_end)   * 0.0000038  / n
    print(f"\nPer-video est.: VoidAI=${cost_voidai:.3f}  VoiceAPI=${cost_voiceapi:.3f}  Total≈${cost_voidai+cost_voiceapi:.3f}", flush=True)
print(f"{'='*62}", flush=True)
