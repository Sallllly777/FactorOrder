# 没有行情文件时运行网页演示

先在项目根目录安装和生成合成快照：

```bash
python -m pip install -e ".[service,test]"
python examples/provider_demo.py --output outputs/provider_demo
python start_ui.py --config examples/service.demo.json
```

使用启动器显示的临时密钥登录，选择 `synthetic_demo`。将表达式设为 `rank(ts_return(close, 2))`，设置以下参数：

| 设置字段 | 值 |
| --- | --- |
| 开始 / 结束日期 | 2023-08-01 / 2023-08-31 |
| min_history / active_window / liquidity_window / valid_price_window | 1 / 1 / 1 / 1 |
| liquidity_quantile / min_price / min_avg_amount | 0 / 0 / 0 |
| top_n / retain_n / max_weight / gross_exposure | 2 / 2 / 0.5 / 1 |
| rebalance / delay | weekly / 1 |
| unknown_st / require_verified_rules | error / true |

运行后查看任务、净值与订单。合成快照只有两只证券和一个月数据，默认真实研究模板的 60 日预热与 2025 年日期不适用，须先修改设置。CLI 演示已直接使用适配的设置并生成报告，不需要手工操作。

这里的价格、工作日日历、ST 事件和无限价规则全部为软件测试构造，不能当作真实市场数据或策略证据。若要重复演示，使用新的 `--output` 路径，并同步修改配置中的快照路径，不覆盖旧记录。
