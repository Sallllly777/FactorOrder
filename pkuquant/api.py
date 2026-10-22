from pathlib import Path
import hashlib
import inspect
import pandas as pd
import numpy as np
from .config import Settings
from .data import MarketData
from .factors import Fields, Panel, Expression, decay_linear
from .engine import simulate, TradingStates
from .report import Result
from .diagnostics import factor_diagnostics
from .security import SecurityHistory, status_coverage
from .sources import load_source


class Research:
    def __init__(self,source='feathers.zip',trading_states=None,security_history=None,reference_cache=None):
        from .providers import DatasetBundle
        if isinstance(source,(str,Path)) and (Path(source)/'bundle.json').is_file(): source=DatasetBundle.load(source)
        self.bundle_metadata=None
        if isinstance(source,DatasetBundle):
            if trading_states is not None or security_history is not None:
                raise ValueError('A bundle already owns its ST history and rules; do not silently override them')
            source.validate()
            self.bundle_metadata=source.provenance()
            trading_states,security_history=source.trading_states,source.security_history
            source=source.market
        self.data = load_source(source)
        self.reference_audit=None
        if reference_cache is not None:
            if trading_states is not None or security_history is not None:
                raise ValueError('Reference cache cannot silently override explicit states/history')
            from .reference import ReferenceSnapshot
            trading_states=ReferenceSnapshot(self.data,reference_cache)
            security_history=trading_states.security
            self.reference_audit=trading_states.audit
        if isinstance(security_history,(str,Path)): security_history=pd.read_csv(security_history)
        self.security = security_history if isinstance(security_history,SecurityHistory) else SecurityHistory(security_history)
        if isinstance(trading_states,TradingStates):
            self.states_hash=getattr(trading_states,'sha256',None)
        elif isinstance(trading_states,(str,Path)):
            self.states_hash = hashlib.sha256(Path(trading_states).read_bytes()).hexdigest()
            trading_states = pd.read_csv(trading_states)
        else:
            self.states_hash = hashlib.sha256(trading_states.to_csv(index=False).encode()).hexdigest() if trading_states is not None else None
        self.states = trading_states if isinstance(trading_states,TradingStates) else TradingStates(trading_states)

    @classmethod
    def from_provider(cls,provider,request,*,require=()):
        from .providers import collect
        return cls(collect(provider,request,require=require))

    def _prepare(self,settings):
        data = self.data.through(settings.end) if settings.end else self.data
        trimmed = []
        if settings.require_trade_counts:
            while data.frames['num_trades'].iloc[-1].isna().all():
                trimmed.append(str(data.dates[-1].date()))
                if len(data.dates)<2: raise ValueError('No date with trade counts')
                data = data.through(data.dates[-2])
        status=self.security.matrix(data.dates,data.codes)
        tradable=None
        if self.reference_audit is not None:
            tradable=pd.DataFrame(self.states.tradable,index=self.states.dates,columns=self.states.codes).reindex(index=data.dates,columns=data.codes)
        eligible,adv = data.universe(settings,status,tradable)
        return data,eligible,adv,trimmed

    def doctor(self,settings=None):
        from .readiness import inspect_readiness
        return inspect_readiness(self,settings or Settings())

    def factor(self,alpha,settings=None):
        settings = settings or Settings()
        data,eligible,_,_ = self._prepare(settings)
        fields = Fields(data,eligible)
        panel = Expression(alpha).evaluate(fields)[0] if isinstance(alpha,str) else alpha(fields)
        if not isinstance(panel,Panel): raise TypeError('Python strategies must return a Panel')
        return decay_linear(panel,settings.decay).values.where(eligible)

    def run(self,alpha,settings=None,**kwargs):
        if settings is not None and kwargs: raise ValueError('Pass Settings or keyword settings, not both')
        settings = settings or Settings(**kwargs)
        data,eligible,adv,trimmed = self._prepare(settings)
        status=self.security.matrix(data.dates,data.codes,at='09:25:00')
        coverage=status_coverage(status,data.frames['close'].notna())
        if settings.require_verified_rules:
            # Fail closed for every observed security/day in the requested run;
            # a candidate selected later must not silently get guessed limits.
            begin=pd.Timestamp(settings.start) if settings.start else data.dates[0]
            for i,date in enumerate(data.dates):
                if date<begin: continue
                for j in np.flatnonzero(data.frames['close'].iloc[i].notna().to_numpy()):
                    if self.states.rows.get((date,str(data.codes[j])),{}).get('source')!='provided':
                        raise ValueError(f'Missing verified trading rules: {date.date()} {data.codes[j]}')
        fields = Fields(data,eligible)
        if isinstance(alpha,str):
            panel,used = Expression(alpha).evaluate(fields)
            description = alpha
        elif callable(alpha):
            panel,used = alpha(fields), {'python_callback'}
            try: description = inspect.getsource(alpha).strip()
            except (OSError,TypeError): description = repr(alpha)
        else: raise TypeError('alpha must be an expression string or callable')
        if not isinstance(panel,Panel): raise TypeError('Alpha must return a Panel')
        if not panel.values.index.equals(data.dates) or not panel.values.columns.equals(data.codes):
            raise ValueError('Strategy changed axes')
        score = decay_linear(panel,settings.decay).values.where(eligible)
        if score.notna().to_numpy().sum()==0: raise ValueError('Factor has no valid eligible observations')
        sim = simulate(data,score,eligible,adv,settings,self.states,status)
        limits = [
            '账户采用复权经济单位，隐含公司行动调整及分红再投资；不重建真实分红税、现金到账日与股份可卖日。持仓 shares_equiv 是原始价格对应的经济等效股数。',
            f'股数计划={settings.sizing}；previous_close 使用前收与前日权益固定计划，execution_open 为旧版开盘重算研究假设。信号延迟与计划日期单独记录。容量={settings.capacity}；lagged_adv 仅用前日均额和前收固定容量股数。日线仍不能证明开盘流动性或排队成交。',
            '无逐日交易规则时使用标记的估计：已知普通主板按10%，主板ST或未知状态在2026-07-06前按5%保守阈值；不能替代官方ST/IPO/除权特例数据。缓存重建规则也不等于官方逐日限价。',
            '所有高低价相同的日线都拒绝成交，这是保守假设；不会把涨停卖出等同于交易所禁止卖出。',
            '滑点后价格恰好触及不利方向涨跌停也拒单。前收定量模式对复权因子相对变化超过1e-5的日期取消旧计划；阈值用于过滤源数据微小噪声，不证明较小公司行动不存在。仍是经济单位账户，未据此生成真实分红或送转股份。',
            f'amt 乘数为 {settings.amount_multiplier:g}；'+('适配器已显式换算为元，通常保持乘数 1。' if data.metadata.get('units_explicit') else '原始供应商单位未确认，默认按元解释。')+'num_trades 不作为成交股数或 VWAP 分母。',
            f'佣金、最低收费和滑点为可配置研究假设；过户等额外费用率设为 {settings.transfer_rate:g}。印花税按配置及历史切换日处理，非完整券商交割单。',
            f'缺失/无成交行情持仓保留最近估值；停滞 {settings.stale_haircut_after} 日后的估值折扣为 {settings.stale_haircut:.0%}。不事后在最后有行情日强制卖出，不证明退市处置价值。',
            f'ST 策略={settings.st_policy}，未知状态={settings.unknown_st}；开盘状态覆盖率={coverage["coverage"]}，未知证券日={coverage["unknown_cells"]}。只有输入的历史状态可用于过滤；未提供状态不等于普通股。',
            '无历史行业/市值，未实施行业中性、规模中性；历史数据覆盖仍可能存在供应商选择偏差。',
            '未自动寻优；本报告不是样本外盈利认证。夏普使用零无风险利率、日收益和固定年化系数。',
        ]
        if trimmed: limits.append(f'末尾成交笔数整行缺失，自动剔除日期：{", ".join(trimmed)}。')
        if data.metadata.get('synthetic'): limits.insert(0,'本报告使用合成示例行情和虚构状态，仅用于接口/软件验证，不是真实证券回测。')
        if not isinstance(alpha,str): limits.append('Python 回调是可信研究代码，可自行访问未来数据；因果安全需由使用者审计。表达式接口禁止未来窗口。')
        metadata = dict(expression=description,fields=sorted(used),accounting='total_return_economic_units',
                        source=data.source,source_sha256=data.source_hash,quality=data.quality(),
                        effective_end=str(data.dates[-1].date()),trimmed_dates=trimmed,
                        trading_states_sha256=self.states_hash,limitations=limits,
                        execution_contract_version='0.5',
                        historical_reference=self.reference_audit,
                        security_history_sha256=self.security.sha256,st_coverage=coverage,provider_bundle=self.bundle_metadata,
                        readiness=self.doctor(settings),
                        references=['https://www.worldquant.com/brain/',
                        'https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml'])
        diagnostics = factor_diagnostics(score,data,sim.daily.index[0],sim.daily.index[-1])
        return Result(sim,settings,metadata,diagnostics)
