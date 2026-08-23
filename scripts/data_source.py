#!/usr/bin/env python3
"""数据接口层 — 由使用者实现。Pdf2Fac 引擎/管线只调用本文件的函数，
不直接依赖任何具体数据源（数据库/API/本地文件均可）。

实现要求：返回指定 schema 的 pandas DataFrame。日期列为 date (datetime64)，
股票代码为 stock_id (str)。实现后由 data_loader.py 负责按月缓存，
缓存可删可重建。

需要实现的 7 个函数（返回字段即下列签名注释；缺一不可）：
"""


def fetch_stock_market(start: str, end: str):
    """A股个股日行情。返回列:
    date, stock_id, open, high, low, close, volume, amt, pct_chg,
    adj_close, max_updown_close, trade_status
    说明:
    - pct_chg 为百分数 (如 9.98 表示 +9.98%); adj_close 后复权收盘价
    - max_updown_close: 收盘是否触及涨跌停 (1=涨停收盘, -1=跌停收盘, 0=未触及)
    - trade_status: 1=正常交易, 0=停牌
    - volume 单位: 手(100股); amt 单位: 千元 (与 ctx 内 VWAP=10*amt/volume 的口径一致,
      若你的数据单位不同, 请在本层换算后返回)
    """
    raise NotImplementedError('请在 data_source.py 中实现 fetch_stock_market')


def fetch_indices(start: str, end: str):
    """宽基指数日行情。返回 dict {指数代码: DataFrame}:
    至少包含 HS300(沪深300), ZZ500(中证500), ZZ1000(中证1000), ZZ2000(中证2000),
    建议另含 ZZ800(中证800) / WAllA(万得全A, 或你的全市场基准)。
    每个 DataFrame 列: date, open, high, low, close, volume, amount
    注: 中证2000 发布于 2013-12-31, 此前数据可缺省 (引擎会用中证1000 承接)。
    """
    raise NotImplementedError('请在 data_source.py 中实现 fetch_indices')


def fetch_industry_index(start: str, end: str, level: int = 1):
    """行业指数日行情 (轮动类回测用)。返回 dict {行业代码: DataFrame},
    列同 fetch_indices。level=1 为一级行业 (建议中信/申万一级, ~30 个),
    level=2 为二级行业 (~90-200 个, 可选)。"""
    raise NotImplementedError('请在 data_source.py 中实现 fetch_industry_index')


def fetch_stock_industry():
    """个股 → 一级行业映射 (最新快照即可)。返回 pandas Series:
    index=stock_id, values=行业名 (需与 fetch_industry_index 的行业口径对应)。"""
    raise NotImplementedError('请在 data_source.py 中实现 fetch_stock_industry')


def fetch_stock_equity(start: str, end: str):
    """个股市值/股本 (日频或低频均可)。返回列:
    date, stock_id, tot_share_cap (总市值), float_share_cap (流通市值)"""
    raise NotImplementedError('请在 data_source.py 中实现 fetch_stock_equity')


def fetch_stock_ttm(start: str, end: str):
    """TTM 财务指标 (基本面因子用)。返回列:
    date, stock_id, ann_date (公告日, 时点正确对齐的关键), 以及财务字段, 如:
    roe_ttm, roic_ttm, grossmargin_ttm (毛利额,元), net_profit_ttm,
    net_cash_flows_oper_act_ttm, asset_mrq, debt_mrq, totalequity_mrq,
    debttoassets_mrq, ebitda_ttm, tot_oper_rev_ttm ...
    注: 引擎按 ann_date 做时点正确对齐防前视; 字段缺啥基本面因子就少啥。
    """
    raise NotImplementedError('请在 data_source.py 中实现 fetch_stock_ttm')


def fetch_st_status(start: str, end: str):
    """ST 特殊处理标记 (日频)。返回列: date, stock_id, is_ST (0/1)。
    只需 is_ST=1 的行 (稀疏) 即可。"""
    raise NotImplementedError('请在 data_source.py 中实现 fetch_st_status')
