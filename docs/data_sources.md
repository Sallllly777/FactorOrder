# 数据源与历史 ST 状态

行情、证券历史状态、逐日交易规则是三份独立输入。只有行情不能推出当时是否 ST，也不能推出所有特殊交易日的合法涨跌停价。当前实现仅面向沪深 A 股做多日频研究，不支持将债券、ETF、期货或北交所直接套入同一执行规则。

## ST / *ST：历史事件接口

```python
from pkuquant import Research, Settings

lab = Research("feathers.zip", trading_states="daily_rules.csv",
               security_history="security_history.csv")
result = lab.run("rank(ts_return(close, 60))", Settings(
    start="2021-01-01", end="2023-12-29",
    st_policy="exclude", unknown_st="error", require_verified_rules=True,
))
```

`security_history.csv` 列：

| 字段 | 含义 |
|---|---|
| code | 规范证券代码，如 000001.SZ |
| effective_date | 状态生效的交易日期 |
| known_at | 信息实际已公开/可获取的时间；无时区时按上海本地时间解释 |
| is_st | 1 表示 ST 或 *ST；0 表示有依据的普通状态 |

两种时间都满足才应用事件。信号在 15:00 查询，开盘风险检查在 09:25 查询；当日午间才获知的信息不能阻止同日早盘订单。状态延续到下一个已知且生效的事件。**事件流必须包含起始状态及全部变化**，缺少中间变化无法靠格式校验发现。只有生效日期、没有实际公告时间的历史表，不能擅自把 known_at 填成提前一天。

默认 `st_policy="exclude"` 排除已知 ST；`unknown_st="allow"` 允许未知状态以兼容原始研究数据，同时在报告中显示未知覆盖率。严谨研究使用 `unknown_st="error"` 遇未知停止，或 `"exclude"` 剔除未知；不能把未知标记为普通股。严格模式会检查整个因子准备区间的可选股票池，包括预热区间。

执行日再次检查 ST。默认 `liquidate_st=True` 时，调仓间隔中的 ST 持仓也逐日尝试退出；设置 False 仅关闭额外的日常退出，正常调仓仍按排除后的股票池减仓。退出服从 T+1、停牌、跌停、金额/股数容量，失败不会凭空变现。`orders.csv` 含执行时 `is_st` 与拒绝原因。

`examples/security_history_format.csv` **仅为虚构格式示例，不是对应股票的真实历史**。不要将它用于真实回测。

QMT 有历史 ST 数据功能，行情文档也列出了 `specialtreatment` 和 `stoppricedata`。实际权限、历史覆盖与可得时间字段依版本而异；本项目未把实时 InstrumentName 自动倒填为历史。参考 [QMT 行情文档](https://dict.thinktrader.net/nativeApi/xtdata.html)、[QMT 股票功能文档](https://dict.thinktrader.net/dictionary/stock.html)。

## 多数据源入口

```text
QMT / Tushare / CSV / Parquet / 自定义供应商
                ↓ 显式字段与单位转换
       MarketData：原始 OHLC + cumadj
                ↓ 批量 PIT 因子
      历史 ST + 逐日交易规则 → 日频撮合
```

`DataSource` 只有 `load() -> MarketData` 一个方法。Research 接受该接口、MarketData、原始 Feather ZIP 或目录。每次回测加载静态快照，交易循环不向券商查询实时行情。

### 通用文件与自定义接口

```python
from pkuquant import Research, TabularSource, FunctionSource

source = TabularSource("broker.csv", amount_unit="CNY", volume_unit="shares",
                       mapping={"symbol":"code", "turnover":"amt"})
lab = Research(source)
# 一个可信的本地函数也可以接供应商 SDK / 数据库：
# source = FunctionSource(fetch_daily_dataframe, provider="my_vendor",
#                         amount_unit="CNY", volume_unit="shares")
```

标准长表必须有 `date,code,open,high,low,close,amt,cumadj`，可选真实 `volume` 和 `num_trades`。缺少交易笔数保留 NaN，不由成交量伪造；该类数据需显式设置 `require_trade_counts=False` 才可运行。`amount_unit` 必填：CNY / CNY_1K / CNY_10K；有 volume 时必须明确 shares / lots100。规范化后金额统一元、成交量统一股；不要再用 amount_multiplier 重复换算。

代码需要明确交易所，支持 `000001.SZ`、`SZ000001`、`600000.XSHG` 等转换；不猜纯数字代码。重复日期/代码、映射重名、非法 OHLC、负数/无穷值、缺复权列会失败。只提供前复权 OHLC 会被拒绝；研究前复权基于原始 OHLC 与 cumadj 按历史时点计算。

可传 `expected_sessions`（官方交易日期列表）补齐全市场漏掉的日期行，使延迟和滚动窗口不会跨过缺失交易日。补齐行保留 NaN，不能被撮合。提供此列表只表示已显式采用日历；完整股票历史、停复牌和退市覆盖仍须核对供应商。

```powershell
python run.py import-data --source-spec examples/tabular_source.json --output data/vendor_snapshot
python run.py inspect --data data/vendor_snapshot
```

source-spec 路径相对 JSON 文件；示例中的 vendor_daily.csv 需要替换为真实文件。也可以 `source.load().export("data/new_snapshot")` 保存独立快照。不得覆盖已有目录。

### QMT / miniQMT

```python
from pkuquant import QMTSource, Research

source = QMTSource(["000001.SZ", "600000.SH"], "2020-01-02", "2023-12-29",
                   volume_unit="lots100")  # 必须按本地 SDK 数据字典确认单位
data = source.load()
data.export("data/qmt_snapshot")
result = Research(data).run("rank(ts_return(close, 20))",
                            require_trade_counts=False, end="2023-12-29")
```

调用 `get_market_data_ex` 读取 none 原始价和 back_ratio 等比后复权价，按两者收盘价之比还原乘法累计因子，禁止自动填充停牌行情。SDK 历史数据须先补全；接口不自动下载或登录，不使用交易账户。基于同一 QMT 行情 API 的券商可以复用，其他券商需实现 DataSource；**不是所有券商都已逐家适配认证**。此处完成了模拟 SDK 合约测试，未连接用户的真实券商终端。[官方接口说明](https://dict.thinktrader.net/nativeApi/xtdata.html)。

### Tushare（数据服务，不是券商）

```python
import os, tushare as ts
from pkuquant import TushareSource
client = ts.pro_api(os.environ["TUSHARE_TOKEN"])
source = TushareSource(client, ["000001.SZ"], "2020-01-02", "2023-12-29")
data = source.load()
```

读取 daily + adj_factor；金额千元转元、成交量手转股，单次达到 6000 行则拒绝，要求拆分区间。网络异常直接失败，不静默返回部分股票；供应商提前截断、权限不足、缺失股票和缺日仍需结合日历及证券主表核验。本项目没有保存 Token，也未做有凭据的远程联调。[Tushare 日线官方字段及限额](https://tushare.pro/document/2?doc_id=27)。

## 官方逐日规则

使用 README 中的 trading_states 格式，可额外给 `price_tick`（默认 0.01）。`require_verified_rules=True` 时，所选回测区间内所有有收盘报价的证券日都必须有规则，否则开始前失败；不是只检查最终成交股票。传入规则表示使用者核验过，软件不凭 CSV 形式证明其官方真实性。

`is_st` 与 `upper_limit/lower_limit` 是不同概念。ST 不自动等于 5%；板块、日期、上市特殊期间等都需要逐日规则。默认估计撮合仍保留明确标记，不能替代该数据。
