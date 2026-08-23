#!/usr/bin/env python3
"""
pdf2fac 因子回测+绘图引擎

四类因子统一接口:
  timing        择时: 信号→多空仓位→净值 vs bench
  cross_section 截面: IC序列/累计IC/十分组净值
  rotation      轮动: 多头净值 vs 等权基准 + 超额净值
  screening     筛选: 信号组合净值 vs 基准 + 超额 + 回撤

builder 返回约定见 references/factor-builder-api.md。
数据全部走 data_loader 的本地缓存 (需先 prefetch)。

用法:
  /usr/bin/python3 scripts/factor_engine.py --list           # 列出已注册 builder
  /usr/bin/python3 scripts/factor_engine.py --only DYZ-F01   # 跑单个因子
  /usr/bin/python3 scripts/factor_engine.py                  # 跑全部
"""

import argparse
import json
import os
import sys
import time
import traceback
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / 'scripts'))

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

plt.rcParams['font.sans-serif'] = ['Hiragino Sans GB', 'Arial Unicode MS', 'sans-serif']
plt.rcParams['axes.unicode_minus'] = False

CHART_DIR = PROJECT_ROOT / 'results' / 'charts'
CHART_DIR.mkdir(parents=True, exist_ok=True)

BT_START = '2010-01-01'
BT_END = pd.Timestamp.now().strftime('%Y-%m-%d')
LOOKBACK_DAYS = 500   # 数据 warmup 缓冲(自然日), 覆盖 MA250 等长窗口因子

# 基准重映射: 小盘/情绪类默认中证2000, 大盘类用沪深300
BENCH_REMAP = {'ZZ1000': 'ZZ2000'}

# 择时执行口径: T 日收盘出信号, T+1 开盘成交, 双边千三成本
#   'open'  = 可执行口径: 当日收益 = 旧仓×隔夜 + 新仓×日内 - fee×|Δ仓位|
#   'close' = 旧口径: 视为信号日收盘价成交, 不扣费 (偏乐观, 仅供对比)
TIMING_EXEC = 'open'
TIMING_FEE_ONE_SIDE = 0.0015   # 双边千三 → 单边 0.15%, 按 |Δ仓位| 计

# ============================================================
# 绩效工具
# ============================================================
def annual_ret(cum):
    if cum is None or len(cum) == 0:
        return np.nan
    n = len(cum) / 252
    return cum.iloc[-1] ** (1 / n) - 1 if n > 0 and cum.iloc[-1] > 0 else -1

def max_dd(cum):
    return ((cum - cum.cummax()) / cum.cummax()).min()

def sharpe(r):
    return r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0


# ============================================================
# 数据上下文 (一次加载, 所有 builder 共享; pivot 用 float32 省内存)
# ============================================================
_ctx_cache = None


def _build_onehot(stock_ids, stock_ind):
    """个股×中信一级行业 one-hot DataFrame (index=stock_id, columns=行业名)。"""
    inds = stock_ind.reindex(stock_ids)
    oh = pd.get_dummies(inds).astype(np.float32)
    oh.index = stock_ids
    return oh


def _try_mktcap(start, end):
    """市值 pivot (无缓存则返回 None, builder 需自行判空)"""
    try:
        from data_loader import load_stock_equity
        eq = load_stock_equity(start, end, verbose=False)
        if len(eq) == 0:
            return None
        return eq.pivot_table(index='date', columns='stock_id', values='float_share_cap')
    except Exception as e:
        print(f'[ctx] 市值数据不可用: {e}', flush=True)
        return None


_BIN_CACHE = None  # 延迟到函数内取 PROJECT_ROOT

