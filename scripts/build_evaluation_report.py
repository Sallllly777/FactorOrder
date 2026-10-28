"""Build the offline public evaluation page from published evidence (no market data)."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'example_factors' / 'evaluation'


def render():
    records = []
    for key in ('baseline', '5', '10', '20'):
        folder = ROOT / 'results' / key
        strategy = json.loads((folder / 'strategy.json').read_text(encoding='utf-8'))
        records.append(dict(key=key, alpha=strategy['alpha'],
            evaluation=json.loads((folder / 'evaluation.json').read_text(encoding='utf-8'))))
    html = (ROOT / 'assets' / 'report.template.html').read_text(encoding='utf-8')
    css = (ROOT / 'assets' / 'report.css').read_text(encoding='utf-8')
    js = (ROOT / 'assets' / 'report.js').read_text(encoding='utf-8')
    payload = json.dumps(records, ensure_ascii=False, allow_nan=False).replace('<', '\\u003c')
    js = js.replace('/* PUBLIC_EVALUATION_DATA */', payload)
    return html.replace('<!-- PUBLIC_STYLE -->', '<style>' + css + '</style>').replace(
        '<!-- PUBLIC_SCRIPT -->', '<script>' + js + '</script>')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    expected = render().encode('utf-8')
    if args.check:
        assert (ROOT / 'report.html').read_bytes() == expected, 'Run scripts/build_evaluation_report.py'
        print('PASS: offline evaluation page matches published JSON and templates.')
    else:
        (ROOT / 'report.html').write_bytes(expected)
        print(ROOT / 'report.html')


if __name__ == '__main__':
    main()
