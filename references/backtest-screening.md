# 个股筛选因子回测指南

## 回测逻辑

筛选型因子不是截面打分，而是 **条件筛选**（如"涨停+换手20-40%+10元以下"），选出满足条件的个股组合，考察组合绩效。

## 核心指标

| 指标 | 含义 |
|------|------|
| 年化绝对收益 | 组合年化收益率 |
| 年化超额收益 | 组合 - 基准 年化 |
| 胜率 | 单笔交易正收益占比 |
| 相对基准胜率 | 单笔交易跑赢基准占比 |
| 最大回撤 | 组合净值最大回撤 |
| 超额最大回撤 | 超额净值最大回撤 |
| Calmar | 年化收益/最大回撤 |
| 信息比率 | 超额年化/超额波动 |

## 回测框架

```python
import pandas as pd
import numpy as np

def backtest_screening(signals, stock_returns, benchmark_returns,
                       hold_days=1, max_stocks=10, equal_weight=True):
    """
    signals: DataFrame, columns=['date','stock_id'], 每行=一个买入信号
    stock_returns: pivot table, index=date, columns=stock_id, values=日收益率
    benchmark_returns: Series, index=date, values=基准日收益率
    hold_days: 持仓天数
    max_stocks: 最大同时持仓数
    equal_weight: 等权分配
    
    Returns: dict of performance metrics
    """
    trading_dates = sorted(stock_returns.index)
    date_idx = {d: i for i, d in enumerate(trading_dates)}
    
    # 构建每日持仓
    daily_holdings = {}  # date -> list of stock_ids
    
    for _, row in signals.iterrows():
        entry_date = row['date']
        stock = row['stock_id']
        
        if entry_date not in date_idx:
            continue
        
        start_i = date_idx[entry_date] + 1  # T+1买入
        end_i = min(start_i + hold_days, len(trading_dates))
        
        for i in range(start_i, end_i):
            dt = trading_dates[i]
            if dt not in daily_holdings:
                daily_holdings[dt] = []
            daily_holdings[dt].append(stock)
    
    # 计算组合日收益
    port_rets = []
    bench_rets = []
    
    for dt in trading_dates:
        if dt in daily_holdings and daily_holdings[dt]:
            stocks = daily_holdings[dt][:max_stocks]
            rets = [stock_returns.loc[dt, s] for s in stocks 
                    if s in stock_returns.columns and not np.isnan(stock_returns.loc[dt, s])]
            if rets:
                port_ret = np.mean(rets) if equal_weight else np.mean(rets)
                port_rets.append(port_ret)
                bench_rets.append(benchmark_returns.get(dt, 0))
    
    if not port_rets:
        return None
    
    port_rets = pd.Series(port_rets)
    bench_rets = pd.Series(bench_rets)
    excess = port_rets - bench_rets
    
    n_years = len(port_rets) / 252
    port_cum = (1 + port_rets).cumprod()
    excess_cum = (1 + excess).cumprod()
    
    # 绩效指标
    annual_ret = port_cum.iloc[-1] ** (1/n_years) - 1
    annual_vol = port_rets.std() * np.sqrt(252)
    sharpe = annual_ret / annual_vol if annual_vol > 0 else 0
    
    peak = port_cum.cummax()
    max_dd = ((port_cum - peak) / peak).min()
    
    excess_annual = excess_cum.iloc[-1] ** (1/n_years) - 1
    excess_peak = excess_cum.cummax()
    excess_max_dd = ((excess_cum - excess_peak) / excess_peak).min()
    excess_vol = excess.std() * np.sqrt(252)
    
    return {
        'annual_ret': annual_ret,
        'annual_vol': annual_vol,
        'sharpe': sharpe,
        'max_dd': max_dd,
        'calmar': annual_ret / abs(max_dd) if max_dd != 0 else 0,
        'excess_annual_ret': excess_annual,
        'excess_max_dd': excess_max_dd,
        'excess_sharpe': excess_annual / excess_vol if excess_vol > 0 else 0,
        'win_rate': (port_rets > 0).mean(),
        'win_rate_vs_bench': (port_rets > bench_rets).mean(),
        'total_trades': len(signals),
    }
```

## 基准选择

根据筛选条件的股票域自动选择基准：
- 大盘股条件 → HS300
- 中盘股条件 → ZZ500  
- 小盘股条件 → ZZ1000
- 无市值限制 → 等权全A

指数收益用 `data_loader.load_indices()` 获取（覆盖期取决于使用者的数据源）；
个股收益用 `data_loader.load_stock_market()`（Factor DB，pct_chg 为百分比口径）。
市值数据（如需按市值选基准）在 `market.stock_equity`（tot_share_cap 等字段）。

## 输出

每个筛选因子 × 基准 = 一行记录，存入 `factor_screening_perf` 表。
字段名与表列对齐（factor_id/benchmark/frequency/annual_ret/...），
便于 `save_to_db.py` 直接导入。