def get_ctx(refresh=False):
    """构建共享数据上下文。见 references/factor-builder-api.md 字段说明。
    数据起点按 BT_START 前推 LOOKBACK_DAYS 自然日作 warmup 缓冲,
    回测结果再裁剪回 [BT_START, BT_END]。
    二进制缓存: 同日同窗口读 data_cache/ctx_bin.pkl (秒级), 否则全量构建(分钟级)。"""
    global _ctx_cache
    if _ctx_cache is not None and not refresh:
        return _ctx_cache

    import datetime
    import pickle as _pkl
    from data_loader import (load_stock_market, load_indices, load_citic_l1,
                             load_stock_industry, INDEX_CODES, load_st_status)
    bin_cache = PROJECT_ROOT / 'data_cache' / 'ctx_bin.pkl'
    meta_want = {'bt_start': BT_START, 'bt_end': BT_END,
                 'date': datetime.date.today().isoformat(), 'ver': 3}
    if not refresh and bin_cache.exists():
        try:
            blob = _pkl.load(open(bin_cache, 'rb'))
            if blob['meta'] == meta_want:
                ctx = blob['ctx']
                ctx['sm'] = None          # 长表不入缓存 (当前无 builder 使用)
                _attach_ttm(ctx)
                _ctx_cache = ctx
                print(f'[ctx] 读二进制缓存 ({bin_cache.name}, '
                      f'{len(ctx["piv"])} pivots)', flush=True)
                return ctx
        except Exception as e:
            print(f'[ctx] 缓存读取失败, 全量重建: {e}', flush=True)

    load_start = (pd.Timestamp(BT_START) - pd.Timedelta(days=LOOKBACK_DAYS)).strftime('%Y-%m-%d')
    print(f'[ctx] loading stock_market (buffered from {load_start}) ...', flush=True)
    t0 = time.time()
    sm = load_stock_market(load_start, BT_END, verbose=False)
    sm = sm[sm['trade_status'] == 1].copy()
    sm = sm.sort_values(['stock_id', 'date']).reset_index(drop=True)

    # 涨跌停/连板标记 (主板近似口径, builder 可自行细化)
    sm['is_lu'] = ((sm['max_updown_close'] == 1) | (sm['pct_chg'] >= 9.5)).astype(np.int8)
    sm['is_yizi'] = ((sm['is_lu'] == 1) & ((sm['open'] - sm['close']).abs() < 0.01)).astype(np.int8)
    sm['is_ld'] = ((sm['max_updown_close'] == -1) | (sm['pct_chg'] <= -9.5)).astype(np.int8)
    # 连板数: 涨停 run 内从 1 起数 (首板=1)
    stock_change = sm['stock_id'] != sm['stock_id'].shift(1)
    prev_lu = sm['is_lu'].shift(1).fillna(0)
    new_run = (sm['is_lu'] == 1) & ((prev_lu == 0) | stock_change)
    run_id = new_run.cumsum()
    sm['consec'] = np.where(sm['is_lu'] == 1,
                            sm.groupby(run_id).cumcount() + 1, 0).astype(np.int16)

    print('[ctx] pivoting ...', flush=True)
    pivots = {}
    for col in ['open', 'high', 'low', 'close', 'volume', 'amt', 'pct_chg',
                'adj_close', 'is_lu', 'is_ld', 'is_yizi', 'consec']:
        pivots[col] = sm.pivot_table(index='date', columns='stock_id', values=col)

    # ST 特殊处理标记 (stock_status.is_ST 日频时点正确; 缺数据股票视为非 ST)
    try:
        st_df = load_st_status(load_start, BT_END)
        st_piv = st_df.pivot_table(index='date', columns='stock_id', values='is_ST')
        pivots['is_st'] = st_piv.reindex(index=pivots['close'].index,
                                         columns=pivots['close'].columns).fillna(0).astype(np.int8)
        print(f'[ctx] ST pivot: 日均 ST {pivots["is_st"].sum(axis=1).mean():.0f} 只', flush=True)
    except Exception as e:
        print(f'[ctx] ST 数据不可用(不排除): {e}', flush=True)
        pivots['is_st'] = pd.DataFrame(0, index=pivots['close'].index,
                                       columns=pivots['close'].columns, dtype=np.int8)

    # 市场级情绪序列 (timing builder 直接取用) — 全部向量化 groupby.sum
    print('[ctx] market emotion series ...', flush=True)
    sm['natural_lu'] = sm['is_lu'] - sm['is_yizi']
    sm['consec_ge2'] = (sm['consec'] >= 2).astype(np.int8)
    sm['is_up'] = (sm['pct_chg'] > 0).astype(np.int8)
    sm['is_dn'] = (sm['pct_chg'] < 0).astype(np.int8)
    sm['is_gt5'] = (sm['pct_chg'] >= 5).astype(np.int8)
    g = sm.groupby('date')
    mkt = pd.DataFrame({
        'n_lu': g['is_lu'].sum().astype(float),           # 涨停家数
        'n_ld': g['is_ld'].sum().astype(float),           # 跌停家数
        'n_natural_lu': g['natural_lu'].sum().astype(float),   # 自然涨停
        'max_consec': g['consec'].max().astype(float),    # 最高连板高度
        'n_consec_ge2': g['consec_ge2'].sum().astype(float),     # 连板家数
        'amt_sum': g['amt'].sum(),                        # 全市场成交额
        'n_up': g['is_up'].sum().astype(float),           # 上涨家数
        'n_dn': g['is_dn'].sum().astype(float),           # 下跌家数
        'n_gt5': g['is_gt5'].sum().astype(float),         # 涨幅>=5%家数
    })
    # 昨日涨停今日表现 (赚钱效应/晋级率/炸板率近似)
    lu = sm[sm['is_lu'] == 1][['date', 'stock_id']]
    date_arr = np.array(sorted(sm['date'].unique()))
    date_next = dict(zip(date_arr[:-1], date_arr[1:]))
    lu2 = lu.rename(columns={'date': 'prev_date'})
    lu2['date'] = lu2['prev_date'].map(date_next)
    lu_next = lu2.dropna().merge(sm[['date', 'stock_id', 'pct_chg', 'is_lu']],
                                 on=['date', 'stock_id'], how='inner')
    gg = lu_next.groupby('date')
    mkt['lu_next_ret'] = gg['pct_chg'].mean()              # 昨日涨停今日平均收益(溢价)
    mkt['lu_win_rate'] = gg['pct_chg'].apply(lambda x: (x > 0).mean())  # 赚钱效应
    mkt['jinji_rate'] = gg['is_lu'].mean()                 # 晋级率(昨涨停今再涨停)

    # 行业指数对齐个股交易日历 (Wind 偶有缺日, reindex+ffill; ret 用对齐后 close 重算)
    trade_dates = pd.DatetimeIndex(sorted(sm['date'].unique()))
    citic_raw = load_citic_l1(load_start, BT_END)
    citic_close = pd.DataFrame({n: d['close'] for n, d in citic_raw.items()})
    citic_close = citic_close.reindex(trade_dates).ffill()
    citic_ret = citic_close.pct_change()
    citic_high = pd.DataFrame({n: d['high'] for n, d in citic_raw.items()}).reindex(trade_dates).ffill()
    citic_low = pd.DataFrame({n: d['low'] for n, d in citic_raw.items()}).reindex(trade_dates).ffill()
    citic_vol = pd.DataFrame({n: d['volume'] for n, d in citic_raw.items()}).reindex(trade_dates).ffill()

    ctx = {
        'sm': sm,
        'piv': pivots,              # dict of pivots
        'ret': pivots['pct_chg'],   # 日收益(%)
        'mkt': mkt,
        'indices': load_indices(load_start, BT_END),
        'citic': load_citic_l1(load_start, BT_END),
        'INDEX_CODES': INDEX_CODES,
        'bench_ret': {k: v['ret'].reindex(trade_dates)
                      for k, v in load_indices(load_start, BT_END).items()},
        # 指数开/收盘价 (择时 T+1 开盘成交口径用; 收益拆为 隔夜×旧仓 + 日内×新仓)
        'bench_px': {k: v[['open', 'close']].reindex(trade_dates)
                     for k, v in load_indices(load_start, BT_END).items()},
        'citic_ret': citic_ret,
        'citic_close': citic_close,
        'citic_high': citic_high,
        'citic_low': citic_low,
        'citic_vol': citic_vol,
        'stock_ind': load_stock_industry(),   # Series(stock_id -> 中信一级行业名)
        # 个股×行业 one-hot 矩阵 (index=stock_id, columns=行业名), 与 piv 列对齐;
        # 行业聚合: factor_pivot.values @ onehot.values -> 行业层矩阵
        'ind_onehot': _build_onehot(pivots['close'].columns, load_stock_industry()),
        # 流通市值 pivot (亿元换算交由 builder 处理; 单位与 stock_equity 一致)
        'mktcap': _try_mktcap(load_start, BT_END),
    }

    _attach_ttm(ctx, load_start=load_start, trade_dates=trade_dates)
    print(f'[ctx] done in {time.time()-t0:.0f}s: sm {len(sm):,} rows, '
          f'{len(pivots)} pivots, mkt {len(mkt)} days', flush=True)
    _ctx_cache = ctx
    # 写二进制缓存 (sm 长表除外; float64→float32 压缩体积)
    try:
        ctx_to_save = {}
        for k, v in ctx.items():
            if k in ('sm', 'get_ttm') or k.startswith('_'):
                continue
            ctx_to_save[k] = v
        for pk, pv in ctx_to_save['piv'].items():
            if isinstance(pv, pd.DataFrame):
                ctx_to_save['piv'][pk] = pv.astype(np.float32, errors='ignore')
        if isinstance(ctx_to_save.get('mktcap'), pd.DataFrame):
            ctx_to_save['mktcap'] = ctx_to_save['mktcap'].astype(np.float32, errors='ignore')
        if isinstance(ctx_to_save.get('ret'), pd.DataFrame):
            ctx_to_save['ret'] = ctx_to_save['ret'].astype(np.float32, errors='ignore')
        _pkl.dump({'meta': meta_want, 'ctx': ctx_to_save}, open(bin_cache, 'wb'))
        print(f'[ctx] 二进制缓存已写 {bin_cache.name}', flush=True)
    except Exception as e:
        print(f'[ctx] 缓存写失败(不影响使用): {e}', flush=True)
    return ctx


