"""PKUQuant: concise factors, explicit execution assumptions."""
from .api import Research
from .config import Settings
from .evaluation import EvaluationSpec, EvaluationResult
from .data import MarketData
from .security import SecurityHistory
from .sources import DataSource,TabularSource,QMTSource,TushareSource,FunctionSource,from_long
from .providers import DataRequest,ProviderInfo,HistoricalProvider,FunctionProvider,SourceProvider,DatasetBundle,MissingCapability,collect
from .factors import (rank,zscore,scale,ts_delay,ts_delta,ts_return,ts_mean,ts_sum,
                      ts_std,ts_min,ts_max,ts_rank,ts_corr,decay_linear,where,
                      clip,log,sign,trade_when)

__version__ = '0.6.1'
