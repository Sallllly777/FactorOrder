"""Run from repository: python examples/custom_strategy.py.
The bootstrap is only for this workspace; pip install -e . removes that need.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'.analysis_deps'))
from pkuquant import Research, Settings, rank, ts_delay, ts_std


def alpha(f):
    momentum = ts_delay(f.close,20) / ts_delay(f.close,120) - 1
    risk = ts_std(f.returns,60)
    return rank(momentum) - 0.25*rank(risk)


if __name__ == '__main__':
    from datetime import datetime
    result = Research(ROOT/'feathers.zip').run(alpha, Settings(end='2023-12-29'))
    folder = result.save(ROOT/'outputs'/datetime.now().strftime('custom_%Y%m%d_%H%M%S_%f'))
    print(result.summary())
    print(folder)