def _attach_ttm(ctx, load_start=None, trade_dates=None):
    """把惰性 get_ttm 闭包挂到 ctx (二进制缓存加载后闭包需重建)"""
    if load_start is None:
        load_start = (pd.Timestamp(BT_START) - pd.Timedelta(days=LOOKBACK_DAYS)).strftime('%Y-%m-%d')
    if trade_dates is None:
        trade_dates = ctx['ret'].index

    def _get_ttm(field):
        """惰性获取 TTM 财务字段的时点正确 pivot (ann_date 对齐, 防前视)。
        字段见 data_loader.TTM_FIELDS (roe_ttm/roic_ttm/grossmargin_ttm/...)。"""
        if '_ttm_raw' not in ctx:
            from data_loader import load_stock_ttm
            print('[ctx] loading stock_ttm (first ttm access) ...', flush=True)
            ctx['_ttm_raw'] = load_stock_ttm(load_start, BT_END, verbose=False)
        piv_cache = ctx.setdefault('_ttm_piv', {})
        if field not in piv_cache:
            from data_loader import ttm_pivot_pit
            piv_cache[field] = ttm_pivot_pit(ctx['_ttm_raw'], field, trade_dates)
        return piv_cache[field]

    ctx['get_ttm'] = _get_ttm


# ============================================================
# 回测内核
# ============================================================
def bt_timing(signal, bench_ret, long_thresh, short_thresh=None, direction='high',
              position=None, bench_px=None, exec_mode='close', fee_one_side=0.0015):
    """信号滞后1天 → 仓位 → 策略收益序列。
    position: 可选, 直接给出目标仓位 Series(滞后1天执行), 支持多档仓位(如0.8/0.5/0)。
    exec_mode='open' (需传 bench_px=DataFrame[open,close]):
        T 日收盘出信号, T+1 开盘成交; 当日收益 = 旧仓×隔夜 + 新仓×日内 - fee×|Δ仓位|。
    exec_mode='close' (旧口径): 视为信号日收盘价成交, 仓位 shift(1) × 收盘收益, 不扣费。"""
    if position is not None:
        position = position.loc[position.index >= pd.Timestamp(BT_START)]
    elif signal is not None:
        signal = signal.loc[signal.index >= pd.Timestamp(BT_START)]

    if exec_mode == 'open':
        if bench_px is None:
            raise ValueError("exec_mode='open' 需要 bench_px (DataFrame[open, close])")
        # 目标仓位 pt: 以信号日收盘为决策时点
        if position is not None:
            pt = position.rename('pt')
        else:
            pt = pd.Series(0.0, index=signal.index, name='pt')
            if direction == 'high':
                pt[signal > long_thresh] = 1.0
                if short_thresh is not None:
                    pt[signal < short_thresh] = -1.0
            else:
                pt[signal < long_thresh] = 1.0
                if short_thresh is not None:
                    pt[signal > short_thresh] = -1.0
        a = pd.DataFrame({'r': bench_ret, 'pt': pt}).join(bench_px, how='inner').dropna()
        a['pos'] = a['pt'].shift(1)                      # 今日开盘成交到的仓位 (昨收盘信号)
        a['prev'] = a['pos'].shift(1)                    # 昨日开盘成交的仓位 (吃今日隔夜)
        a['on'] = a['open'] / a['close'].shift(1) - 1    # 隔夜: 昨收盘→今开盘
        a['oc'] = a['close'] / a['open'] - 1             # 日内: 今开盘→今收盘
        a = a.dropna(subset=['pos', 'on', 'oc'])
        a['prev'] = a['prev'].fillna(0.0)
        a['dpos'] = (a['pos'] - a['prev']).abs()
        a['sr'] = a['prev'] * a['on'] + a['pos'] * a['oc'] - fee_one_side * a['dpos']
        a['sl'] = a['pos']  # 仅用于信号计数
    elif position is not None:
        a = pd.DataFrame({'p': position, 'r': bench_ret}).dropna()
        a['pos'] = a['p'].shift(1)
        a = a.dropna()
        a['sl'] = a['pos']  # 仅用于信号计数
        a['dpos'] = a['pos'].diff().abs()
        a['sr'] = a['pos'] * a['r']
    else:
        a = pd.DataFrame({'s': signal, 'r': bench_ret}).dropna()
        a['sl'] = a['s'].shift(1)
        a = a.dropna()
        a['pos'] = 0
        if direction == 'high':
            a.loc[a['sl'] > long_thresh, 'pos'] = 1
            if short_thresh is not None:
                a.loc[a['sl'] < short_thresh, 'pos'] = -1
        else:
            a.loc[a['sl'] < long_thresh, 'pos'] = 1
            if short_thresh is not None:
                a.loc[a['sl'] > short_thresh, 'pos'] = -1
        a['dpos'] = a['pos'].diff().abs()
        a['sr'] = a['pos'] * a['r']
    if len(a) < 10:
        raise NotImplementedError('信号与基准重叠区间为空或过短')
    cum = (1 + a['sr']).cumprod()
    bcum = (1 + a['r']).cumprod()
    yrs = len(a) / 252
    perf = {
        'annual_ret': annual_ret(cum), 'max_dd': max_dd(cum), 'sharpe': sharpe(a['sr']),
        'benchmark_annual_ret': annual_ret(bcum),
        'long_days_pct': (a['pos'] == 1).mean(),
        'signal_count': int((a['dpos'].fillna(0) > 0).sum()),
        'turnover': a['dpos'].sum() / yrs,                    # 年均单边换手 (仓位口径)
        'fee_drag': fee_one_side * a['dpos'].sum() / yrs,     # 年均费用拖累
        'exec_mode': exec_mode,
    }
    perf['excess'] = perf['annual_ret'] - perf['benchmark_annual_ret']
    return perf, a[['sr', 'pos']].assign(cum=cum, bcum=bcum)


def _period_ends(dates, freq):
    s = pd.Series(dates, index=dates)
    if freq == 'daily':
        return list(dates)
    rule = 'M' if freq == 'monthly' else 'W'
    return list(s.groupby(s.dt.to_period(rule)).last().values)


