# 未来券商与数据源接入：v0.3 接口说明

本轮按“现在留好接口，将来再接真实券商/数据”的要求完成。当前不需要券商账号、Token 或服务器；下面的离线示例可完整运行。接口只读，不包含下单或账户资金操作。

## 接入之后，策略仍然只写一行

```python
from pkuquant import Research, DataRequest

request = DataRequest(
    symbols=("000001.SZ", "600001.SH"),
    start="2020-01-02", end="2023-12-29",
)
lab = Research.from_provider(provider, request)
result = lab.run("rank(ts_return(close, 60))",
                 start="2021-01-01", end="2023-12-29")
```

`request.start` 是取数起点，需要包含预热历史；`run(start=...)` 是策略执行起点。证券列表必须包括研究所需的历史证券，不能只用今天仍上市的名单。请求只支持本系统的沪深 A 股日频范围。

所有供应商回调在 `Research.from_provider` / `collect` 阶段调用一次。之后策略、日频交易循环和 API worker 使用已加载数据或快照，**不再请求实时接口**。没有注册的能力保留缺失状态；已注册但返回 None、数据错误或网络失败时直接报错，不默默改用其他数据源。

## 四类接口

| dataset / 回调 | 返回类型 | 必须遵守的口径 |
|---|---|---|
| bars | MarketData | 原始 OHLC + 累计复权因子；金额显式归一为元；可选真实股数 volume 与笔数 num_trades |
| calendar | pandas.DatetimeIndex | 请求范围内完整且有序、唯一的交易日期；无时区、午夜；不能用普通工作日替代真实交易日历 |
| security_history | pandas.DataFrame | code, effective_date, known_at, is_st；包含起始状态及完整变更；允许生效日期早于请求起点 |
| trading_states | pandas.DataFrame | date, code, tradable, upper_limit, lower_limit, no_limit, lot_size, min_buy_qty；可选 price_tick |

bars 必选；其余可选。接口能力声明只表示返回了这一类数据，**不等于日期/证券全覆盖，更不等于官方认证**。空 ST 表仍是未知；没有 ST 记录不能自动解释为普通股。行情日期不在显式日历中会报错；日历有、行情全缺的一天保留 NaN 行，不压缩时间轴，也不虚构报价。

ST 使用当时已知时间来避免未来信息。外部交易规则需要供应商提供对应交易日的历史规则，不能用当前 InstrumentName、当前涨跌停价或下载时间代替。`doctor` 检查覆盖与显式单位；人工核验数据出处仍不可替代。

## 最简适配器

```python
from pkuquant import FunctionProvider, from_long

def bars(request):
    frame = your_client.fetch_daily(request.symbols, request.start, request.end)
    return from_long(frame, amount_unit="CNY", volume_unit="shares",
                     provider="your-vendor")

provider = FunctionProvider(name="your-vendor", version="1", bars=bars)
```

`your_client.fetch_daily` 是需要你实现的供应商调用，**不是本项目假设存在的通用券商 API**。SDK 登录和权限配置在调用方处理，密钥不写入 ProviderInfo、请求对象或快照。字段映射与单位转换沿用 [数据源说明](data_sources.md)。

以后拥有其他数据时，追加回调：

```python
provider = FunctionProvider(
    name="your-vendor", version="2",
    bars=bars,
    calendar=fetch_calendar,
    security_history=fetch_historical_st_events,
    trading_states=fetch_daily_rules,
)
lab = Research.from_provider(provider, request,
    require=("calendar", "security_history", "trading_states"))
```

`require` 会在调用供应商前检查能力；缺少某类接口立即抛出 `MissingCapability`。它不是完整性验收，仍需 `lab.doctor(...)` 和严格设置：

```python
result = lab.run("rank(ts_return(close,60))",
                 start="2021-01-01", end="2023-12-29",
                 unknown_st="error", require_verified_rules=True)
```

已实现类也可以接入，遵守 `HistoricalProvider` Protocol：持有 `ProviderInfo`，实现 `fetch(dataset, request)`。不需要继承某个框架基类。

## 复用现有 QMT / Tushare / 文件适配器

```python
from pkuquant import SourceProvider, QMTSource

provider = SourceProvider(
    lambda r: QMTSource(r.symbols, r.start, r.end, volume_unit="lots100"),
    name="your-broker-qmt", version="1",
)
```

这里只声明行情能力。QMT 本地历史、版本、成交量单位、账号权限需将来实际确认；Tushare 和 TabularSource 也可用同一工厂形式包装。没有凭据时不导入 SDK 或触发连接。`examples/provider_template.py` 提供可复制模板；这是未来数据接入层，不是实盘交易网关。

## 保存完整快照，供 CLI / 服务复用

```python
from pkuquant import collect, Research

bundle = collect(provider, request)
bundle.save("data/vendor_snapshot_001")  # 必须是新目录
lab = Research("data/vendor_snapshot_001")
```

目录格式（schema_version=1）：

```text
bundle.json                  # 供应商名/版本、能力、请求范围与每个文件的 SHA-256
market/*.feather              # 行情矩阵
market/metadata.json          # 单位、上游来源等元信息
calendar.csv                 # 可选
security_history.csv         # 可选
trading_states.csv           # 可选
```

写入先暂存、成功后原子改名，不覆盖原有快照。加载会校验版本、文件清单、路径和 SHA-256；文件被修改、缺失或额外混入会失败。SHA-256 提供一致性检查，不是供应商数字签名。报告记录 bundle manifest 哈希、日历哈希和供应商版本。

已有本地文件也可打包：

```powershell
python run.py pack-data --data data/vendor_market --states data/daily_rules.csv --security-history data/st.csv --calendar data/calendar.csv --provider-name my-vendor --provider-version 1 --output data/vendor_snapshot_001
python run.py doctor --data data/vendor_snapshot_001
python run.py run --data data/vendor_snapshot_001 --strategy examples/low_volatility.json --end 2023-12-29
```

没有某项数据就不传对应选项，不能使用示例 CSV 冒充真实数据。`pack-data` 包装的是输入完整范围，`--start/--end` 只用于回测，不截断打包；日历若包含前后缺失交易日，快照保留这些行。行情也可通过 `--source-spec` 读 CSV/Parquet。

服务配置无需新增可执行插件入口，只把管理员登记的 data 路径指向快照：

```json
{
  "sources": {"vendor_001": {"data": "../data/vendor_snapshot_001"}},
  "root": "../service-data",
  "timeout": 1800,
  "max_queue": 8,
  "max_jobs": 1000
}
```

已有 API 请求中的 `source` 改为 `vendor_001` 即可，Alpha 不变。快照包含 ST/规则后，禁止再同时传另一个 states/security_history 覆盖它，避免悄悄混用版本。HTTP 用户不能上传代码或指定 Python import 路径。

## 现在就能运行的离线示例

```powershell
python examples/provider_demo.py --output outputs/provider_demo
```

该例以**合成**行情、虚构 ST 事件、虚构交易规则和工作日日历验证接口，生成 bundle 与 HTML 报告；没有访问券商或数据服务。其软件结构检查可以通过，但 `doctor` 会因 `synthetic=True` 拒绝把它认定为可验收的真实数据。生成目录若已存在，使用另一个输出目录。

回归测试覆盖：行情单独接入、强制能力缺失、网络错误传播、缺交易日、空 ST 表、非法证券/日期、快照往返结果一致、篡改检测、版本拒绝，以及 API worker 读取完整快照执行回测。以后写新的真实适配器时，用同一份 `collect → save → load → doctor → 小样本回测` 流程验收即可。
