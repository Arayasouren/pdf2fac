#!/usr/bin/env python3
"""数据加载层: 调用 data_source.py 的接口函数, 提供按月 pickle 缓存。
引擎与组合脚本只 import 本模块, 不直接碰数据源。

缓存目录: data_cache/ (sm_YYYYMM.pkl / eq_ / ttm_ / st_all / indices / citic_*)
当月数据每次重取, 历史月份读缓存; 缓存可整体删除后重建。
"""
import time
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / 'data_cache'
CACHE_DIR.mkdir(exist_ok=True)

INDEX_CODES = {
    'HS300':  ('000300.SH', '沪深300'),
    'ZZ500':  ('000905.SH', '中证500'),
    'ZZ1000': ('000852.SH', '中证1000'),
    'ZZ2000': ('932000.CSI', '中证2000'),   # 2013-12 起
    'ZZ800':  ('000906.SH', '中证800'),
    'WAllA':  ('881001.WI', '万得全A'),
}


def load_stock_market(start, end, refresh=False, verbose=True):
    """个股日行情, 按月分块缓存 sm_YYYYMM.pkl。"""
    import data_source as ds
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    parts = []
    for p in pd.period_range(start, end, freq='M'):
        cache = CACHE_DIR / f'sm_{p.strftime("%Y%m")}.pkl'
        if cache.exists() and not refresh and p.end_time <= pd.Timestamp.now():
            parts.append(pd.read_pickle(cache))
            continue
        df = ds.fetch_stock_market(str(max(start, p.start_time).date()),
                                   str(min(end, p.end_time).date()))
        df.to_pickle(cache)
        parts.append(df)
        if verbose:
            print(f'  stock_market {p} fetched', flush=True)
    out = pd.concat(parts, ignore_index=True)
    out['date'] = pd.to_datetime(out['date'])
    return out


def load_indices(start, end, refresh=False, verbose=True):
    """宽基指数日行情 dict {内部代码: DataFrame[open..close, ret, volume, amount]}"""
    import data_source as ds
    cache = CACHE_DIR / 'indices.pkl'
    if cache.exists() and not refresh:
        idx = pd.read_pickle(cache)
        if all(k in idx for k in INDEX_CODES):
            s, e = pd.Timestamp(start), pd.Timestamp(end)
            return {k: df.loc[s:e] for k, df in idx.items()}
    idx_raw = ds.fetch_indices(str(pd.Timestamp(start).date()), str(pd.Timestamp(end).date()))
    idx = {}
    for key, df in idx_raw.items():
        df = df.sort_values('date').set_index('date').astype(float)
        df['ret'] = df['close'].pct_change()
        idx[key] = df
    pd.to_pickle(idx, cache)
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    return {k: df.loc[s:e] for k, df in idx.items()}


def load_citic_l1(start, end, refresh=False):
    """一级行业指数日行情 dict {行业名: DataFrame}"""
    import data_source as ds
    cache = CACHE_DIR / 'industry_l1.pkl'
    if cache.exists() and not refresh:
        idx = pd.read_pickle(cache)
    else:
        idx = ds.fetch_industry_index(str(pd.Timestamp(start).date()),
                                      str(pd.Timestamp(end).date()), level=1)
        pd.to_pickle(idx, cache)
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    out = {}
    for k, df in idx.items():
        df = df.sort_values('date').set_index('date')
        if 'ret' not in df.columns:
            df['ret'] = df['close'].pct_change()
        out[k] = df.loc[s:e]
    return out


def load_citic_l2(start, end, refresh=False):
    """二级行业指数日行情 (可选, 用于更细颗粒度轮动)。"""
    import data_source as ds
    cache = CACHE_DIR / 'industry_l2.pkl'
    if cache.exists() and not refresh:
        return pd.read_pickle(cache)
    idx = ds.fetch_industry_index(str(pd.Timestamp(start).date()),
                                  str(pd.Timestamp(end).date()), level=2)
    pd.to_pickle(idx, cache)
    return idx


def load_stock_industry():
    """个股→行业映射 Series(stock_id -> 行业名)。"""
    import data_source as ds
    cache = CACHE_DIR / 'stock_industry.pkl'
    if cache.exists():
        return pd.read_pickle(cache)
    s = ds.fetch_stock_industry()
    pd.to_pickle(s, cache)
    return s


def load_stock_equity(start, end, refresh=False):
    """市值/股本, 按月缓存 eq_YYYYMM.pkl。"""
    import data_source as ds
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    parts = []
    for p in pd.period_range(start, end, freq='M'):
        cache = CACHE_DIR / f'eq_{p.strftime("%Y%m")}.pkl'
        if cache.exists() and not refresh and p.end_time <= pd.Timestamp.now():
            parts.append(pd.read_pickle(cache))
            continue
        df = ds.fetch_stock_equity(str(max(start, p.start_time).date()),
                                   str(min(end, p.end_time).date()))
        df.to_pickle(cache)
        parts.append(df)
    out = pd.concat(parts, ignore_index=True)
    out['date'] = pd.to_datetime(out['date'])
    return out


TTM_FIELDS = ['roe_ttm', 'roic_ttm', 'grossmargin_ttm', 'net_profit_ttm',
              'net_cash_flows_oper_act_ttm', 'asset_mrq', 'debt_mrq',
              'totalequity_mrq', 'debttoassets_mrq', 'ebitda_ttm', 'tot_oper_rev_ttm']


def load_stock_ttm(start, end, fields=None, refresh=False):
    """TTM 财务 (含 ann_date), 按月缓存 ttm_YYYYMM.pkl。"""
    import data_source as ds
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    parts = []
    for p in pd.period_range(start, end, freq='M'):
        cache = CACHE_DIR / f'ttm_{p.strftime("%Y%m")}.pkl'
        if cache.exists() and not refresh and p.end_time <= pd.Timestamp.now():
            parts.append(pd.read_pickle(cache))
            continue
        df = ds.fetch_stock_ttm(str(max(start, p.start_time).date()),
                                str(min(end, p.end_time).date()))
        if len(df):
            df.to_pickle(cache)
        parts.append(df)
    out = pd.concat(parts, ignore_index=True)
    out['date'] = pd.to_datetime(out['date'])
    out['ann_date'] = pd.to_datetime(out['ann_date'])
    return out


def ttm_pivot_pit(ttm, field, trade_dates):
    """单字段时点正确 pivot: 每个交易日取 ann_date <= 当日的最新值 (防前视)。"""
    df = ttm[['ann_date', 'stock_id', field]].dropna(subset=['ann_date', field])
    if len(df) == 0:
        return pd.DataFrame(index=trade_dates)
    piv = df.pivot_table(index='ann_date', columns='stock_id', values=field)
    union = trade_dates.union(piv.index).sort_values()
    return piv.reindex(union).ffill().reindex(trade_dates)


def load_st_status(start, end, refresh=False):
    """ST 标记, 全量缓存 st_all.pkl (当日有效)。"""
    import data_source as ds
    import datetime
    cache = CACHE_DIR / 'st_all.pkl'
    today = datetime.date.today().isoformat()
    if cache.exists() and not refresh:
        blob = pd.read_pickle(cache)
        if blob.get('date') == today:
            df = blob['df']
            return df[(df['date'] >= pd.Timestamp(start)) & (df['date'] <= pd.Timestamp(end))]
    df = ds.fetch_st_status(str(pd.Timestamp(start).date()), str(pd.Timestamp(end).date()))
    df['date'] = pd.to_datetime(df['date'])
    pd.to_pickle({'date': today, 'df': df}, cache)
    return df
