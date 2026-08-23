# 因子 Builder API（给实现者/子代理）

每个因子一个 builder 函数，放在 `scripts/builders/<书码>.py`，用 `BUILDERS` 字典注册。
引擎 `scripts/factor_engine.py` 负责数据加载、回测、绘图；builder 只负责把因子规则算出来。

## 骨架

```python
"""<书名> 因子 builders"""
import numpy as np
import pandas as pd

BUILDERS = {}

def register(fid):
    def deco(fn):
        BUILDERS[fid] = fn
        return fn
    return deco

@register('DYZ-F02')
def build_dyz_f02(ctx):
    mkt = ctx['mkt']
    sig = mkt['n_ld']                      # 跌停家数
    return {
        'type': 'timing',
        'signal': sig,
        'long_thresh': 30, 'short_thresh': None, 'direction': 'high',
        'bench': 'ZZ1000',
    }
```

**factor_id 必须与 SQLite factor_meta 中的 ID 一致**（书码-F 编号，见
`scripts/aggregate_factors.py` 的 BOOK_CODES）。

## ctx 字段

| 字段 | 内容 |
|------|------|
| `ctx['sm']` | 个股日行情长表 (date, stock_id, open/high/low/close/volume/amt/pct_chg/adj_close/max_updown_close/trade_status/is_lu/is_ld/is_yizi/consec)，已按 stock_id,date 排序，已滤 trade_status==1 |
| `ctx['piv']` | 常用 pivot 字典: open/high/low/close/volume/amt/pct_chg/adj_close/is_lu/is_ld/consec，均为 index=date, columns=stock_id 的 DataFrame |
| `ctx['ret']` | = piv['pct_chg']，日收益（**百分比口径**，1.5 = 1.5%） |
| `ctx['mkt']` | 市场级情绪序列 (index=date): n_lu 涨停家数 / n_ld 跌停家数 / n_natural_lu 自然涨停 / max_consec 最高连板 / n_consec_ge2 连板家数 / amt_sum 全市场成交额 / n_up 上涨家数 / n_dn 下跌家数 / n_gt5 涨幅≥5%家数 / lu_next_ret 昨日涨停今日平均收益(%) / lu_win_rate 赚钱效应 / jinji_rate 晋级率 |
| `ctx['indices']` | {HS300/ZZ500/ZZ1000/GZ2000: DataFrame(close, ret)} |
| `ctx['bench_ret']` | {代码: 基准日收益 Series(小数)} |
| `ctx['citic']` | {行业名: DataFrame(close, ret)}，30 个中信一级 |
| `ctx['citic_ret']` / `ctx['citic_close']` | 行业收益/收盘 pivot (index=date, columns=行业名) |
| `ctx['INDEX_CODES']` | {代码: (Wind代码, 中文名)} |
| `ctx['stock_ind']` | 个股→中信一级行业名 Series (index=stock_id)，最新快照静态近似 |
| `ctx['mktcap']` | 流通市值 pivot (index=date, columns=stock_id)，来自 stock_equity.float_share_cap；**可能为 None，用前判空** |
| `ctx['ind_onehot']` | 个股×行业 one-hot DataFrame (index=stock_id, columns=行业名)。行业聚合: `pd.DataFrame(个股因子pivot.values @ oh.values, index=pivot.index, columns=oh.columns)`；反向映射: `oh.values.T` |

## 各类型返回约定

### timing 择时
```python
{'type': 'timing',
 'signal': pd.Series(index=date),   # 信号值
 'long_thresh': x,                  # direction='high': 信号>x 做多
 'short_thresh': y 或 None,         # 信号<y 做空 (None=空仓)
 'direction': 'high' | 'low',       # 'low': 信号<x 做多, >y 做空
 'bench': 'ZZ1000'}                 # HS300/ZZ500/ZZ1000/GZ2000

# 多档仓位规则（如 80%/50%/0% 仓位阶梯）: 改用 position 直给目标仓位, 不再传 signal/thresholds
{'type': 'timing',
 'position': pd.Series(index=date), # 目标仓位 (0~1 或含 -1), 引擎滞后 1 天执行
 'bench': 'ZZ1000'}
```
引擎自动信号滞后 1 天（防前视），出图：策略净值 vs 基准净值 + 回撤。

### cross_section 截面
```python
{'type': 'cross_section',
 'factor': DataFrame(index=date, columns=stock_id),  # 因子值 pivot
 'freq': 'monthly' | 'weekly' | 'daily',             # 调仓频率
 'universe': '全A'}                                   # 可省
```
出图：IC 序列 + 累计 IC + 十分组净值。

### rotation 轮动
```python
{'type': 'rotation',
 'factor': DataFrame(index=date, columns=行业名),    # 列名须与 ctx['citic'] 键一致
 'top_n': 5, 'bottom_n': 5}                          # 可省, 默认 5/5
```
引擎按日频调仓（今日信号→明日收益），出图：多头净值 vs 等权基准 + 超额净值。

### screening 筛选
```python
{'type': 'screening',
 'signals': DataFrame(columns=['date','stock_id']),  # 每行一个买入信号(T日收盘产生)
 'hold_days': 5,          # 持有天数 (T+1 起算)
 'bench': 'ZZ1000',       # 基准
 'max_stocks': 50}        # 单日最大持仓 (可省)
```
出图：组合净值 vs 基准 + 超额净值 + 回撤。
**注意**：引擎对单日超 max_stocks 的信号按传入顺序截断——大池子因子务必在
builder 内先按强度降序排序（保留每日最强前 N 个），别让顺序随机截断。

## 纪律

0. **可成交口径（引擎统一处理，builder 无需改动）**：
   截面进场价 = T+1 开盘并剔除 T+1 一字涨停股；筛选剔除 T+1 一字板信号；
   择时/筛选基准 ZZ1000 重映射为 ZZ2000（中证2000），大盘类用 HS300。
   builder 不需自行做可成交性剔除。
1. **不可实现的因子**：`raise NotImplementedError('原因')`（如依赖龙虎榜/封单/分钟数据）。
   引擎会记录到失败清单，不出图。
2. **防前视**：因子值只能用 T 日及之前数据；引擎对择时信号统一滞后 1 天，
   截面/轮动用「T 因子 → T+1 期收益」，筛选 T+1 进场。
3. **口径**：pct_chg 是百分比；ctx['ret']/piv 均为百分比。mkt 的 lu_next_ret 也是百分比。
4. **性能**：尽量用 ctx['piv'] 现成 pivot 做向量化运算，避免对 sm 再做 pivot_table（很贵）。
   不要在 builder 里重新加载数据。
5. **涨停口径**：is_lu 已含主板近似（max_updown_close==1 或 pct_chg>=9.5）。
   创业板 20cm 如需区分，自行用 pct_chg>=19.5 细化。
6. builder 文件必须可独立 `exec`（不 import 项目模块；np/pd 已可用）。
7. 写完用以下命令自测（数据已缓存，秒级~分钟级）：
   ```bash
   /usr/bin/python3 scripts/factor_engine.py --only <书码>-F01,<书码>-F02 --start 2022-01-01 --end 2024-06-30
   ```
