"""Verify public evidence hashes and recompute aggregate metrics without raw data."""
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[1] / 'example_factors'


def load(path): return json.loads(path.read_text(encoding='utf-8-sig'))


def rows(path):
    with path.open(encoding='utf-8-sig', newline='') as f: return list(csv.DictReader(f))


def close(actual, expected, context):
    if not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-7):
        raise ValueError(f'{context}: {actual} != {expected}')


def check_run(path):
    manifest = load(path / 'manifest.json'); metrics = manifest['metrics']
    daily = rows(path / 'daily.csv'); ic = rows(path / 'factor_diagnostics.csv')
    assert len(daily) == metrics['trading_days']
    previous = peak = float(manifest['settings']['initial_cash']); worst = 0.0
    for row in daily:
        nav = float(row['nav'])
        close(nav, float(row['cash']) + float(row['invested']), str(path) + ': NAV accounting')
        close(float(row['return']), nav / previous - 1, 'daily return')
        peak = max(peak, nav); worst = min(worst, nav / peak - 1); previous = nav
    close(previous / manifest['settings']['initial_cash'] - 1, metrics['total_return'], 'total_return')
    close(worst, metrics['max_drawdown'], 'max_drawdown')
    close(math.fsum(float(r['costs']) for r in daily), metrics['total_explicit_cost'], 'costs')
    annualization = manifest['settings']['annualization']
    returns = [float(r['return']) for r in daily]
    close((1 + metrics['total_return']) ** (annualization / len(daily)) - 1, metrics['annual_return'], 'annual_return')
    close(statistics.stdev(returns) * math.sqrt(annualization), metrics['annual_volatility'], 'annual_volatility')
    close(statistics.mean(returns) / statistics.stdev(returns) * math.sqrt(annualization), metrics['sharpe_zero_rf'], 'sharpe')
    for name, expected in manifest['factor_diagnostics'].items():
        values = [float(r[name]) for r in ic if r[name] and math.isfinite(float(r[name]))]
        assert len(values) == expected['valid_dates'], (path, name, 'valid_dates')
        close(statistics.mean(values), expected['mean'], name)
    if (path / 'orders.csv').exists():
        orders = rows(path / 'orders.csv')
        assert len(orders) == metrics['orders']
        assert sum(float(r['filled_shares_equiv']) > 0 for r in orders) == metrics['orders_with_fills']
        close(math.fsum(float(r[k]) for r in orders for k in ('commission','stamp_tax','transfer_fee')),
              metrics['total_explicit_cost'], 'order cost reconciliation')
        for r in orders:
            if float(r['filled_shares_equiv']) > 0 and r['signal_date']:
                assert r['signal_date'] < r['date'], (path, 'future/same-day signal fill')
    return manifest


def main():
    checksums = load(ROOT / 'checksums.json')
    actual = {p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*') if p.is_file() and p != ROOT / 'checksums.json'}
    assert actual == set(checksums), 'Unexpected or missing public evidence file'
    for name, expected in checksums.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    runs = sorted(ROOT.rglob('manifest.json'))
    for manifest in runs:
        result = check_run(manifest.parent)
        assert result['source'] == 'feathers.zip (not distributed)', 'Absolute source path was not redacted'
        for name,digest in load(manifest.parent / 'checksums.json').items():
            assert hashlib.sha256((manifest.parent / name).read_bytes()).hexdigest() == digest, name
    for w in (5, 10, 20):
        path = ROOT / f'intraday_{w}d'
        config = load(path / 'strategy.json')
        for period in ('train','validation'):
            manifest = load(path / period / 'manifest.json')
            assert config['alpha'] == manifest['expression']
            for key,value in config['settings'].items():
                if key not in ('start','end'): assert value == manifest['settings'][key], (w, period, key)
    print(f'PASS: {len(checksums)} public files; {len(runs)} runs; NAV, returns, fees, orders, IC, configs and SHA-256.')
    print('This verifies published output consistency, not raw-data authenticity, future performance or live execution.')


if __name__ == '__main__': main()
