"""Frozen, deterministic V2 decisions. No model or market access."""
import math

EPS = 1e-12
YEARS = ('2023', '2024', '2025')


def finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def dev(row):
    return row['evaluation']['periods']['development']


def adequate(evaluation):
    for name in ('development', *YEARS):
        r = evaluation.get('periods', {}).get(name, {})
        if not all(finite(r.get(k)) for k in ('rank_ic', 'sharpe', 'drawdown', 'top_excess',
                                              'valid_ic_dates', 'matured_label_coverage')):
            return False
        if not (r['valid_ic_dates'] >= 60 and r['matured_label_coverage'] >= .8
                and r['rank_ic'] > 0 and 0 <= r['drawdown'] <= 1):
            return False
    return True


def promotion(evaluation, anchor):
    checks = {'adequate': adequate(evaluation)}
    c = evaluation.get('periods', {}).get('development', {})
    a = anchor['periods']['development']
    for k, offset in [('rank_ic', .002), ('sharpe', .10), ('top_excess', 0)]:
        checks[k + ':anchor_threshold'] = finite(c.get(k)) and c[k] + EPS >= a[k] + offset
    checks['top_excess:positive'] = finite(c.get('top_excess')) and c['top_excess'] > 0
    checks['drawdown:fixed_cap'] = finite(c.get('drawdown')) and 0 <= c['drawdown'] <= a['drawdown'] + .05 + EPS
    yearly = 0
    for year in YEARS:
        x = evaluation.get('periods', {}).get(year, {})
        y = anchor['periods'][year]
        yearly += all(finite(x.get(k)) and x[k] + EPS >= y[k] for k in ('rank_ic', 'sharpe'))
    checks['two_years_preserve_both'] = yearly >= 2
    for name in ('cost_x2', 'delay_plus_1'):
        x, y = evaluation.get('stress', {}).get(name, {}), anchor['stress'][name]
        checks[name] = (x.get('period') == y.get('period') == '2023-01-01/2025-12-31'
                        and finite(x.get('sharpe')) and finite(x.get('drawdown'))
                        and x['sharpe'] + EPS >= y['sharpe'])
    accepted = all(checks.values())
    return {'promoted': accepted, 'checks': checks, 'failed': [k for k, v in checks.items() if not v],
            'three_objective_improvement': accepted and c['drawdown'] < a['drawdown'] - EPS}


def admission(row, parent, anchor):
    if not adequate(row['evaluation']):
        return {'formal': False, 'diagnostic': False, 'failed': ['adequate'], 'strength': None}
    c, p, a = dev(row), dev(parent), dev(anchor)
    di, ds = c['rank_ic'] - p['rank_ic'], c['sharpe'] - p['sharpe']
    checks = {'ic_floor': c['rank_ic'] + EPS >= a['rank_ic'] - .010,
              'sharpe_floor': c['sharpe'] + EPS >= a['sharpe'] - .30,
              'positive_top20': c['top_excess'] > 0,
              'drawdown_cap': c['drawdown'] <= a['drawdown'] + .05 + EPS,
              'local_improvement': ((di > EPS and ds >= -.10 - EPS) or
                                    (ds > EPS and di >= -.002 - EPS))}
    accepted = all(checks.values())
    return {'formal': accepted, 'diagnostic': not accepted and checks['drawdown_cap']
            and (di + EPS >= .002 or ds + EPS >= .10), 'checks': checks,
            'failed': [k for k, v in checks.items() if not v],
            'strength': max(di / .002, ds / .10), 'delta_ic': di, 'delta_sharpe': ds,
            'compared_to': parent['id']}


def pareto(rows):
    def dominates(a, b):
        x, y = dev(a), dev(b)
        return (all(x[k] + EPS >= y[k] for k in ('rank_ic', 'sharpe'))
                and any(x[k] > y[k] + EPS for k in ('rank_ic', 'sharpe')))
    return [r for r in rows if not any(s['id'] != r['id'] and dominates(s, r) for s in rows)]


def lower(row, key='rank_ic'):
    x = row.get('screen', row).get('periods', {}).get('development', {}).get(key, {}).get('ci_low')
    return x if finite(x) else -1e100


def complexity(row):
    return row.get('nodes', 9999), row['expression']


def champion(rows, anchor):
    eligible = pareto([r for r in rows if r.get('decision', {}).get('promoted')])
    if not eligible:
        return anchor
    a = dev(anchor)
    def key(r):
        d = dev(r)
        q = min((d['rank_ic'] - a['rank_ic']) / .002, (d['sharpe'] - a['sharpe']) / .10)
        return (-math.floor((q + EPS) / .1), d['drawdown'], -q, *complexity(r))
    return min(eligible, key=key)


def select_screen(rows, seeds, anchor, corr):
    qualified = [r for r in rows if r.get('screen', {}).get('screen_qualified')]
    selected, decisions = [], []
    for slot in ('ic', 'top20', 'diversity'):
        remaining = [r for r in qualified if r not in selected]
        if slot == 'top20':
            remaining = [r for r in remaining if r['screen']['periods']['development']['top_excess']['mean'] > 0]
        if slot == 'diversity':
            remaining = [r for r in remaining if r['screen']['periods']['development']['top_excess']['mean'] > 0
                         and r['screen']['periods']['development']['rank_ic']['mean'] + EPS >= dev(anchor)['rank_ic'] - .010]
        ranking = []
        for r in remaining:
            base = (-lower(r), *complexity(r))
            if slot == 'ic':
                key = base
            elif slot == 'top20':
                key = (-lower(r, 'top_excess'), *base)
            else:
                values = [corr(r, x) for x in [*seeds, *selected]]
                if not values or any(v is None for v in values):
                    continue
                key = (max(values), *base)
            ranking.append((key, r))
        if ranking:
            _, chosen = min(ranking, key=lambda x: x[0])
            selected.append(chosen)
            decisions.append({'slot': slot, 'id': chosen['id']})
        else:
            decisions.append({'slot': slot, 'id': None, 'reason': 'no_qualified_candidate'})
    return selected, decisions


def update_pools(previous, new, seeds, corr):
    candidates = {r['id']: r for r in [*previous, *new] if r.get('admission', {}).get('formal')}
    remaining, chosen, categories = pareto(list(candidates.values())), [], {}
    for category in ('ic', 'sharpe', 'diversity'):
        taken = 0
        while taken < 3 and remaining:
            ranked = []
            for r in remaining:
                # Undefined correlation does not prove duplicates or diversity.
                existing = [corr(r, x) for x in chosen]
                if any(v is not None and v > .98 for v in existing):
                    continue
                d = dev(r)
                tail = (d['drawdown'], *complexity(r))
                if category == 'ic':
                    key = (-lower(r), -d['sharpe'], *tail)
                elif category == 'sharpe':
                    key = (-d['sharpe'], -d['rank_ic'], *tail)
                else:
                    values = [corr(r, x) for x in [*seeds, *chosen]]
                    if any(v is None for v in values):
                        continue
                    key = (max(values), -lower(r), *tail)
                ranked.append((key, r))
            if not ranked:
                break
            r = min(ranked, key=lambda x: x[0])[1]
            chosen.append(r); remaining.remove(r); categories[r['id']] = category; taken += 1
    diagnostic = sorted([r for r in new if r.get('admission', {}).get('diagnostic')],
                        key=lambda r: (-r['admission']['strength'], len(r['admission']['failed']), *complexity(r)))[:3]
    return chosen, diagnostic, categories