def bt_cross_section(factor, ret_pct, open_piv, yizi_piv,
                     freq='monthly', n_groups=10, min_stocks=30,
                     st_piv=None, fee_one_side=0.0015):
    """截面 v2 (可成交口径):
    - 进场价 = T+1 开盘 (T 收盘成交的涨停价买不进, 隔夜缺口收益不可实现)
    - 剔除 T+1 一字涨停股 (全天封死, 开盘同样买不进)
    - st_piv: ST 特殊处理股剔除 (信号日 is_ST=1 不进池)
    - fee_one_side: 单边费率 (双边千3=0.0015), 分组换手成本计入组净值; IC 本身与费用无关
    收益口径: daily = open_{T+1}→open_{T+2}; 周/月 = open_{T+1}→open_{T2+1}
    Rank IC 向量化 (rank+corrwith=Spearman)。"""
    entry_px = open_piv.shift(-1)                              # T+1 开盘价
    untradable = yizi_piv.shift(-1).fillna(0).astype(bool)     # T+1 一字板买不进
    dates = ret_pct.index
    # warmup 数据仅供 builder 计算因子, 回测期从 BT_START 起
    factor = factor.loc[factor.index >= pd.Timestamp(BT_START)]
    if st_piv is not None:                                     # ST 剔除 (信号日)
        factor = factor.mask(st_piv.reindex(factor.index).fillna(0).astype(bool))
    ends = [d for d in _period_ends(dates, freq) if d >= pd.Timestamp(BT_START)]

    # 各期的下期收益 (T+1 开盘进场)
    if freq == 'daily':
        fwd_df = entry_px.shift(-1) / entry_px - 1             # open_{T+1}→open_{T+2}
    else:
        fwd_rows = {}
        for i in range(len(ends) - 1):
            t0, t1 = ends[i], ends[i + 1]
            if t0 in entry_px.index and t1 in entry_px.index:
                fwd_rows[t0] = entry_px.loc[t1] / entry_px.loc[t0] - 1
        fwd_df = pd.DataFrame(fwd_rows).T

    # 剔除无法成交样本 (因子值与收益同 mask: 买不进的股票不进池)
    factor = factor.mask(untradable.reindex(factor.index).fillna(True))
    fwd_df = fwd_df.mask(untradable.reindex(fwd_df.index).fillna(True))

    # 期末日采样 + 覆盖过滤
    common_idx = factor.index.intersection(fwd_df.index)
    F, R = factor.loc[common_idx], fwd_df.loc[common_idx]
    both = F.notna() & R.notna()
    cnt = both.sum(axis=1)
    ic_valid = cnt >= max(min_stocks, n_groups)
    F_ic, R_ic = F[ic_valid], R[ic_valid]
    if len(F_ic) == 0:
        raise NotImplementedError('无有效IC期(因子过稀疏或全覆盖不足)')

    # 向量化 Rank IC (逐行 pearson of ranks = spearman)
    ics = F_ic.rank(axis=1).corrwith(R_ic.rank(axis=1), axis=1).dropna()
    if len(ics) == 0:
        raise NotImplementedError('无有效IC期(因子过稀疏或全覆盖不足)')

    # 向量化分组净值 (覆盖不足 n_groups*3 的期跳过, 防单股组统计伪影)
    grp_valid = cnt >= n_groups * 3
    F_g, R_g = F[grp_valid], R[grp_valid]
    g_nav = {}
    if len(F_g) > 0:
        pct = F_g.rank(axis=1, method='first', pct=True)  # method='first' 打破同分(二值因子)
        grp = np.ceil(pct * n_groups).clip(1, n_groups)
        for k in range(1, n_groups + 1):
            mem = grp == k
            s = R_g.where(mem).mean(axis=1).dropna()
            if len(s) > 0:
                # 换仓费: 组内成员变更比例 × 双边 (建仓期视为全换)
                inter = (mem & mem.shift(1).fillna(False)).sum(axis=1)
                churn = (1 - inter / mem.sum(axis=1).clip(lower=1)).reindex(s.index).fillna(1.0)
                s = s - churn * 2 * fee_one_side
                g_nav[k] = (1 + s).cumprod()
            else:
                g_nav[k] = pd.Series(dtype=float)
    else:
        for k in range(1, n_groups + 1):
            g_nav[k] = pd.Series(dtype=float)

    # 分组年化必须按调仓频率取期数 (daily=252/weekly=52/monthly=12)
    ppy = {'daily': 252, 'weekly': 52, 'monthly': 12}[freq]

    def period_annual(nav):
        n = len(nav) / ppy
        return nav.iloc[-1] ** (1 / n) - 1 if n > 0 and nav.iloc[-1] > 0 else np.nan

    ic_mean, ic_std = ics.mean(), ics.std()
    ls = g_nav[n_groups].iloc[-1] / g_nav[1].iloc[-1] - 1 if len(g_nav[n_groups]) and len(g_nav[1]) else np.nan
    perf = {
        'ic_mean': ic_mean, 'ic_std': ic_std,
        'icir': ic_mean / ic_std if ic_std > 0 else 0,
        'ic_pos_pct': (ics > 0).mean() if len(ics) else np.nan,
        'g1_ann': period_annual(g_nav[1]) if len(g_nav[1]) else np.nan,
        'g10_ann': period_annual(g_nav[n_groups]) if len(g_nav[n_groups]) else np.nan,
        'ls_total': ls,
        'n_periods': len(ics),
    }
    return perf, ics, g_nav


def bt_rotation(factor_ind, ind_ret, top_n=5, bottom_n=5, min_common=None,
                fee_one_side=0.0015):
    """轮动: 日频, 多 top_n / 空 bottom_n / 等权基准。
    min_common: 每日最少非NaN行业数(默认 top_n+bottom_n); 事件型因子可传小值(如3)。
    fee_one_side: 单边费率 (双边千3=0.0015), 多头组行业变更按替换比例×双边计入。"""
    from scipy import stats
    if min_common is None:
        min_common = top_n + bottom_n
    factor_ind = factor_ind.loc[factor_ind.index >= pd.Timestamp(BT_START)]
    common = factor_ind.index.intersection(ind_ret.index)
    long_r, bench_r, short_r, ics = [], [], [], []
    dates = []
    prev_long = None
    for i in range(len(common) - 1):
        dt, nxt = common[i], common[i + 1]
        fv = factor_ind.loc[dt].dropna()
        nr = ind_ret.loc[nxt].dropna()
        cmn = fv.index.intersection(nr.index)
        if len(cmn) < min_common:
            continue
        fv_s = fv[cmn].sort_values(ascending=False)
        long_set = set(fv_s.index[:top_n])
        # 换仓费: 替换比例 × 双边 (首期全换)
        replaced = top_n if prev_long is None else len(long_set - prev_long)
        cost = (replaced / top_n) * 2 * fee_one_side
        prev_long = long_set
        long_r.append(nr[fv_s.index[:top_n]].mean() - cost)
        short_r.append(nr[fv_s.index[-bottom_n:]].mean())
        bench_r.append(nr.mean())
        dates.append(nxt)
        ic, _ = stats.spearmanr(fv[cmn], nr[cmn])
        ics.append(ic)
    lr, br, sr = pd.Series(long_r, index=dates), pd.Series(bench_r, index=dates), pd.Series(short_r, index=dates)
    if len(lr) < 20:
        raise NotImplementedError('轮动有效交易日过少')
    lcum, bcum = (1 + lr).cumprod(), (1 + br).cumprod()
    excess_nav = (1 + (lr - br)).cumprod()
    perf = {
        'long_annual_ret': annual_ret(lcum), 'benchmark_ret': annual_ret(bcum),
        'excess_ret': annual_ret(lcum) - annual_ret(bcum),
        'max_dd': max_dd(lcum), 'win_rate': (lr > br).mean(),
        'ic_mean': np.nanmean(ics) if ics else np.nan,
        'long_short_ret': annual_ret((1 + (lr - sr)).cumprod()),
    }
    series = pd.DataFrame({'long': lcum, 'bench': bcum, 'excess': excess_nav})
    return perf, series


