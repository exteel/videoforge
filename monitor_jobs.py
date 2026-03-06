"""Sync job monitor using requests — robust against httpx ReadTimeout."""
import sys, time, requests
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')

JOBS = [('4e04d812', 'Ken Burns'), ('18101452', 'no_ken_burns')]
T0 = time.time()


def get_job(jid):
    try:
        r = requests.get(f'http://localhost:8000/api/jobs/{jid}', timeout=120)
        return r.json()
    except Exception as e:
        return {'_err': str(e)}


def main():
    print(f'Monitor started at {datetime.now().strftime("%H:%M:%S")}', flush=True)
    prev = {}
    done = set()
    while True:
        time.sleep(45)
        mins = (time.time() - T0) / 60
        for jid, label in JOBS:
            if jid in done:
                continue
            d = get_job(jid)
            if '_err' in d:
                print(f'[+{mins:.1f}m] {label}: TIMEOUT/ERR {d["_err"][:60]}', flush=True)
                continue

            status = d['status']
            step = d['step']
            sname = d.get('step_name', '')
            pct = d.get('pct', 0)
            key = f'{status}:{step}:{int(pct // 5)}'

            if key != prev.get(jid):
                prev[jid] = key
                ts = datetime.now().strftime('%H:%M:%S')
                print(f'[{ts} +{mins:.1f}m] {label}: {status} step={step} {sname} {pct:.0f}%', flush=True)
                logs = d.get('logs') or []
                if logs:
                    print(f'  > {logs[-1]}', flush=True)

            if status == 'waiting_review':
                rs = d.get('review_stage', '')
                print(f'  !! NEEDS APPROVE stage={rs}', flush=True)

            if status in ('done', 'failed', 'cancelled'):
                done.add(jid)
                ts = datetime.now().strftime('%H:%M:%S')
                elapsed = d.get('elapsed') or 0
                print(f'[{ts} +{mins:.1f}m] {label} FINISHED: {status} elapsed={elapsed:.0f}s', flush=True)
                if d.get('error'):
                    print(f'  ERR: {d["error"]}', flush=True)

        if len(done) >= len(JOBS):
            mins = (time.time() - T0) / 60
            print(f'[+{mins:.1f}m] === ALL DONE ===', flush=True)
            break


if __name__ == '__main__':
    main()
