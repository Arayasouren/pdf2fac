# 截面选股因子回测指南

## 回测逻辑

截面因子：每期在全部/子集股票上计算因子值，按因子值分组，考察分组收益差异。

## 核心指标

| 指标 | 含义 | 优秀标准 |
|------|------|----------|
| IC_mean | Rank IC均值 | \|IC\| > 0.03 |
| ICIR | IC/IC_std | \|ICIR\| > 0.5 |
| 覆盖率 | 有因子值的股票占比 | > 80% |
| 单调性 | 分组收益是否单调 | > 0.8 |
| 多空收益 | G5-G1(或G1-G5)年化 | > 10% |

## 数据加载

用项目数据层 `scripts/data_loader.py`（由使用者在 data_source.py 中接入数据源）：

```python
import sys
sys.path.insert(0, 'scripts')
from data_loader import load_stock_market

# 全A日行情, 按月分块拉取 + pickle 缓存 (首次慢, 之后秒回)
# 列: date/stock_id/open/high/low/close/volume/amt/pct_chg/
#     adj_close/adj_factor/max_updown_close/trade_status
sm = load_stock_market('2010-01-01', '2024-12-31')
sm = sm[sm['trade_status'] == 1]          # 剔除停牌/指数行
ret = sm.pivot_table(index='date', columns='stock_id', values='pct_chg')
```

指数成分股标记（选股域子集）用 `market.stock_status`（有数据，含
hs300/zz500/zz1000 成分哑变量和权重）：

```python
from data_loader import _mysql_query
hs300 = _mysql_query("""
    SELECT date, stock_id FROM stock_status
    WHERE hs300 = 1 AND date BETWEEN '2024-01-01' AND '2024-12-31'
""")
```

交易日历可用 `market.trading_calendar`（有数据），或从 stock_market
提取 `DISTINCT date`。

## IC分析框架

```python
from scipy import stats

def calc_ic_series(factor_df, return_df, method='rank'):
    """
    factor_df: DataFrame, index=date, columns=stock_id, values=factor_value
    return_df: DataFrame, index=date, columns=stock_id, values=next_period_return
    method: 'rank' for Rank IC (Spearman), 'normal' for Pearson IC

    Returns: (IC序列, 覆盖率序列)
    """
    common_dates = factor_df.index.intersection(return_df.index)
    ic_series = {}
    coverage = {}

    for dt in common_dates:
        f = factor_df.loc[dt].dropna()
        r = return_df.loc[dt].dropna()
        common = f.index.intersection(r.index)

        if len(common) < 30:
            continue

        coverage[dt] = len(common) / len(r)

        if method == 'rank':
            ic, _ = stats.spearmanr(f[common], r[common])
        else:
            ic, _ = stats.pearsonr(f[common], r[common])

        ic_series[dt] = ic

    return pd.Series(ic_series), pd.Series(coverage)

def calc_group_returns(factor_df, return_df, n_groups=5):
    """按因子值分组, 计算各组平均收益"""
    common_dates = factor_df.index.intersection(return_df.index)
    group_rets = {g: [] for g in range(1, n_groups+1)}

    for dt in common_dates:
        f = factor_df.loc[dt].dropna()
        r = return_df.loc[dt].dropna()
        common = f.index.intersection(r.index)

        if len(common) < n_groups * 10:
            continue

        f_common = f[common]
        r_common = r[common]

        # 等频分组
        labels = pd.qcut(f_common.rank(method='first'), n_groups, labels=range(1, n_groups+1))

        for g in range(1, n_groups+1):
            mask = labels == g
            group_rets[g].append(r_common[mask].mean())

    return {g: pd.Series(rets) for g, rets in group_rets.items()}
```

## 调仓频率说明

- **日频因子**: 每日计算IC, 用次日收益率
- **周频因子**: 每周五计算, 用下周收益率
- **月频因子**: 每月末计算, 用下月收益率

频率在因子提炼时根据因子特性标记，回测时对齐。
注意 `stock_market.pct_chg` 是**百分比**（1.5 = 1.5%），计算累计收益时除以 100
（`backtest_yangj.py` 中有自动量纲探测，可复用）。

## 输出

每个截面因子 × 每个选股域 = 一行绩效记录，存入 `factor_cs_perf` 表。
字段名与表列对齐（factor_id/universe/frequency/ic_mean/.../monotonicity），
便于 `save_to_db.py` 直接导入。完整示例见 `scripts/backtest_yangj.py`。
