"""Run frozen example settings against an explicitly supplied local dataset."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
EXPECTED = '5766950f2a105244867b842709cf4bad890e4e11df0eebeeaa4fec42dc05803d'


def file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--factor', choices=['all', '5', '10', '20', 'baseline'], default='all')
    parser.add_argument('--period', choices=['both', 'train', 'validation'], default='both')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--allow-different-data', action='store_true')
    args = parser.parse_args()
    if not args.data.exists(): parser.error('Dataset does not exist; raw market data is not distributed.')
    digest = file_hash(args.data) if args.data.is_file() else None
    if digest != EXPECTED and not args.allow_different_data:
        parser.error('Dataset differs from published evidence. Use --allow-different-data for a NEW study.')
    from pkuquant import Research, Settings
    output = args.output or ROOT / 'outputs' / datetime.now().strftime('examples_%Y%m%d_%H%M%S_%f')
    output.mkdir(parents=True, exist_ok=False)
    lab = Research(args.data.resolve())
    selected = ['5', '10', '20'] if args.factor == 'all' else [args.factor]
    periods = ['train', 'validation'] if args.period == 'both' else [args.period]
    records = []
    for factor in selected:
        config = ROOT / 'example_factors' / ('baseline_5dr_strategy.json' if factor == 'baseline' else f'intraday_{factor}d/strategy.json')
        spec = json.loads(config.read_text(encoding='utf-8-sig'))
        for period in periods:
            options = dict(spec['settings'])
            options.update(dict(start='2021-01-01', end='2025-01-01') if period == 'train' else dict(start='2025-01-02', end='2026-10-16'))
            print(f'Running {factor}: {period}', flush=True)
            result = lab.run(spec['alpha'], Settings(**options))
            folder = output / f'{factor}_{period}'
            result.save(folder)
            records.append(dict(factor=factor, period=period, **result.metrics))
    (output / 'comparison.json').write_text(json.dumps(dict(matches_original_zip=digest == EXPECTED,
        data_sha256=digest, exploratory_selection=True, runs=records), ensure_ascii=False, indent=2), encoding='utf-8')
    print(output.resolve())


if __name__ == '__main__': main()
