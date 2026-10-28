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
    check_evaluations()
    print(f'PASS: {len(checksums)} public files; {len(runs)} runs; NAV, returns, fees, orders, IC, configs and SHA-256.')
    print('This verifies published output consistency, not raw-data authenticity, future performance or live execution.')


def check_evaluations():
    folder = ROOT / 'evaluation'
    hashes = load(folder / 'checksums.json')
    actual = {p.relative_to(folder).as_posix() for p in folder.rglob('*')
              if p.is_file() and p != folder / 'checksums.json'}
    assert actual == set(hashes), 'Unexpected or missing evaluation export'
    for name, digest in hashes.items():
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest, name
    provenance = load(folder / 'provenance.json')
    for export in provenance['exports']:
        path = folder / 'results' / export['factor']
        for name, digest in export['original_artifact_sha256'].items():
            assert hashlib.sha256((path / name).read_bytes()).hexdigest() == digest, name
        e = load(path / 'evaluation.json'); strategy = load(path / 'strategy.json')
        assert strategy['alpha'] == export['expression']
        assert strategy['settings'] == e['protocol']['settings']
        assert strategy['evaluation'] == e['protocol']['evaluation']
        assert e['comparison_sha256'] == provenance['comparison_sha256']
        score = e['score']; items = score['items']
        assert sum(x['weight'] for x in items) == 100
        for x in items:
            if x['grade'] is None: assert x['points'] is None
            else: close(x['points'], x['weight'] * x['grade'] / 4, 'item points')
        close(sum(x['points'] or 0 for x in items), score['earned_points'], 'score sum')
        weight = sum(x['weight'] for x in items if x['grade'] is not None)
        assert score['evaluated_weight'] == weight
        if weight == 100: close(score['total'], score['earned_points'], 'total')
        else: assert score['total'] is None
        returns = rows(path / 'evaluation_period_returns.csv')
        for period in e['stability']['periods']:
            data = [r for r in returns if r['period'] == period['name']]
            opts = period['portfolio']['settings']
            for role in ('candidate', 'benchmark'):
                previous = peak = opts['initial_cash']; worst = 0.0
                for r in data:
                    nav = float(r[role + '_nav'])
                    close(float(r[role + '_return']), nav / previous - 1, 'period daily return')
                    peak = max(peak, nav); worst = min(worst, nav / peak - 1); previous = nav
                m = period['portfolio'][role]
                assert len(data) == m['trading_days']
                close(previous / opts['initial_cash'] - 1, m['total_return'], 'period return')
                close((previous / opts['initial_cash']) ** (opts['annualization'] / len(data)) - 1,
                      m['annual_return'], 'period annual return')
                close(worst, m['max_drawdown'], 'period drawdown')
    from build_evaluation_report import render
    assert (folder / 'report.html').read_bytes() == render().encode('utf-8'), 'Stale evaluation HTML'
    print('PASS: four factor evaluations, score arithmetic, independent NAV/returns/drawdowns and offline HTML.')


if __name__ == '__main__': main()
