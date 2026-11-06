"""Operational recovery only: atomic journals, one writer, awake-time budget."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid


def atomic_write(path, value):
    # Preserve the existing evaluator's numeric/NaN serialization exactly.
    from evolution_preflight import clean
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8') as handle:
            json.dump(clean(value), handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def awake_seconds():
    if os.name != 'nt':
        raise RuntimeError('Recovery clock requires Windows QueryUnbiasedInterruptTime')
    value = ctypes.c_ulonglong()
    if not ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(value)):
        raise ctypes.WinError()
    return value.value / 10_000_000


class SingleInstance:
    """Named kernel mutex releases automatically on process death/reboot."""
    def __init__(self, identity):
        self.name = 'Global\\PKUQuantV3_' + hashlib.sha256(str(identity).lower().encode()).hexdigest()
        self.handle = None

    def __enter__(self):
        if os.name != 'nt':
            raise RuntimeError('Recovery locking requires Windows')
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k.CreateMutexW.restype = ctypes.c_void_p
        k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        k.ReleaseMutex.argtypes = [ctypes.c_void_p]
        self.kernel = k
        self.handle = k.CreateMutexW(None, False, self.name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        result = k.WaitForSingleObject(self.handle, 0)
        if result not in (0, 0x80):
            k.CloseHandle(self.handle)
            self.handle = None
            raise RuntimeError('Experiment already running; duplicate launch refused')
        return self

    def __exit__(self, *args):
        if self.handle:
            self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)


def carried_seconds(folder, checkpoint):
    elapsed = float(checkpoint.get('elapsed_seconds', 0))
    journal = Path(folder) / 'active_clock.json'
    if journal.exists():
        value = json.loads(journal.read_text(encoding='utf-8'))
        elapsed = max(elapsed, float(value['charged_seconds']))
    return elapsed


class ActiveBudget:
    """Precharge five seconds so a power loss cannot reset accumulated usage."""
    def __init__(self, folder, checkpoint, clock=awake_seconds):
        self.path = Path(folder) / 'active_clock.json'
        self.prior = carried_seconds(folder, checkpoint)
        self.clock = clock
        self.start = clock()
        self.stop = threading.Event()
        self.failure = None

    def elapsed(self):
        if self.failure:
            raise RuntimeError('Active-time journal failed') from self.failure
        return self.prior + max(0., self.clock() - self.start)

    def record(self, reserve=5):
        elapsed = self.elapsed()
        atomic_write(self.path, dict(clock='QueryUnbiasedInterruptTime', pid=os.getpid(),
                     elapsed_seconds=elapsed, charged_seconds=elapsed + reserve,
                     crash_reserve_seconds=reserve, updated_unix=time.time()))

    def loop(self):
        while not self.stop.wait(2):
            try:
                self.record()
            except BaseException as error:
                self.failure = error
                return

    def __enter__(self):
        self.record()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()
        self.record(reserve=0)


def authorized_unknown(folder, call):
    """An explicit user exception preserves unknown cost; never authorizes a loop."""
    path=Path(folder)/'manual_retry_authorization.json'
    if not path.exists() or call.get('status')!='failed_or_uncertain':
        return False
    approval=json.loads(path.read_text(encoding='utf-8'))
    return (approval.get('explicit_user_authorization') is True and
            approval.get('original_request_number')==call.get('number') and
            approval.get('original_request_sha256')==call.get('request_sha256') and
            approval.get('reserved_unknown_tokens')==call.get('reservation') and
            approval.get('maximum_new_attempts')==1)


def unknown_reserve(folder, ledger):
    return sum(c['reservation'] for c in ledger.get('calls',[]) if authorized_unknown(folder,c))


def check_model_ledgers(study):
    """Never replay uncertain requests, even if a later generation was saved."""
    for path in Path(study).glob('*/model/ledger.json'):
        ledger = json.loads(path.read_text(encoding='utf-8'))
        for call in ledger.get('calls', []):
            if authorized_unknown(path.parent,call):
                # Its request and reserved token budget remain in the ledger.
                continue
            response = path.parent / ('response_%02d.json' % call['number'])
            if call.get('status') != 'completed' or not response.exists():
                raise RuntimeError('Unresolved API request; inspect before resume: ' +
                                   str(path.parent.parent.name) + '/' + call['phase_id'])


def verified_scripts(study, protocol, actual, digest):
    if actual == protocol['scripts']:
        return
    path = Path(study) / 'runtime_recovery/amendment.json'
    if not path.exists():
        raise RuntimeError('Frozen V3 code/design changed without recovery audit')
    amendment = json.loads(path.read_text(encoding='utf-8'))
    if (amendment['original_scripts'] != protocol['scripts'] or
            amendment['protocol_sha256'] != digest(Path(study) / 'protocol.json') or
            amendment['approved_scripts'] != actual or
            amendment['research_rules_changed'] is not False):
        raise RuntimeError('Recovery code differs from approved audit')
    for name, sha in amendment['approved_scripts'].items():
        if digest(path.parent / 'source_snapshot' / name) != sha:
            raise RuntimeError('Recovery snapshot changed: ' + name)
