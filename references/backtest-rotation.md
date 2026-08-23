# 板块轮动因子回测指南

## 回测逻辑

板块轮动因子：在行业层面计算因子值，做多因子值高的行业，做空因子值低的行业。

## 行业分类

| 分类体系 | 数据来源 | 行业数 | 状态 |
|----------|----------|--------|------|
| **中信一级** | 行业指数 (由数据接口提供) | 30 | ✅ 默认 |
| 中信行业个股标记 | Factor DB `market.stock_status` (zx_* 哑变量) | 30 | ✅ 可用于个股→行业映射 |
| 申万一级 | iFinD `history` 逐只拉取 | 31 | ⚠️ 备选(慢/有配额) |

默认用中信一级（覆盖期取决于数据源），
与 Factor DB 的 `barra_factors` 行业哑变量体系一致。

## 数据加载

用项目数据层 `scripts/data_loader.py`：

```python
import sys
sys.path.insert(0, 'scripts')
from data_loader import load_citic_l1

# dict {行业名: DataFrame(index=date, columns=[close, ret])}, 30 个中信一级行业
# 全历史缓存在 data_cache/citic_l1.pkl
ind = load_citic_l1('2010-01-01', '2024-12-31')
ind_close = pd.DataFrame({n: d['close'] for n, d in ind.items()})
ind_ret   = pd.DataFrame({n: d['ret']   for n, d in ind.items()})
```

## 回测框架

```python
def backtest_rotation(factor_values, industry_rets, top_n=5, bottom_n=5):
    """
    factor_values: DataFrame, index=date, columns=industry_name, values=因子值
    industry_rets: DataFrame, index=date, columns=industry_name, values=行业收益率
    top_n: 做多行业数
    bottom_n: 做空行业数

    Returns: dict of performance metrics
    """
    common_dates = factor_values.index.intersection(industry_rets.index)

    long_rets = []
    short_rets = []
    bench_rets = []

    for i, dt in enumerate(common_dates[:-1]):
        next_dt = common_dates[i+1]
        fv = factor_values.loc[dt].dropna()
        nr = industry_rets.loc[next_dt].dropna()
        common = fv.index.intersection(nr.index)

        if len(common) < top_n + bottom_n:
            continue

        fv_c = fv[common].sort_values(ascending=False)

        # 做多top_n, 做空bottom_n
        long_industries = fv_c.index[:top_n]
        short_industries = fv_c.index[-bottom_n:]

        long_ret = nr[long_industries].mean()
        short_ret = nr[short_industries].mean()
        bench_ret = nr.mean()

        long_rets.append(long_ret)
        short_rets.append(short_ret)
        bench_rets.append(bench_ret)

    long_rets = pd.Series(long_rets)
    short_rets = pd.Series(short_rets)
    bench_rets = pd.Series(bench_rets)

    n_years = len(long_rets) / 252

    long_cum = (1 + long_rets).cumprod()
    long_annual = long_cum.iloc[-1] ** (1/n_years) - 1

    ls_rets = long_rets - short_rets
    ls_cum = (1 + ls_rets).cumprod()

    return {
        'long_annual_ret': long_annual,
        'short_annual_ret': (1+short_rets).prod()**(1/n_years)-1,
        'long_short_ret': (1+ls_rets).prod()**(1/n_years)-1,
        'benchmark_ret': (1+bench_rets).prod()**(1/n_years)-1,
        'excess_ret': long_annual - ((1+bench_rets).prod())**(1/n_years)-1,
        'max_dd': ((long_cum - long_cum.cummax()) / long_cum.cummax()).min(),
        'win_rate': (long_rets > bench_rets).mean(),
    }
```

## 行业IC

```python
from scipy import stats

def calc_industry_ic(factor_values, industry_rets):
    """计算行业截面IC"""
    common_dates = factor_values.index.intersection(industry_rets.index)
    ic_list = []
    for i, dt in enumerate(common_dates[:-1]):
        next_dt = common_dates[i+1]
        fv = factor_values.loc[dt].dropna()
        nr = industry_rets.loc[next_dt].dropna()
        common = fv.index.intersection(nr.index)
        if len(common) >= 10:
            ic, _ = stats.spearmanr(fv[common], nr[common])
            ic_list.append(ic)
    return pd.Series(ic_list)
```

## 输出

每个轮动因子 × 行业分类 = 一行记录，存入 `factor_rotation_perf` 表
（`industry_cls` 填 `CITIC_L1`）。字段名与表列对齐，便于 `save_to_db.py`
直接导入。完整示例见 `scripts/backtest_yangj.py`。
