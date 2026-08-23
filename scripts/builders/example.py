#!/usr/bin/env python3
"""builder 模板: 每本书一个 builder 文件, 每个因子一个注册函数。
引擎启动时自动发现本目录下全部 .py 并收集 @register 的函数。

契约 (详见 references/factor-builder-api.md):
- build(ctx) -> dict, type ∈ {timing, cross_section, rotation, screening}
- 只用 ctx 提供的共享数据 (piv/mkt/mktcap/stock_ind/citic_*/bench_ret/get_ttm...),
  不要自行加载数据
- 只输出"当日收盘可知"的信号: T+1 开盘成交、一字涨停剔除、ST 剔除、费用
  全部由引擎统一执行
"""
import numpy as np
import pandas as pd

BUILDERS = {}


def register(fid):
    def deco(fn):
        BUILDERS[fid] = fn
        return fn
    return deco


@register('DEMO-F01')
def build_demo_f01(ctx):
    """示例·择时: 市场温度 (上涨家数占比)。
    温度 >= 55 做多基准, <= 40 空仓, 介于之间半仓。"""
    mkt = ctx['mkt']
    n_up, n_dn = mkt['n_up'], mkt['n_dn']
    temp = 100 * n_up / (n_up + n_dn).clip(lower=1)
    position = pd.Series(np.select([temp >= 55, temp <= 40], [1.0, 0.0], default=0.5),
                         index=temp.index)
    return {'type': 'timing', 'position': position, 'bench': 'ZZ1000'}


@register('DEMO-F02')
def build_demo_f02(ctx):
    """示例·截面: 20 日反转 (过去 20 日跌幅越大排名越前)。"""
    adj = ctx['piv']['adj_close']
    factor = -adj.pct_change(20)          # 负号: 跌得多 = 分高
    return {'type': 'cross_section', 'factor': factor, 'freq': 'monthly', 'min_stocks': 30}