def bt_screening(signals, ret_pct, bench_ret, yizi_piv, open_piv, close_piv,
                 hold_days=5, max_stocks=50, exit_mode='close', exit_defer=False,
                 ydn_piv=None, adj_close_piv=None, st_piv=None, fee_one_side=0.0015):
    """筛选 v2.1 (可成交口径): T+1 **开盘**进场持有, 等权;
    剔除 T+1 一字涨停 **及 T+1 停牌(open 为 NaN)** 无法买入的信号。
    进场日收益 = close/open-1 (日内), 其后为 close-to-close (pct_chg 复权)。

    出场模式选项 (默认 exit_mode='close'/exit_defer=False):
    - exit_mode='open': 持有 hold_days 个夜晚后于**次日开盘**卖出
      (hold_days=1 + open 卖出 = T+1 开盘买/T+2 开盘卖, T+1 合规的"次日了结");
      卖出段收益 = 复权open/前日复权收-1 (用 adj_close_piv 推算复权因子)
    - exit_defer=True: 预定卖出日遇一字跌停(需 ydn_piv)或停牌 → 递延卖出逐日重试,
      锁仓期间收益如实计入 (与 portfolio_cs.py 同口径)
    - T+0 守卫: hold_days=1 且 exit_mode='close' 为当日买当日卖, A股不可执行,
      perf['t1_tradable']=False 并打印警告 (保留仅供与旧结果对照)
    - st_piv 剔除信号日 ST 股; fee_one_side=0.0015 (双边千3),
      按当日进出场笔数/max_stocks 计组合日费用
    """
    signals = signals[signals['date'] >= pd.Timestamp(BT_START)]
    ret = (ret_pct / 100.0).astype(np.float32)

    # v2 剔除 T+1 一字板信号 (全天封死, 无法买入); v2.1 同时剔除 T+1 停牌信号
    yz_next = yizi_piv.shift(-1).fillna(0).astype(bool)
    ub_next = yz_next | open_piv.shift(-1).isna()
    ub_long = ub_next.stack()
    ub_long = ub_long[ub_long].reset_index()[['date', 'stock_id']]
    if len(ub_long) > 0 and len(signals) > 0:
        merged = signals.merge(ub_long, on=['date', 'stock_id'], how='left', indicator=True)
        signals = merged[merged['_merge'] == 'left_only'][['date', 'stock_id']]
    # ST 剔除: 信号日 is_ST=1 的股票不入选
    if st_piv is not None and len(signals) > 0:
        st_long = st_piv.stack()
        st_long = st_long[st_long.astype(bool)].reset_index()[['date', 'stock_id']]
        if len(st_long) > 0:
            merged = signals.merge(st_long, on=['date', 'stock_id'], how='left', indicator=True)
            signals = merged[merged['_merge'] == 'left_only'][['date', 'stock_id']]
    dates = list(ret.index)
    didx = {d: i for i, d in enumerate(dates)}
    nd = len(dates)

    col_of = {s: j for j, s in enumerate(ret.columns)}
    open_v = open_piv.reindex(index=ret.index, columns=ret.columns).values
    close_v = close_piv.reindex(index=ret.index, columns=ret.columns).values
    ret_v = ret.values
    if exit_mode == 'open':
        ac = adj_close_piv if adj_close_piv is not None else close_piv
        ac = ac.reindex(index=ret.index, columns=ret.columns)
        adj_open_v = (open_piv.reindex(index=ret.index, columns=ret.columns)
                      * (ac / close_piv.reindex(index=ret.index, columns=ret.columns))).values
        adj_close_v = ac.values
    else:
        adj_open_v = adj_close_v = None
    ydn_v = None
    if exit_defer:
        if ydn_piv is None:
            raise ValueError('exit_defer=True 需要 ydn_piv (一字跌停标记透视表)')
        ydn_v = ydn_piv.reindex(index=ret.index, columns=ret.columns).fillna(0).values

    t1_tradable = not (hold_days == 1 and exit_mode == 'close')
    if not t1_tradable:
        print("  [bt_screening] 警告: hold_days=1 + 收盘卖出 = T+0 口径, 收益不可执行, "
              "仅供对照 (exit_mode='open' 为合规口径)", flush=True)

    # 每个信号 -> (进场日索引 i0, 出场日索引 x, 列索引 j)
    enter_by_day = {}
    n_defer_trades, defer_days_total = 0, 0
    for _, row in signals.iterrows():
        d, stk = row['date'], row['stock_id']
        if d not in didx or stk not in col_of:
            continue
        i0 = didx[d] + 1
        if i0 >= nd:
            continue
        j = col_of[stk]
        x = i0 + hold_days if exit_mode == 'open' else i0 + hold_days - 1
        if x >= nd:
            x = nd - 1
        if exit_defer:
            x0 = x
            while x < nd - 1:
                if exit_mode == 'open':
                    sellable = (np.isfinite(open_v[x, j]) and open_v[x, j] > 0
                                and ydn_v[x, j] == 0)
                else:
                    sellable = np.isfinite(ret_v[x, j]) and ydn_v[x, j] == 0
                if sellable:
                    break
                x += 1
            if x > x0:
                n_defer_trades += 1
                defer_days_total += x - x0
        enter_by_day.setdefault(i0, []).append((i0, x, j))

    port, dts = [], []
    active = []
    n_hold_sum, n_hold_days = 0, 0
    for t in range(nd):
        new = enter_by_day.get(t)
        if new:
            # close 模式: 并集截断 (排队中的信号等空位);
            # open 模式: 进场处截断 (每日全回收, 只买当日新信号, 递延锁仓另计)
            active.extend(new if exit_mode == 'close' else new[:max_stocks])
        if not active:
            continue
        if exit_mode == 'open':
            # 两段式记账 (与资金全回收的真实账目一致):
            #   隔夜段: 昨收持仓 H 的 复权open(t)/复权close(t-1)-1 均值
            #   日内段: 竞价后持仓 B (新进 ∪ 继续持有) 的 close/open-1 均值
            #   日收益 = 隔夜段 + 日内段 (新进批次与前批各自等权, 不做并集稀释)
            H = [rec for rec in active if rec[0] < t]            # 昨收持有
            B = [rec for rec in active if rec[0] == t or rec[1] > t]  # 竞价后持有
            ov = []
            for _, _, j in H:
                ao, ac_ = adj_open_v[t, j], adj_close_v[t - 1, j]
                if np.isfinite(ao) and np.isfinite(ac_) and ac_ != 0:
                    ov.append(ao / ac_ - 1)
            intra = []
            for _, _, j in B:
                o, c = open_v[t, j], close_v[t, j]
                if np.isfinite(o) and np.isfinite(c) and o != 0:
                    intra.append(c / o - 1)
            n_hold_sum += len(B)
            n_hold_days += 1
            if not ov and not intra:
                active = [rec for rec in active if rec[1] > t]
                continue
            day_ret = (np.mean(ov) if ov else 0.0) + (np.mean(intra) if intra else 0.0)
        else:
            n_hold_sum += min(len(active), max_stocks)
            n_hold_days += 1
            day_rets = []
            n_ent = 0
            for i0, x, j in active[:max_stocks]:
                if t == i0:
                    n_ent += 1
                if t == i0:
                    # 进场日: 开盘价进场, 收益 = close/open - 1
                    o, c = open_v[t, j], close_v[t, j]
                    r = c / o - 1 if (np.isfinite(o) and np.isfinite(c) and o != 0) else None
                else:
                    r = ret_v[t, j]
                    if not np.isfinite(r):
                        r = None
                if r is not None:
                    day_rets.append(r)
            if not day_rets:
                active = [rec for rec in active if rec[1] > t]
                continue
            day_ret = np.mean(day_rets)
        # 费用: 当日进场 + 当日出场笔数, 每笔按 1/max_stocks 权重 × 单边费率
        n_before = len(active)
        active = [rec for rec in active if rec[1] > t]
        n_ext = n_before - len(active)
        if exit_mode == 'close':
            day_ret -= (n_ent + n_ext) * fee_one_side / max_stocks
        else:
            n_ent_o = len([rec for rec in (new[:max_stocks] if new else []) ])
            day_ret -= (n_ent_o + n_ext) * fee_one_side / max_stocks
        port.append(day_ret)
        dts.append(dates[t])
    if len(dts) < 20:
        raise NotImplementedError('筛选信号过少或无有效持仓日')
    # 全区间口径: 非持仓日组合收益=0(空仓), 基准全期复利 — 稀疏因子基准不再失真
    hold_dates = pd.DatetimeIndex(dts)
    pr_hold = pd.Series(port, index=hold_dates)
    all_days = ret.index[ret.index >= pd.Timestamp(BT_START)]
    pr = pd.Series(0.0, index=all_days)
    pr.loc[hold_dates] = pr_hold
    br = bench_ret.reindex(all_days).fillna(0)
    cum, bcum = (1 + pr).cumprod(), (1 + br).cumprod()
    excess_nav = (1 + (pr - br)).cumprod()
    perf = {
        'annual_ret': annual_ret(cum), 'sharpe': sharpe(pr), 'max_dd': max_dd(cum),
        'benchmark_annual_ret': annual_ret(bcum),
        'excess_annual_ret': annual_ret(cum) - annual_ret(bcum),
        # 胜率按持仓日口径; 另报持仓覆盖率便于横向比较稀疏因子
        'win_rate': (pr_hold > 0).mean(),
        'win_rate_vs_bench': (pr_hold > br.loc[hold_dates]).mean(),
        'hold_coverage': len(hold_dates) / max(len(all_days), 1),
        'total_trades': len(signals),
        # avg_holding 按截断后实际持仓口径
        'avg_holding': int(n_hold_sum / n_hold_days) if n_hold_days else 0,
        'excess_max_dd': max_dd(excess_nav),
        # v2.1: T+1 可执行性标记 与 递延卖出统计
        't1_tradable': t1_tradable,
        'defer_pct': (n_defer_trades / max(len(signals), 1)) if exit_defer else None,
        'defer_days_avg': (defer_days_total / n_defer_trades) if n_defer_trades else None,
    }
    series = pd.DataFrame({'port': cum, 'bench': bcum, 'excess': excess_nav})
    series['held'] = series.index.isin(hold_dates)
    return perf, series


