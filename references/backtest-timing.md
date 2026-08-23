# 择时因子回测指南

## 回测逻辑

择时因子产生 **信号值**（如赚钱效应>0.5做多，<0.3做空），对指数进行多空切换。

## 标的指数

| 代码 | 名称 | Wind 代码 | 数据来源 | 日期范围 |
|------|------|-----------|----------|----------|
| HS300 | 沪深300 | 000300.SH | 数据接口 | 视数据源 |
| ZZ500 | 中证500 | 000905.SH | 同上 | 2004~2026.07 |
| ZZ1000 | 中证1000 | 000852.SH | 同上 | 2004~2026.07 |
| GZ2000 | 国证2000 | 399303.SZ | 同上 | 2009~2026.07 |

**注意**: 各指数起始年份不同，回测取信号与指数的重叠区间（脚本自动对齐）。

## 数据加载方式

用项目数据层 `scripts/data_loader.py`：

```python
import sys
sys.path.insert(0, 'scripts')   # 或从 scripts/ 目录直接 import
from data_loader import load_indices

# dict {HS300: df, ZZ500: df, ZZ1000: df, GZ2000: df}
# 每个 df: index=date, columns=[close, ret]，全历史缓存在 data_cache/indices.pkl
indices = load_indices('2010-01-01', '2024-12-31')
hs300_ret = indices['HS300']['ret']
```

市场级信号（如涨停家数、赚钱效应）来自个股行情聚合，见
`backtest-cross-section.md` 的 `load_stock_market`。

## 回测框架

```python
def backtest_timing(signal_series, index_ret, long_threshold, short_threshold=None):
    """
    signal_series: pd.Series, index=date, value=信号值
    index_ret: pd.Series, index=date, value=指数日收益率
    long_threshold: 信号>此值做多
    short_threshold: 信号<此值做空(None=空仓)

    Returns: dict of performance metrics
    """
    aligned = pd.DataFrame({'signal': signal_series, 'ret': index_ret}).dropna()

    # 信号滞后1天(避免前视偏差)
    aligned['signal_lag'] = aligned['signal'].shift(1)
    aligned = aligned.dropna()

    # 仓位
    aligned['position'] = 0
    aligned.loc[aligned['signal_lag'] > long_threshold, 'position'] = 1
    if short_threshold is not None:
        aligned.loc[aligned['signal_lag'] < short_threshold, 'position'] = -1

    # 策略收益
    aligned['strat_ret'] = aligned['position'] * aligned['ret']
    aligned['cum_ret'] = (1 + aligned['strat_ret']).cumprod()
    aligned['bench_cum'] = (1 + aligned['ret']).cumprod()

    # 绩效指标
    n_years = len(aligned) / 252
    total_ret = aligned['cum_ret'].iloc[-1] - 1
    annual_ret = (1 + total_ret) ** (1/n_years) - 1
    annual_vol = aligned['strat_ret'].std() * (252**0.5)
    sharpe = annual_ret / annual_vol if annual_vol > 0 else 0

    # 最大回撤
    peak = aligned['cum_ret'].cummax()
    dd = (aligned['cum_ret'] - peak) / peak
    max_dd = dd.min()

    # 胜率
    trading_days = aligned[aligned['position'] != 0]
    win_rate = (trading_days['strat_ret'] > 0).mean() if len(trading_days) > 0 else 0

    return {
        'annual_ret': annual_ret,
        'max_dd': max_dd,
        'sharpe': sharpe,
        'win_rate': win_rate,
        'long_days_pct': (aligned['position'] == 1).mean(),
        'signal_count': (aligned['position'].diff().abs() > 0).sum(),
        'benchmark_annual_ret': ((1+aligned['ret']).prod())**(1/n_years) - 1,
    }
```

## 输出要求

每个择时因子 × 每个指数 = 一行绩效记录，存入 `factor_timing_perf` 表。
字段名与表列对齐（factor_id/index_code/index_name/long_annual_ret/...），
便于 `save_to_db.py` 直接导入。完整示例见 `scripts/backtest_yangj.py`。
