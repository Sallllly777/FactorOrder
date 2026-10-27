# 因序 · FactorOrder

**因子批量算，交易逐日走。** 面向沪深 A 股日频做多研究的 Python 回测平台，提供中文网页工作台、轻量表达式、执行审计和可扩展的数据源接口。

这是可运行的研究与私有服务版本（v0.6.1），尚未完成真实券商、生产服务器与实盘验收。GitHub 项目名为 FactorOrder；为保持现有策略兼容，Python 导入和安装包名称仍为 `pkuquant` / `pkuquant-backtest`，命令行同时支持 `factororder` 和 `pkuquant`。本仓库没有在 PyPI 发布声明。

## 因子评价与评分

新增延迟开盘标签、逐期限 IC / Top-N / 分层诊断、样本审计、HAC 与区块 bootstrap、同条件基线、可选压力测试和同伴组合增量对照。网页通过“回测设置 → 因子评价协议”配置，在“因子评价”页查看分项依据并下载完整证据。Python 使用 `Research.run(..., evaluation=EvaluationSpec(...))`，CLI 策略文件新增 `evaluation`。

100分框架按预测25、经济25、稳健20、实施15、增量15组织。缺失项不补零、不重新归一化；样本不足不评分，证据未齐不发布总分。阈值为尚未独立校准的研究规则。见 [评价协议、公式和限制](docs/factor_evaluation.md) 与 [可运行配置示例](examples/evaluation_reversal.json)。历史报告保持原样，旧收盘标签与新开盘标签分开展示。

v0.6.1补充训练—验证稳定性：在评价协议填写四个日期，并让整体回测区间覆盖两段。新增6分分别衡量 IC、Top-N 超额和扣费后基线超额的保留程度；同时显示差值、衰减与逐年对照，原区间内稳定性降至2分。未配置日期、训练参考值过小或某验证年份样本不足时，相应分项保留未评价。新旧评分版本不直接横比。

## 三个示例因子

独立目录 **[example_factors](example_factors/README.md)** 包含策略配置、综合报告、训练/验证报告、逐日资金曲线、委托和持仓账本、因子诊断及校验和。

| 示例 | 训练累计净收益 | 验证累计净收益 | 验证最大回撤 |
| --- | ---: | ---: | ---: |
| [日内5日反转](example_factors/intraday_5d/README.md) | 62.52% | 17.65% | -29.78% |
| [日内10日反转](example_factors/intraday_10d/README.md) | 122.13% | 21.93% | -27.37% |
| [日内20日反转](example_factors/intraday_20d/README.md) | 96.66% | 34.50% | -28.61% |

训练实际为 2021-01-04 至 2024-12-31，验证为 2025-01-02 至 2026-10-15；每段独立以 100 万元开始。三者来自同一设置下六个信号配置的事后比较，验证期已反复查看，不能称为独立盲测。三个窗口是同一家族的变体，不代表独立的分散化收益来源。

基础 5DR 继续作为默认研究基线：验证收益 **14.66%**、最大回撤 **-38.68%**。其验证期未来 1/5/20 日平均 Rank IC 分别为 **0.030944 / 0.028983 / 0.039312**。**5DR 的 5 是回看窗口，Rank IC 是排序相关性，均不等于预测正确率。** 详细定义、样本数量和执行时点差异见 [5DR 预测水平说明](example_factors/prediction_horizons.md)。

下载后打开 `example_factors/report.html` 可离线浏览图表；GitHub 文件页可直接阅读 Markdown 表格。未配置 GitHub Pages。

## 安装与无需真实数据的演示

建议 Python 3.13；声明支持 Python 3.10+，其他版本/平台应以 CI 和本机测试为准。在仓库根目录：

```bash
python -m venv .venv
```

Windows PowerShell 激活：`.\.venv\Scripts\Activate.ps1`；Linux/macOS：`source .venv/bin/activate`。然后：

```bash
python -m pip install -e ".[service,test]"
python run.py operators
python examples/provider_demo.py --output outputs/provider_demo
```

打开 `outputs/provider_demo/report/report.html`。演示行情、交易日历和 ST 事件均为**虚构数据，仅验证接口和软件**。输出目录必须不存在，避免覆盖已有记录。

网页演示：