# ============================================================
# 绘图 (每因子一张 PNG, 多子图)
# ============================================================
def _stats_text(d, keys):
    return '\n'.join(f'{k}={d[k]:.3f}' for k in keys if k in d and d[k] is not None and not pd.isna(d[k]))


def chart_timing(fid, name, book, res, perf, out):
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                             gridspec_kw={'height_ratios': [3, 1]})
    axes[0].plot(res.index, res['cum'], label='策略净值', lw=1.5, color='#c0392b')
    axes[0].plot(res.index, res['bcum'], label='基准净值', lw=1.2, color='#7f8c8d', alpha=0.85)
    exec_tag = '（T+1开盘成交·双边千三）' if perf.get('exec_mode') == 'open' else ''
    axes[0].set_title(f'{fid} {name}（{book}）— 择时净值 vs 基准{exec_tag}')
    axes[0].legend(loc='upper left')
    axes[0].grid(alpha=0.3)
    axes[0].text(0.99, 0.02, _stats_text(perf, ['annual_ret', 'benchmark_annual_ret', 'excess',
                                                'sharpe', 'max_dd', 'turnover', 'fee_drag']),
                 transform=axes[0].transAxes, ha='right', va='bottom', fontsize=9,
                 bbox=dict(boxstyle='round', fc='#f8f9fa', ec='#dee2e6'))
    dd = (res['cum'] - res['cum'].cummax()) / res['cum'].cummax()
    axes[1].fill_between(dd.index, dd, 0, color='#2980b9', alpha=0.5)
    axes[1].set_title('策略回撤', fontsize=10)
    axes[1].grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    plt.close(fig)


def chart_cs(fid, name, book, ics, g_nav, perf, out, n_groups=10):
    fig, axes = plt.subplots(3, 1, figsize=(11, 10),
                             gridspec_kw={'height_ratios': [2, 2, 3]})
    width = max(0.5, (ics.index[-1] - ics.index[0]).days / max(len(ics), 1) * 0.7) if len(ics) > 1 else 5
    axes[0].bar(ics.index, ics.values, width=width, color=np.where(ics.values > 0, '#c0392b', '#27ae60'))
    axes[0].axhline(ics.mean(), color='#2c3e50', ls='--', lw=1, label=f'IC均值={ics.mean():.3f}')
    axes[0].set_title(f'{fid} {name}（{book}）— IC 序列')
    axes[0].legend(loc='upper right')
    axes[0].grid(alpha=0.3)

    cumic = ics.cumsum()
    axes[1].plot(cumic.index, cumic.values, color='#c0392b', lw=1.5)
    axes[1].set_title(f'累计 IC（ICIR={perf["icir"]:.2f}, IC>0占比={perf["ic_pos_pct"]:.0%}）')
    axes[1].grid(alpha=0.3)

    cmap = plt.get_cmap('RdYlGn')
    for k in range(1, n_groups + 1):
        nav = g_nav[k]
        if len(nav) == 0:
            continue
        lw, alpha = (2.2, 1.0) if k in (1, n_groups) else (1.0, 0.65)
        axes[2].plot(nav.index, nav.values, lw=lw, alpha=alpha,
                     color=cmap((k - 1) / (n_groups - 1)), label=f'G{k}')
    axes[2].set_title(f'分 {n_groups} 组净值（G1={perf["g1_ann"]:.1%}, G{n_groups}={perf["g10_ann"]:.1%} 年化）')
    axes[2].legend(ncol=5, fontsize=8, loc='upper left')
    axes[2].grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    plt.close(fig)


