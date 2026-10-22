# 表达式语义

本地语义以此文件及测试为准，不承诺 WorldQuant Fast Expression 完全兼容。

| 字段/算子 | 含义 |
|---|---|
| open/high/low/close | 价格面板；时序操作按当前行累计因子对历史值前复权 |
| amt | 原始成交金额，单位待供应商确认；容量乘数在设置中指定 |
| num_trades | 原始成交笔数字段，不是成交股数 |
| returns | `close / ts_delay(close,1) - 1` |
| rank(x) | 当日合格股票池内升序百分位，平均并列秩；范围 (0,1] |
| zscore(x) | 当日合格池内均值/样本标准差标准化；零方差为缺失 |
| scale(x) | 当日合格池内除以绝对值之和；不是持仓构建指令 |
| ts_delay(x,d) / delay | 正数 d 行滞后；价格历史值对齐到当前复权基准 |
| ts_delta(x,d) / delta | 当前值减 d 行前值 |
| ts_return(x,d) | 当前值除以 d 行前值减 1 |
| ts_mean/ts_sum/ts_min/ts_max(x,d) | 完整 d 行窗口；缺一个值则结果缺失 |
| ts_std(x,d) / ts_std_dev | 完整窗口样本标准差（ddof=1） |
| ts_rank(x,d) | 当前值在完整历史窗口中的百分位秩，平均并列秩 |
| ts_corr(x,y,d) | 完整窗口相关系数；零方差为缺失 |
| decay_linear(x,d) | 由旧到新权重 1..d 的归一化加权平均 |
| where(condition,a,b) | 按条件选择；缺失条件当 False |
| trade_when(trigger,alpha,exit) | exit>0 优先清除值；否则 trigger>0 更新；否则保持前值 |
| clip(x,lo,hi) | 数值截断，不是投资组合权重截断 |
| abs/sign/log | 绝对值/符号/对数；log 只接受无价格量纲的正比值 |

支持 `+ - * / **`、`> >= < <= == !=`、`& | ~`；布尔组合要加括号，不能用 Python `and`/`or`。数值除零和无穷结果转为缺失。缺失因子不入选，不自动填成零。

可先赋值再返回最终表达式：

```text
x = ts_return(close, 20);
y = ts_std(returns, 60);
rank(x) - rank(y)
```

窗口必须为 1—5000 的整数，禁止负数和零滞后。幂指数限制在 [-8,8]。表达式不允许属性、下标、任意函数、关键字调用、循环、文件读写或导入。字段和内置函数名不能被覆盖。

价格量纲传播：两个价格相除后为无量纲，乘法/除法按次数传播；加减必须具有同样的价格维度。跨截面 rank/zscore 会去除价格量纲。示例：

```text
close / ts_delay(close, 20) - 1  # 正确，处理窗口内除权
log(close / ts_delay(close, 1))  # 正确
log(close)                     # 拒绝：先构造无量纲比值
close + rank(amt)               # 拒绝：价格与分数混加
```

`rank(close)` 按当日原始价格排序，因为前复权锚在当日；不同股票的价格水平不是估值或规模。所有跨截面算子只看当天 eligible 股票池；时间序列算子保留历史观测，不根据未来是否合格删除过去数据。

日线时间窗口按输入交易日期的行数定义，不是自然日。没有未来标签字段；未来收益仅在 `diagnostics.py` 生成，并与策略入口隔离。