```bash
python start_ui.py --config examples/service.demo.json
```

浏览器打开本机 `http://127.0.0.1:8000`，输入终端显示的临时密钥。网页保留真实研究模板；用于短小合成数据的表达式和设置见 [演示步骤](docs/quickstart_demo.md)。密钥不写入仓库，服务停止后本次自动密钥失效。

## 使用自己的行情

原始研究的 `feathers.zip` **不包含在仓库中**，其他使用者需自行准备可用且有权限的数据。将同一原始数据放在仓库根目录后可以使用：

```bash
python run.py inspect --data feathers.zip
python run.py run --data feathers.zip --strategy example_factors/intraday_5d/strategy.json
python start_ui.py --config examples/service.json
```

也可通过 CSV / Parquet / QMT / Tushare 适配器或自定义 Provider 接入。服务配置仅由管理员登记快照路径，不接受网页任意路径或上传 Python。配置内相对路径基于配置文件所在目录。

原研究数据共 1643 个日期 × 5451 只证券，2020-01-02 至 2026-10-16。末日成交笔数整行缺失，研究实际截至 10 月 15 日。缺少历史 ST、官方逐日交易规则和已确认单位；详见 [数据限制与来源哈希](example_factors/README.md)。具备接入接口不表示这些信息已经补齐。

## 策略代码

```python
from pkuquant import Research, Settings

lab = Research("feathers.zip")
settings = Settings(
    start="2025-01-02", end="2026-10-16",
    min_history=60, active_window=1,
    top_n=20, retain_n=20, max_weight=0.0475,
    gross_exposure=0.6, rebalance="daily", delay=1,
)
result = lab.run("-rank(ts_sum(log(close / open), 5))", settings)
print(result.summary())
result.save("outputs/my_research")
```

此短例展示 API，未包含示例研究的全部过滤与费用；要复现表中结果，应使用 `example_factors/*/strategy.json` 完整配置：

```bash
python scripts/run_example_factors.py --data feathers.zip --factor all --period both
```

脚本分别执行训练和验证，保留预热历史但不继承持仓，输出到新目录；完整数据批量运行可能占用较多时间和内存。变更数据文件会改变结果，脚本默认核对原始 ZIP 哈希，使用其他数据需显式 `--allow-different-data` 并作为新的研究记录。

## 能力与边界

- 受限 AST 因子表达式、批量矩阵运算、逐日撮合；支持可信本地 Python 回调，网页只接收受限表达式。
- 收盘信号至少延迟一日；前收定量、事前容量、T+1、整手、费用取整、停牌和涨跌停拒单，保留拒绝原因。
- 历史 ST 支持生效时间与可得时间，执行前复核；未知数据可选择允许、排除或报错。
- Provider 将行情、日历、历史状态与交易规则封装为带校验和的 DatasetBundle。
- 单租户鉴权 API、SQLite 持久化队列、独立研究进程、中文 UI、报告下载。私有部署仅支持单实例/单 worker。

账户为复权经济单位，隐含公司行动调整和再投资；没有真实股份与现金权益账本。日线不能证明开盘成交量或队列成交。当前没有实盘下单网关，也不宣称是 WorldQuant BRAIN 的官方或兼容实现。收益改善不能证明引擎更准确，准确性还需数据、逐笔账本与独立测试支持。

## 验证和文档

```bash
python -m unittest discover -s tests -v
python scripts/verify_examples.py
python -m pip wheel --no-deps . --wheel-dir dist
```

测试使用合成数据，不需要私有行情或券商账号。公开结果核验脚本不依赖第三方库。GitHub Actions 已配置 Python 3.13 的 Windows/Linux 测试与打包流程，其远端运行需上传后才会发生。

[算子语义](docs/operators.md) · [数据源与ST](docs/data_sources.md) · [Provider接口](docs/provider_contract.md) · [网页说明](docs/web_ui.md) · [私有部署](docs/deployment.md) · [GitHub上传](docs/github_upload.md) · [版本说明](CHANGELOG.md)

仓库未指定开源许可证；整理本地 Git 不代表已经选择对外授权条款。

[基础5DR完整报告](example_factors/baseline_5dr_report.html) · [信号隔离、验证污染与日线撮合偏差](docs/research_integrity.md)