def chart_rotation(fid, name, book, series, perf, out):
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    axes[0].plot(series.index, series['long'], label='多头组合净值', lw=1.5, color='#c0392b')
    axes[0].plot(series.index, series['bench'], label='行业等权基准', lw=1.2, color='#7f8c8d', alpha=0.85)
    axes[0].set_title(f'{fid} {name}（{book}）— 轮动净值 vs 基准')
    axes[0].legend(loc='upper left')
    axes[0].grid(alpha=0.3)
    axes[0].text(0.99, 0.02, _stats_text(perf, ['long_annual_ret', 'benchmark_ret', 'excess_ret', 'max_dd', 'win_rate', 'ic_mean']),
                 transform=axes[0].transAxes, ha='right', va='bottom', fontsize=9,
                 bbox=dict(boxstyle='round', fc='#f8f9fa', ec='#dee2e6'))
    axes[1].plot(series.index, series['excess'], color='#2980b9', lw=1.3)
    axes[1].axhline(1, color='#7f8c8d', ls='--', lw=0.8)
    axes[1].set_title('相对基准超额净值', fontsize=10)
    axes[1].grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    plt.close(fig)


def chart_screening(fid, name, book, series, perf, out):
    fig, axes = plt.subplots(3, 1, figsize=(11, 9), sharex=True,
                             gridspec_kw={'height_ratios': [3, 2, 2]})
    axes[0].plot(series.index, series['port'], label='组合净值', lw=1.5, color='#c0392b')
    axes[0].plot(series.index, series['bench'], label='基准净值', lw=1.2, color='#7f8c8d', alpha=0.85)
    axes[0].set_title(f'{fid} {name}（{book}）— 筛选组合 vs 基准')
    axes[0].legend(loc='upper left')
    axes[0].grid(alpha=0.3)
    axes[0].text(0.99, 0.02, _stats_text(perf, ['annual_ret', 'benchmark_annual_ret', 'excess_annual_ret', 'sharpe', 'max_dd', 'win_rate_vs_bench', 'total_trades']),
                 transform=axes[0].transAxes, ha='right', va='bottom', fontsize=9,
                 bbox=dict(boxstyle='round', fc='#f8f9fa', ec='#dee2e6'))
    axes[1].plot(series.index, series['excess'], color='#2980b9', lw=1.3)
    axes[1].axhline(1, color='#7f8c8d', ls='--', lw=0.8)
    axes[1].set_title('超额净值', fontsize=10)
    axes[1].grid(alpha=0.3)
    dd = (series['port'] - series['port'].cummax()) / series['port'].cummax()
    axes[2].fill_between(dd.index, dd, 0, color='#8e44ad', alpha=0.4)
    axes[2].set_title('组合回撤', fontsize=10)
    axes[2].grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    plt.close(fig)


# ============================================================
# builder 注册与运行
# ============================================================
def load_builders():
    """加载 scripts/builders/*.py 中所有 BUILDERS 注册项"""
    registry = {}
    bdir = PROJECT_ROOT / 'scripts' / 'builders'
    if not bdir.exists():
        return registry
    for f in sorted(bdir.glob('*.py')):
        if f.stem.startswith('_'):
            continue
        mod = {}
        try:
            code = f.read_text(encoding='utf-8')
            exec(compile(code, str(f), 'exec'), mod)
            for fid, fn in mod.get('BUILDERS', {}).items():
                if fid in registry:
                    print(f'[builders] 警告: {fid} 重复注册 '
                          f'({registry[fid][1]}.py 被 {f.stem}.py 覆盖)', flush=True)
                registry[fid] = (fn, f.stem)
        except Exception as e:
            print(f'[builders] 加载 {f.name} 失败: {e}', flush=True)
    return registry


def get_factor_names():
    """从 SQLite 取 factor_id → (名称, 书) 映射 (取不到则用 id)"""
    try:
        import sqlite3
        conn = sqlite3.connect(PROJECT_ROOT / 'db' / 'factor.db')
        rows = conn.execute('SELECT factor_id, factor_name, source_book FROM factor_meta').fetchall()
        conn.close()
        return {r[0]: (r[1], r[2]) for r in rows}
    except Exception:
        return {}


