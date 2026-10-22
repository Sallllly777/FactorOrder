"""Causality checks for the exact four published factor configurations."""
import json
import unittest
import numpy as np
import pandas as pd
from test_system import ROOT, fixture
from pkuquant import Research, Settings, MarketData


class PublishedFactorIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture(160)
        t = np.arange(160)[:, None]
        j = np.arange(3)[None, :]
        close = 10 + j + .005*t + .3*np.sin(t/(3+j)+j)
        opening = close * (1 + .012*np.cos(t/(2+j)+j))
        for name, values in [('close',close), ('open',opening),
                             ('high',np.maximum(close,opening)+.2),
                             ('low',np.minimum(close,opening)-.2)]:
            self.data.frames[name].iloc[:, :] = values
        paths = [ROOT/'example_factors/baseline_5dr_strategy.json'] + [
            ROOT/f'example_factors/intraday_{w}d/strategy.json' for w in (5,10,20)]
        self.configs = [json.loads(p.read_text(encoding='utf-8-sig')) for p in paths]
        self.cutoff = self.data.dates[119]

    def settings(self, config, end):
        return Settings(**dict(config['settings'], start=str(self.data.dates[65].date()), end=str(end.date())))

    def test_published_signals_equal_prefix_and_ignore_later_data(self):
        changed = MarketData({k: v.copy() for k,v in self.data.frames.items()})
        for name in ('open','high','low','close','cumadj','amt'):
            changed.frames[name].iloc[120:] *= np.array([1.7, 2.3, .8])
        for cfg in self.configs:
            with self.subTest(alpha=cfg['alpha']):
                full = Research(self.data).factor(cfg['alpha'], self.settings(cfg,self.data.dates[-1]))
                prefix = Research(self.data).factor(cfg['alpha'], self.settings(cfg,self.cutoff))
                mutated = Research(changed).factor(cfg['alpha'], self.settings(cfg,self.data.dates[-1]))
                pd.testing.assert_frame_equal(full.loc[:self.cutoff], prefix)
                pd.testing.assert_frame_equal(full.loc[:self.cutoff], mutated.loc[:self.cutoff])

    def test_published_past_orders_and_nav_do_not_depend_on_later_sessions(self):
        for cfg in self.configs:
            with self.subTest(alpha=cfg['alpha']):
                short = Research(self.data).run(cfg['alpha'],self.settings(cfg,self.cutoff))
                full = Research(self.data).run(cfg['alpha'],self.settings(cfg,self.data.dates[-1]))
                pd.testing.assert_frame_equal(short.daily, full.daily.loc[:self.cutoff])
                for name in ('orders','targets'):
                    all_rows = getattr(full,name)
                    pd.testing.assert_frame_equal(getattr(short,name).reset_index(drop=True),
                        all_rows[all_rows.date <= self.cutoff].reset_index(drop=True))
                self.assertTrue((short.orders.signal_date < short.orders.date).all())

    def test_training_labels_cannot_cross_requested_end(self):
        for cfg in self.configs:
            with self.subTest(alpha=cfg['alpha']):
                run = Research(self.data).run(cfg['alpha'],self.settings(cfg,self.cutoff))
                self.assertEqual(run.daily.index[-1], self.cutoff)
                for horizon in (1,5,20):
                    self.assertTrue(run.diagnostics[f'rank_ic_{horizon}d'].iloc[-horizon:].isna().all())
                    self.assertTrue((run.diagnostics[f'label_count_{horizon}d'].iloc[-horizon:] == 0).all())


if __name__ == '__main__': unittest.main()
