"""Run this entry point again after waking/restarting; never duplicates API calls."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from evolution_v3_runtime import SingleInstance, check_model_ledgers, atomic_write

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STUDY = ROOT / 'outputs/evolution_v3_20261105_01'


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def preflight(study):
    if not study.is_relative_to(ROOT / 'outputs'):
        raise ValueError('Study must stay within project outputs')
    from evolution_v3_run import verify_protocol
    protocol = verify_protocol(study)
    check_model_ledgers(study)
    # Also detects an orphaned child if its coordinator was interrupted.
    for task in protocol['schedule']:
        with SingleInstance(study / (task['arm'] + str(task['seed']))):
            pass
    return protocol


def worker(study):
    # This outer lock covers both search and the single post-freeze review.
    with SingleInstance(str(study) + ':resume'):
        preflight(study)
        atomic_write(study/'resume_status.json',dict(status='running',pid=os.getpid(),
                     started_unix=time.time(),stage='search_then_single_historical_review'))
        from evolution_v3_run import coordinate
        status = load(study / 'status.json')
        if not (status.get('search_complete') and status.get('status') == 'completed'):
            coordinate(study, resume=True)
        from evolution_v3_review import gate, main as review
        gate(study)  # all nine freezes before any historical results/data access
        previous = study / 'historical_review/status.json'
        if previous.exists():
            state = load(previous)
            if state['status'] != 'completed':
                raise RuntimeError('Existing historical review needs inspection; not restarted')
        else:
            review(study)
        atomic_write(study / 'resume_status.json', dict(status='completed',pid=os.getpid(),
                     search_and_historical_review_complete=True,prospective_followup_complete=False))


def launch(study):
    try:
        with SingleInstance(str(study) + ':resume'), SingleInstance(study):
            preflight(study)
            previous = study / 'historical_review/status.json'
            if previous.exists():
                # Validate search gate before reading review results/status.
                from evolution_v3_review import gate
                gate(study)
                state = load(previous)
                if state['status'] == 'completed':
                    print(json.dumps(dict(status='already_completed')))
                    return
                raise RuntimeError('Existing historical review needs inspection; not restarted')
        with (study / 'resume_worker.log').open('a', encoding='utf-8') as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                '--study', str(study), '--worker'], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        atomic_write(study / 'resume_launcher.json',dict(pid=process.pid,launched_unix=time.time(),
                     entrypoint=str(Path(__file__).resolve()),study=str(study)))
        print(json.dumps(dict(status='launched',pid=process.pid,study=str(study)),ensure_ascii=False))
    except RuntimeError as error:
        if 'already running' in str(error):
            print(json.dumps(dict(status='already_running',message=str(error))))
            return
        raise


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study',type=Path,default=DEFAULT_STUDY)
    parser.add_argument('--worker',action='store_true')
    args = parser.parse_args()
    study = args.study.resolve()
    if args.worker:
        # Failures are visible even when the process is running without a window.
        try:
            worker(study)
        except BaseException as error:
            # A duplicate worker must not overwrite the legitimate owner's state.
            if 'already running' not in str(error):
                atomic_write(study/'resume_status.json',dict(status='failed',pid=os.getpid(),
                             error_type=type(error).__name__,error=str(error),updated_unix=time.time()))
            raise
    else:
        launch(study)