def run_one(fid, builder, ctx, names, save_db=False):
    name, book = names.get(fid, (fid, ''))
    spec = builder(ctx)
    ftype = spec['type']

    if ftype == 'timing':
        bench = BENCH_REMAP.get(spec.get('bench', 'ZZ1000'), spec.get('bench', 'ZZ1000'))
        perf, res = bt_timing(spec.get('signal'), ctx['bench_ret'][bench],
                              spec.get('long_thresh'), spec.get('short_thresh'),
                              spec.get('direction', 'high'),
                              position=spec.get('position'),
                              bench_px=ctx['bench_px'].get(bench),
                              exec_mode=TIMING_EXEC, fee_one_side=TIMING_FEE_ONE_SIDE)
        out = CHART_DIR / f'{fid}.png'
        chart_timing(fid, name, book, res, perf, out)
        db_row = dict(perf, factor_id=fid, index_code=bench,
                      index_name=ctx['INDEX_CODES'][bench][1],
                      backtest_start=str(res.index[0].date()), backtest_end=str(res.index[-1].date()),
                      long_annual_ret=perf['annual_ret'], long_max_dd=perf['max_dd'],
                      long_sharpe=perf['sharpe'], long_win_rate=None,
                      short_annual_ret=None, long_short_ret=perf['excess'],
                      long_short_sharpe=None, avg_holding_days=None)
        kind = 'timing'
    elif ftype == 'cross_section':
        freq = spec.get('freq', 'monthly')
        # 底稿留痕: 因子值 pivot 落盘供组合/变换/消融复用 (见 AGENTS.md 留痕原则)
        try:
            fv_dir = PROJECT_ROOT / 'results' / 'factor_values'
            fv_dir.mkdir(parents=True, exist_ok=True)
            spec['factor'].to_pickle(fv_dir / f'{fid}.pkl')
        except Exception as e:
            print(f'  [{fid}] 因子值缓存失败(不影响回测): {e}', flush=True)
        perf, ics, g_nav = bt_cross_section(spec['factor'], ctx['ret'],
                                            ctx['piv']['open'], ctx['piv']['is_yizi'],
                                            freq=freq,
                                            min_stocks=spec.get('min_stocks', 30),
                                            st_piv=ctx['piv'].get('is_st'))
        out = CHART_DIR / f'{fid}.png'
        chart_cs(fid, name, book, ics, g_nav, perf, out)
        db_row = dict(factor_id=fid, universe=spec.get('universe', '全A'), frequency=freq,
                      backtest_start=str(ics.index[0].date()) if len(ics) else None,
                      backtest_end=str(ics.index[-1].date()) if len(ics) else None,
                      ic_mean=perf['ic_mean'], ic_std=perf['ic_std'], icir=perf['icir'],
                      ic_positive_pct=perf['ic_pos_pct'], coverage_mean=None, coverage_min=None,
                      g1_annual_ret=perf['g1_ann'], g2_annual_ret=None, g3_annual_ret=None,
                      g4_annual_ret=None, g5_annual_ret=perf['g10_ann'],
                      long_short_ret=perf['ls_total'], long_short_sharpe=None,
                      long_excess_ret=None, long_max_dd=None, monotonicity=None)
        kind = 'cs'
    elif ftype == 'rotation':
        perf, series = bt_rotation(spec['factor'], ctx['citic_ret'],
                                   spec.get('top_n', 5), spec.get('bottom_n', 5),
                                   min_common=spec.get('min_common'))
        out = CHART_DIR / f'{fid}.png'
        chart_rotation(fid, name, book, series, perf, out)
        db_row = dict(factor_id=fid, industry_cls='CITIC_L1', frequency=spec.get('freq', 'daily'),
                      backtest_start=str(series.index[0].date()), backtest_end=str(series.index[-1].date()),
                      ic_mean=perf['ic_mean'], icir=None, top_n=spec.get('top_n', 5),
                      bottom_n=spec.get('bottom_n', 5), long_annual_ret=perf['long_annual_ret'],
                      short_annual_ret=None, long_short_ret=perf['long_short_ret'],
                      benchmark_ret=perf['benchmark_ret'], excess_ret=perf['excess_ret'],
                      max_dd=perf['max_dd'], win_rate=perf['win_rate'])
        kind = 'rotation'
    elif ftype == 'screening':
        bench = BENCH_REMAP.get(spec.get('bench', 'ZZ1000'), spec.get('bench', 'ZZ1000'))
        # v2.1: builder 可选 spec['exit_mode']='open' (次日开盘卖) / spec['exit_defer']=True
        exit_defer = spec.get('exit_defer', False)
        ydn_piv = None
        if exit_defer:
            piv = ctx['piv']
            ydn_piv = ((piv['is_ld'].fillna(0).astype(bool))
                       & ((piv['open'] - piv['close']).abs() < 0.01)).astype(np.int8)
        perf, series = bt_screening(spec['signals'], ctx['ret'], ctx['bench_ret'][bench],
                                    ctx['piv']['is_yizi'], ctx['piv']['open'], ctx['piv']['close'],
                                    spec.get('hold_days', 5), spec.get('max_stocks', 50),
                                    st_piv=ctx['piv'].get('is_st'),
                                    exit_mode=spec.get('exit_mode', 'close'),
                                    exit_defer=exit_defer, ydn_piv=ydn_piv,
                                    adj_close_piv=ctx['piv']['adj_close'])
        out = CHART_DIR / f'{fid}.png'
        chart_screening(fid, name, book, series, perf, out)
        db_row = dict(factor_id=fid, benchmark=ctx['INDEX_CODES'][bench][1],
                      frequency=spec.get('freq', 'daily'),
                      backtest_start=str(series.index[0].date()) if len(series) else None,
                      backtest_end=str(series.index[-1].date()) if len(series) else None,
                      annual_ret=perf['annual_ret'], annual_vol=None, sharpe=perf['sharpe'],
                      max_dd=perf['max_dd'], calmar=None,
                      excess_annual_ret=perf['excess_annual_ret'],
                      excess_max_dd=perf['excess_max_dd'], excess_sharpe=None,
                      win_rate=perf['win_rate'], win_rate_vs_bench=perf['win_rate_vs_bench'],
                      avg_holding=perf['avg_holding'], avg_turnover=None,
                      total_trades=perf['total_trades'])
        kind = 'screening'
    else:
        raise ValueError(f'未知因子类型: {ftype}')

    if save_db:
        from save_to_db import save_timing_perf, save_cs_perf, save_rotation_perf, save_screening_perf
        {'timing': save_timing_perf, 'cs': save_cs_perf,
         'rotation': save_rotation_perf, 'screening': save_screening_perf}[kind](db_row)
    return {'factor_id': fid, 'type': ftype, 'chart': str(out), **{k: v for k, v in perf.items()}}


def merge_perf(results, failed):
    """把本次结果合并进 results/chart_perf.json (按 factor_id 去重)。
    逐因子即时调用, 崩溃只丢在跑的那一个。"""
    perf_path = PROJECT_ROOT / 'results' / 'chart_perf.json'
    perf_path.parent.mkdir(exist_ok=True)
    old_results, old_failed = [], []
    if perf_path.exists():
        try:
            old = json.load(open(perf_path))
            old_results, old_failed = old.get('results', []), old.get('failed', [])
        except Exception:
            pass
    new_ok = {r['factor_id'] for r in results}
    new_fail = {f['factor_id'] for f in failed}
    merged_results = [r for r in old_results if r['factor_id'] not in new_ok and r['factor_id'] not in new_fail] + results
    merged_failed = [f for f in old_failed if f['factor_id'] not in new_ok and f['factor_id'] not in new_fail] + failed
    with open(perf_path, 'w') as f:
        json.dump({'results': merged_results, 'failed': merged_failed}, f, indent=1, ensure_ascii=False, default=str)


def main():
    global BT_START, BT_END
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', help='只跑指定 factor_id (逗号分隔)')
    ap.add_argument('--list', action='store_true', help='列出已注册 builder')
    ap.add_argument('--save', action='store_true', help='绩效同时写入 SQLite')
    ap.add_argument('--start', default=BT_START, help=f'回测起始 (默认 {BT_START})')
    ap.add_argument('--end', default=BT_END, help='回测截止 (默认 今天)')
    args = ap.parse_args()

    BT_START, BT_END = args.start, args.end

    registry = load_builders()
    if args.list:
        print(f'{len(registry)} 个已注册 builder:')
        for fid in sorted(registry):
            print(f'  {fid}  <- builders/{registry[fid][1]}.py')
        return

    only = set(args.only.split(',')) if args.only else None
    names = get_factor_names()
    ctx = get_ctx()

    results, failed = [], []
    todo = [(fid, b) for fid, b in sorted(registry.items()) if only is None or fid in only]
    print(f'[run] {len(todo)} 个因子待回测出图', flush=True)
    for i, (fid, (builder, src)) in enumerate(todo):
        t0 = time.time()
        try:
            r = run_one(fid, builder, ctx, names, save_db=args.save)
            results.append(r)
            merge_perf([r], [])          # 逐因子落盘, 防崩溃丢进度
            print(f'[{i+1}/{len(todo)}] {fid} ok ({time.time()-t0:.0f}s) -> {r["chart"]}', flush=True)
        except NotImplementedError as e:
            failed.append({'factor_id': fid, 'reason': f'未实现: {e}'})
            merge_perf([], [failed[-1]])
            print(f'[{i+1}/{len(todo)}] {fid} 未实现: {e}', flush=True)
        except Exception as e:
            failed.append({'factor_id': fid, 'reason': f'{type(e).__name__}: {e}'})
            merge_perf([], [failed[-1]])
            print(f'[{i+1}/{len(todo)}] {fid} 失败: {e}', flush=True)
            traceback.print_exc()

    print(f'\n完成 {len(results)}/{len(todo)}, 失败/未实现 {len(failed)}')
    print(f'图表目录: {CHART_DIR}')
    print(f'绩效汇总: results/chart_perf.json')


if __name__ == '__main__':
    main()
