#!/usr/bin/env python3
"""
VideoForge — Developer Utilities.

Commands:
    python dev.py next          — Наступна задача
    python dev.py next -md      — Mark done + git commit + next
    python dev.py status [-v]   — Прогрес
    python dev.py commit [-m]   — Git commit
    python dev.py check-env     — Перевірити .env
    python dev.py check-apis    — Тест API
    python dev.py new-project   — Нова папка відео
    python dev.py validate      — Перевірити модулі
    python dev.py log [-n N]    — Session log
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent
PROJECT_PLAN = ROOT / "PROJECT_PLAN.md"
CONTEXT_MD = ROOT / "CONTEXT.md"
TASK_MD = ROOT / "TASK.md"
CURSORRULES = ROOT / ".cursorrules"
SESSION_LOG = ROOT / "session_log.md"
ENV_FILE = ROOT / ".env"

class C:
    G="\033[92m";Y="\033[93m";R="\033[91m";B="\033[94m";CY="\033[96m";BD="\033[1m";DM="\033[2m";E="\033[0m"

def ok(m):   print(f"  {C.G}✓{C.E} {m}")
def warn(m): print(f"  {C.Y}⚠{C.E} {m}")
def err(m):  print(f"  {C.R}✗{C.E} {m}")
def info(m): print(f"  {C.B}ℹ{C.E} {m}")
def hdr(m):  print(f"\n{C.BD}{C.CY}{'─'*50}{C.E}\n{C.BD}  {m}{C.E}\n{C.BD}{C.CY}{'─'*50}{C.E}")

def parse_tasks():
    """Parse all tasks from PROJECT_PLAN.md."""
    if not PROJECT_PLAN.exists(): return []
    content = PROJECT_PLAN.read_text(encoding="utf-8")
    tasks = []
    for num, title, body in re.findall(
        r"\*\*№(\d+)\s*—\s*(.+?)\*\*[^\n]*\n(.*?)(?=\*\*№\d|\n###|\Z)", content, re.DOTALL
    ):
        title = re.sub(r"\*+", "", title).strip()
        dep = re.search(r"Залежить від:\s*(.+)", body)
        tasks.append({"number": int(num), "title": title, "body": body.strip(),
                       "depends": dep.group(1).strip() if dep else "—"})
    return sorted(tasks, key=lambda t: t["number"])

def completed():
    if not CONTEXT_MD.exists(): return set()
    return {int(m.group(1)) for m in re.finditer(r"\[x\]\s*№(\d+)", CONTEXT_MD.read_text(encoding="utf-8"))}

def current_task():
    if not TASK_MD.exists(): return None
    m = re.search(r"Задача №(\d+)", TASK_MD.read_text(encoding="utf-8"))
    return int(m.group(1)) if m else None

def git_repo():
    return subprocess.run(["git","rev-parse","--git-dir"], capture_output=True, cwd=ROOT).returncode == 0

def git_dirty():
    return bool(subprocess.run(["git","status","--porcelain"], capture_output=True, text=True, cwd=ROOT).stdout.strip())

def git_commit(msg):
    if not git_repo(): warn("Не git-репо. git init"); return False
    if not git_dirty(): info("Немає змін"); return True
    subprocess.run(["git","add","-A"], cwd=ROOT)
    r = subprocess.run(["git","commit","-m",msg], capture_output=True, text=True, cwd=ROOT)
    if r.returncode == 0: ok(f"Git: {msg}"); return True
    err(f"Git fail: {r.stderr}"); return False

def mark_done(n):
    if not CONTEXT_MD.exists(): return
    c = CONTEXT_MD.read_text(encoding="utf-8")
    c = re.sub(rf"\[ \]\s*№{n}\b", f"[x] №{n}", c)
    c = re.sub(r"Останнє оновлення: .+", f"Останнє оновлення: {datetime.now().strftime('%Y-%m-%d %H:%M')}", c)
    CONTEXT_MD.write_text(c, encoding="utf-8")

def update_focus(task):
    if not CURSORRULES.exists(): return
    c = CURSORRULES.read_text(encoding="utf-8")
    c = re.sub(r"## Current focus\n.+", f"## Current focus\nЗадача №{task['number']} — {task['title']}", c)
    CURSORRULES.write_text(c, encoding="utf-8")

def gen_task_md(task, all_t, done):
    lines = ["# Поточна задача", "", f"## Задача №{task['number']} — {task['title']}"]
    for l in task["body"].strip().split("\n"):
        l = l.strip()
        if l: lines.append(f"- {l[2:]}" if l.startswith("- ") else f"- {l}")
    nxt = next((t for t in all_t if t["number"] > task["number"] and t["number"] not in done), None)
    lines += ["", "## Наступна задача",
              f"№{nxt['number']} — {nxt['title']}" if nxt else "Остання задача!",
              "", "---", "Після виконання: `python dev.py next -md` → git commit"]
    return "\n".join(lines) + "\n"

# ─── Commands ────────────────────────────────────────────────────────────────

def cmd_next(args):
    hdr("Наступна задача")
    tasks = parse_tasks()
    if not tasks: err("Не розпарсив PROJECT_PLAN.md"); return
    done = completed(); cur = current_task()

    if args.mark_done and cur and cur not in done:
        mark_done(cur); done.add(cur)
        t = next((t for t in tasks if t["number"] == cur), None)
        ok(f"№{cur} — {t['title'] if t else '?'} ✅")
        git_commit(f"✅ №{cur} — {t['title'] if t else 'done'}")

    nxt = None
    if args.task_number:
        nxt = next((t for t in tasks if t["number"] == args.task_number), None)
        if not nxt: err(f"№{args.task_number} не знайдена"); return
    else:
        nxt = next((t for t in tasks if t["number"] not in done), None)
    if not nxt: ok("Все виконано! 🎉"); return

    if nxt["depends"] != "—":
        missing = [int(d) for d in re.findall(r"\d+", nxt["depends"]) if int(d) not in done]
        if missing and not args.force:
            warn(f"Залежить від: {', '.join(f'№{m}' for m in missing)}"); info("--force"); return

    TASK_MD.write_text(gen_task_md(nxt, tasks, done), encoding="utf-8")
    update_focus(nxt)
    ok(f"TASK.md → №{nxt['number']} — {nxt['title']}")
    print(f"\n  {C.BD}Cursor/Claude Code:{C.E}")
    print(f"  @CONTEXT.md @TASK.md Виконай поточну задачу\n")


def cmd_status(args):
    hdr("Прогрес VideoForge")
    tasks = parse_tasks(); done = completed(); cur = current_task()
    total = len(tasks); d = len(done)
    bw = 30; f = int(bw * d / total) if total else 0
    print(f"\n  {'█'*f}{'░'*(bw-f)} {d}/{total} ({int(d/total*100) if total else 0}%)\n")

    for name, rng in [("Фундамент (1-5)",range(1,6)),("Модулі (6-13)",range(6,14)),
                       ("E2E (14-15)",range(14,16)),("Pipeline (16-19)",range(16,20)),("UI (20-25)",range(20,26))]:
        st = [t for t in tasks if t["number"] in rng]; sd = sum(1 for t in st if t["number"] in done)
        if sd == len(st) and st: s = f"{C.G}✓ done{C.E}"
        elif sd: s = f"{C.Y}⏳ {sd}/{len(st)}{C.E}"
        else: s = f"{C.DM}○ todo{C.E}"
        print(f"  {s}  {name}")
        if args.verbose:
            for t in st:
                if t["number"] in done: print(f"         {C.G}✓{C.E} {C.DM}№{t['number']} {t['title']}{C.E}")
                elif t["number"] == cur: print(f"         {C.Y}▶{C.E} №{t['number']} {t['title']} {C.Y}← зараз{C.E}")
                else: print(f"         {C.DM}○ №{t['number']} {t['title']}{C.E}")
    if cur: print(f"\n  {C.BD}Поточна:{C.E} №{cur}")
    if git_repo():
        warn("Незакоммічені зміни!") if git_dirty() else ok("Git: чисто")
    else: warn("Git не ініціалізовано (git init)")
    print()


def cmd_commit(args):
    hdr("Git Commit")
    if not git_repo(): err("Не git-репо!"); return
    if not git_dirty(): ok("Немає змін"); return
    r = subprocess.run(["git","status","--short"], capture_output=True, text=True, cwd=ROOT)
    print(f"\n  {C.DM}Зміни:{C.E}")
    for l in r.stdout.strip().split("\n")[:10]: print(f"    {l}")
    cur = current_task()
    msg = args.message or (f"WIP: №{cur}" if cur else f"WIP: {datetime.now().strftime('%H:%M')}")
    print(); git_commit(msg)


def cmd_check_env(args):
    hdr("Перевірка .env")
    req = {"VOIDAI_API_KEY":"VoidAI","VOIDAI_BASE_URL":"VoidAI URL","WAVESPEED_API_KEY":"WaveSpeed","VOICEAPI_KEY":"VoiceAPI"}
    opt = {"DEFAULT_VOICE_ID":"Voice ID","TRANSCRIBER_OUTPUT_DIR":"Transcriber","YOUTUBE_CLIENT_ID":"YouTube","YOUTUBE_CLIENT_SECRET":"YouTube secret"}
    if not ENV_FILE.exists(): err(".env не знайдено! cp .env.example .env"); return
    env = {}
    for l in ENV_FILE.read_text().split("\n"):
        l = l.strip()
        if l and not l.startswith("#") and "=" in l:
            k, _, v = l.partition("="); env[k.strip()] = v.strip().strip('"').strip("'")
    print(f"\n  {C.BD}Обов'язкові:{C.E}"); all_ok = True
    for k, d in req.items():
        if env.get(k,"") and not env[k].startswith("your_"): ok(k)
        else: err(f"{k} — {d}"); all_ok = False
    print(f"\n  {C.BD}Опціональні:{C.E}")
    for k, d in opt.items():
        if env.get(k,"") and not env[k].startswith("your_"): ok(k)
        else: warn(f"{k} — {d}")
    print(f"\n  {'✅ OK' if all_ok else '❌ Заповни .env'}\n")


def cmd_check_apis(args):
    hdr("API з'єднання")
    import asyncio
    async def go():
        for name, mod, cls, task in [("VoidAI","clients.voidai_client","VoidAIClient","№2"),
                                      ("WaveSpeed","clients.wavespeed_client","WaveSpeedClient","№3"),
                                      ("VoiceAPI","clients.voiceapi_client","VoiceAPIClient","№4")]:
            print(f"\n  {C.BD}{name}:{C.E}")
            try:
                m = __import__(mod, fromlist=[cls])
                async with getattr(m, cls)() as client:
                    result = await client.health_check()
                ok(f"OK {result}")
            except ImportError: warn(f"Ще не створено ({task})")
            except Exception as e: err(str(e))
        print(f"\n  {C.BD}FFmpeg:{C.E}")
        try:
            r = subprocess.run(["ffmpeg","-version"], capture_output=True, text=True)
            ok(r.stdout.split("\n")[0]) if r.returncode==0 else err("Не знайдено")
        except FileNotFoundError: err("Не встановлено")
        print()
    asyncio.run(go())


def cmd_new_project(args):
    hdr("Новий проект")
    vid = args.name or f"video-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    d = ROOT / "projects" / args.channel / vid
    if d.exists(): err(f"Існує: {d}"); return
    for s in ["input","images","audio","subtitles","output"]: (d/s).mkdir(parents=True, exist_ok=True)
    ok(f"{d.relative_to(ROOT)}")
    if args.source:
        import shutil; src = Path(args.source)
        if src.is_dir():
            for f in src.iterdir():
                if f.is_file(): shutil.copy2(f, d/"input"/f.name)
            ok(f"Transcriber output скопійовано з {src.name}")
        else: err(f"Не знайдено: {src}")
    print()


def cmd_validate(args):
    hdr("Валідація")
    for d, l in [(ROOT/"modules","Модулі"),(ROOT/"clients","Клієнти"),(ROOT/"utils","Утиліти")]:
        print(f"\n  {C.BD}{l}:{C.E}")
        if not d.exists(): warn(f"{d.name}/ не створена"); continue
        pf = sorted(f for f in d.glob("*.py") if f.name != "__init__.py")
        if not pf: warn("Порожньо"); continue
        for f in pf:
            try: __import__(f"{d.name}.{f.stem}"); ok(f.name)
            except Exception as e: err(f"{f.name} — {e}")
    print()


def cmd_log(args):
    hdr("Session Log")
    if not SESSION_LOG.exists(): warn("Не знайдено"); return
    entries = re.split(r"\n(?=## )", SESSION_LOG.read_text(encoding="utf-8"))
    for e in entries[-(args.count+1):]: print(f"  {e.strip()}\n")


def main():
    p = argparse.ArgumentParser(description="VideoForge Dev Utils")
    s = p.add_subparsers(dest="cmd")

    n=s.add_parser("next"); n.add_argument("--task",type=int,dest="task_number"); n.add_argument("-md","--mark-done",action="store_true"); n.add_argument("-f","--force",action="store_true")
    st=s.add_parser("status"); st.add_argument("-v","--verbose",action="store_true")
    c=s.add_parser("commit"); c.add_argument("-m","--message")
    s.add_parser("check-env"); s.add_parser("check-apis")
    np=s.add_parser("new-project"); np.add_argument("-c","--channel",required=True); np.add_argument("-n","--name"); np.add_argument("-s","--source",help="Transcriber output folder")
    s.add_parser("validate")
    lg=s.add_parser("log"); lg.add_argument("-n","--count",type=int,default=5)

    a = p.parse_args()
    if not a.cmd: p.print_help(); return
    {"next":cmd_next,"status":cmd_status,"commit":cmd_commit,"check-env":cmd_check_env,
     "check-apis":cmd_check_apis,"new-project":cmd_new_project,"validate":cmd_validate,"log":cmd_log}[a.cmd](a)

if __name__ == "__main__": main()
