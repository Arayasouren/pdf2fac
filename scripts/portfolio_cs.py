#!/usr/bin/env python3
"""精选截面因子 → 50 只等权组合回测 (双边千3, T+1 开盘成交, 可成交口径 v2)

流程:
1. 跑精选集中全部 cross_section builder → 因子值 pivot (date×stock)
2. 训练窗口 (2010~2023) 逐因子日频 Spearman IC (向量化秩相关) → 符号 & |ICIR| 权重
3. 调仓: 信号日(月末/周末)截面 rank 标准化(NaN→中性0) → 符号对齐 → 加权合成
   → 取前 50; T+1 开盘成交
   可成交口径: 剔除上市<120交易日 / 成交日停牌 / 成交日一字涨停不可买;
              持仓遇一字跌停递延至次日卖(逐日重试)
4. 价格口径: 全部复权价 (adj_open = open × adj_close/close); 费用按边计
5. 变体 {equal, icir} × {monthly, weekly}, 训练期超额 IR 择优,
   训练/验证分样本对照; 支持费率敏感性扫描
6. 产出: results/portfolio/portfolio_nav.png + 组合报告.md + portfolio_metrics.json

用法: /usr/bin/python3 scripts/portfolio_cs.py [--cost 0.003]
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import factor_engine as fe

PROJ = Path(__file__).resolve().parent.parent
OUT = PROJ / 'results' / 'portfolio'
TRAIN_END = pd.Timestamp('2023-12-31')
VALID_START = pd.Timestamp('2025-01-01')
TOP_N = 50
MIN_HIST = 120          # 上市最少交易日数
FFILL_DAYS = 10         # 因子值在信号日前最多追溯 10 个交易日
COST_PER_SIDE = 0.0015  # 双边千3: 每边 0.15%, 往返 0.3%


# ---------------------------------------------------------------------------
# 1. 因子收集 & 训练期 IC
# ---------------------------------------------------------------------------
def collect_factors(ctx, factor_ids):
    registry = fe.load_builders()
    facs = {}
    for fid in factor_ids:
        t0 = time.time()
        builder, _src = registry[fid]
        spec = builder(ctx)
        assert spec['type'] == 'cross_section', f'{fid} 非截面类: {spec["type"]}'
        f = spec['factor']
        facs[fid] = f
        dens = f.notna().mean().mean()
        print(f'  {fid:12s} pivot {f.shape} 非NaN密度 {dens:.1%} ({time.time()-t0:.0f}s)', flush=True)
    return facs


def factor_ic_stats(fac, fwd_ret, train_mask):
    """向量化 Spearman 日 IC (截面秩的 Pearson 相关)。返回 (ic_mean, icir, n)。
    口径与 v2 可成交口径一致: 前向收益从 T+1 复权开盘价起算, T+1 一字涨停不可买的
    股票当日不参与 IC (掩码 NaN)。"""
    Fr = fac.rank(axis=1, pct=True)
    Rr = fwd_ret.rank(axis=1, pct=True)
    valid = Fr.notna() & Rr.notna()
    Fm, Rm = Fr.where(valid), Rr.where(valid)
    n = valid.sum(axis=1).astype(float)
    n = n.where(n >= 30)                      # 与截面回测 min_stocks=30 对齐
    cov = (Fm * Rm).sum(axis=1) / n - Fm.mean(axis=1) * Rm.mean(axis=1)
    ic = (cov / (Fm.std(axis=1) * Rm.std(axis=1)))
    ic = ic.replace([np.inf, -np.inf], np.nan).dropna()   # 防零方差日污染
    ic = ic[train_mask.reindex(ic.index).fillna(False)]
    if len(ic) < 30:
        return 0.0, 0.0, len(ic)
    return float(ic.mean()), float(ic.mean() / (ic.std() + 1e-12)), len(ic)


# ---------------------------------------------------------------------------
# 2. 组合模拟 (股数制, 逐日结算, 精确费用)
# ---------------------------------------------------------------------------
def simulate(score_by_sig, cal, open_a, close_a, tradable_buy, yizi_dn,
             cost, end_date, buffer=50, top_n=None, min_hold=1):
    """score_by_sig: {信号日: 合成分 Series}; open_a/close_a: 复权开/收 dict[date]->Series
    buffer: 排名缓冲区 — 持仓股跌出前 buffer 名才卖出 (buffer=top_n 即无缓冲),
    降低换手与费用。top_n: 持仓只数 (默认模块级 TOP_N)。
    min_hold: 最短持有交易日数 — 未满即使跌出缓冲区也不卖。
    返回 (nav, ret, turn, n_hold)"""
    nxt = {cal[i]: cal[i + 1] for i in range(len(cal) - 1)}
    didx = {d: i for i, d in enumerate(cal)}
    TN = top_n or TOP_N
    keep_n = max(buffer, TN)
    rebal = {}
    for sd, row in score_by_sig.items():
        td = nxt.get(sd)
        if td is None or td > pd.Timestamp(end_date):
            continue
        row = row.dropna().sort_values(ascending=False)
        ranked = [s for s in row.index if (td, s) in tradable_buy]
        rebal[td] = {'buy': ranked[:TN], 'keep': set(ranked[:keep_n])}
    if not rebal:
        raise RuntimeError('无有效调仓日')
    first_td = min(rebal)
    days = [d for d in cal if first_td <= d <= pd.Timestamp(end_date)]

    pos = {}            # stock -> {'shares':, 'last_px':}
    pending = set()     # 递延卖出 (一字跌停/停牌)
    cash = 1.0
    nav_s, turn_s, n_hold_s = {}, {}, {}
    for d in days:
        open_d, close_d = open_a[d], close_a[d]
        nav_prev = cash + sum(st['shares'] * st['last_px'] for st in pos.values())
        # ---- 开盘卖出: 跌出缓冲区才卖 + 递延重试 ----
        to_sell = set(pending)
        if d in rebal:
            keep = rebal[d]['keep']
            to_sell |= {s for s in pos if s not in keep}
        day_turn = 0.0
        for s in sorted(to_sell):
            st = pos.get(s)
            if st is None:
                pending.discard(s)
                continue
            if didx[d] - st.get('entry_i', 0) < min_hold:
                continue                     # 最短持有期未满, 不卖
            px = open_d.get(s, np.nan)
            if np.isnan(px) or (d, s) in yizi_dn:
                pending.add(s)
                continue
            amt = st['shares'] * px
            cash += amt * (1 - cost)
            day_turn += amt / max(nav_prev, 1e-12)
            del pos[s]
            pending.discard(s)
        # ---- 开盘买入 ----
        if d in rebal:
            cands = [s for s in rebal[d]['buy'] if s not in pos][:max(TN - len(pos), 0)]
            if cands:
                nav_now = cash + sum(st['shares'] * st['last_px'] for st in pos.values())
                per = min(nav_now / TN, cash / len(cands))
                for s in cands:
                    px = open_d.get(s, np.nan)
                    if np.isnan(px) or per <= 1e-12:
                        continue
                    invest = min(per, cash)
                    pos[s] = {'shares': invest * (1 - cost) / px, 'last_px': px,
                              'entry_i': didx[d]}
                    cash -= invest
                    day_turn += invest / max(nav_now, 1e-12)
        # ---- 收盘计价 ----
        for s, st in pos.items():
            c = close_d.get(s, np.nan)
            if not np.isnan(c):
                st['last_px'] = c
        nav_s[d] = cash + sum(st['shares'] * st['last_px'] for st in pos.values())
        if day_turn:
            turn_s[d] = day_turn
        n_hold_s[d] = len(pos)
    nav = pd.Series(nav_s)
    return nav, nav.pct_change().fillna(0.0), pd.Series(turn_s), pd.Series(n_hold_s)


# ---------------------------------------------------------------------------
# 3. 绩效统计
# ---------------------------------------------------------------------------
def perf_stats(ret, bench_ret, turn=None):
    df = pd.DataFrame({'p': ret, 'b': bench_ret}).dropna()
    ex = df['p'] - df['b']
    nav, bnav = (1 + df['p']).cumprod(), (1 + df['b']).cumprod()
    enav = nav / bnav
    yrs = len(df) / 252
    ann, bann = nav.iloc[-1] ** (1 / yrs) - 1, bnav.iloc[-1] ** (1 / yrs) - 1
    ex_ann = (1 + ann) / (1 + bann) - 1
    edd = (enav / enav.cummax() - 1).min()
    stats = {
        'annual_ret': float(ann), 'bench_annual': float(bann), 'excess_annual': float(ex_ann),
        'sharpe': float(df['p'].mean() / (df['p'].std() + 1e-12) * np.sqrt(252)),
        'max_dd': float((nav / nav.cummax() - 1).min()), 'excess_max_dd': float(edd),
        'IR': float(ex.mean() / (ex.std() + 1e-12) * np.sqrt(252)),
        'calmar_excess': float(ex_ann / abs(edd)) if edd < 0 else None,
        'monthly_win': float((ex.resample('M').sum() > 0).mean()),
        'yearly_excess': {int(y): float((1 + df.loc[g.index, 'p']).prod() / (1 + df.loc[g.index, 'b']).prod() - 1)
                          for y, g in ex.groupby(ex.index.year)},
    }
    if turn is not None and len(turn):
        stats['avg_monthly_turnover'] = float(turn.resample('M').sum().mean())
    return stats, nav, bnav, enav


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cost', type=float, default=COST_PER_SIDE, help='单边费率 (默认 0.003 = 双边千3)')
    ap.add_argument('--factors', type=str, default='',
                    help='逗号分隔的因子ID列表, 指定后跳过自动精选 (如 TS151-F07,WXM-F07)')
    ap.add_argument('--tag', type=str, default='', help='输出文件名后缀 (默认 portfolio)')
    args = ap.parse_args()

    if args.factors:
        cs_ids = [s.strip() for s in args.factors.split(',') if s.strip()]
        print(f'手动指定因子 {len(cs_ids)} 个: {cs_ids}', flush=True)
    else:
        from filter_factors import pass_criteria, load_curated
        data = json.load(open(PROJ / 'results' / 'chart_perf.json'))
        curated = load_curated()
        cs_ids = sorted(r['factor_id'] for r in data['results']
                        if r['type'] == 'cross_section'
                        and (pass_criteria(r) or r['factor_id'] in curated))
        print(f'精选截面因子 {len(cs_ids)} 个: {cs_ids}', flush=True)

    ctx = fe.get_ctx()
    print('[1/4] 跑截面 builder ...', flush=True)
    facs = collect_factors(ctx, cs_ids)

    piv = ctx['piv']
    close, open_, adj = piv['close'], piv['open'], piv['adj_close']
    adj_open = open_ * (adj / close)
    ret_d = adj.pct_change()
    cal = list(adj.index)
    trade_dates = pd.DatetimeIndex(cal)
    # 基准: 中证2000 (932000.CSI) 2013-12-31 才发布, 此前用同为小盘宽基的中证1000 承接
    # (已核实: ZZ2000 起于 2013-12-31, ZZ1000 起于 2004-12-31, indices_v4.pkl)。
    # 单位: bench_ret 是 pct_change 小数 (与引擎内核一致), 不要再除 100!
    zz2000 = ctx['bench_ret']['ZZ2000'].reindex(trade_dates)
    zz1000 = ctx['bench_ret']['ZZ1000'].reindex(trade_dates)
    bench = zz2000.where(zz2000.notna(), zz1000).fillna(0.0)
    zz2000_ret = zz2000.fillna(0.0)           # 纯 ZZ2000 口径, 供 2014+ 子窗口

    print('[2/4] 可成交掩码 & 训练期 IC ...', flush=True)
    # 一字跌停(近似): is_ld 且 开收相等; 一字涨停直接用 is_yizi
    yd = (piv['is_ld'].fillna(0).astype(bool) & ((piv['open'] - piv['close']).abs() < 0.01))
    yizi_dn = set(yd.stack()[lambda x: x].index)
    hist_cnt = close.notna().cumsum()
    ok_basic = open_.notna() & ~piv['is_yizi'].fillna(0).astype(bool) & (hist_cnt >= MIN_HIST)
    tradable_buy = set(ok_basic.stack()[lambda x: x].index)
    open_d = adj_open.to_dict('index')
    close_d = adj.to_dict('index')

    train_mask = pd.Series(trade_dates <= TRAIN_END, index=trade_dates)
    # v2 可成交口径前向收益: 信号日 t → T+1 复权开盘进场 → 持有 horizon 日至开盘;
    # T+1 一字涨停的股票不可买, IC 中掩掉 (与买入候选剔除一致)
    yizi_t1 = piv['is_yizi'].fillna(0).astype(bool).shift(-1).reindex(trade_dates).fillna(False)
    IC_FLOOR = 0.003                        # |IC| 低于此视为噪声, 不参与合成
    HORIZONS = {'monthly': 21, 'weekly': 5, 'quarterly': 63}

    def fwd_ret(h):
        f = adj_open.shift(-(h + 1)) / adj_open.shift(-1) - 1.0
        return f.mask(yizi_t1.reindex(f.index).fillna(False))

    ic_stats = {}
    for freq, h in HORIZONS.items():
        fr = fwd_ret(h)
        ic_stats[freq] = {}
        for fid, f in facs.items():
            ic, icir, n = factor_ic_stats(f, fr, train_mask)
            ic_stats[freq][fid] = {'ic': ic, 'icir': icir, 'n': n}
            mark = ' (噪声剔除)' if abs(ic) < IC_FLOOR else ''
            print(f'  {fid:12s} [{freq} h={h}d] train IC {ic:+.4f} ICIR {icir:+.2f} (n={n}){mark}',
                  flush=True)

    print('[3/4] 合成打分 & 网格扫描 (子集×频率×缓冲区) ...', flush=True)
    fac_ff = {fid: f.ffill(limit=FFILL_DAYS) for fid, f in facs.items()}
    md = pd.Series(trade_dates, index=trade_dates)
    month_ends = list(md.groupby(md.index.to_period('M')).max())
    week_ends = list(md.groupby(md.index.to_period('W')).max())
    quarter_ends = list(md.groupby(md.index.to_period('Q')).max())
    warmup_end = trade_dates[0] + pd.Timedelta(days=400)

    def make_subset(freq, name):
        """因子子集: all16=过噪声下限全体; strong=|IC|>=0.03; top8=|ICIR|前8"""
        stats = ic_stats[freq]
        ok = {fid: st for fid, st in stats.items()
              if st['n'] >= 30 and abs(st['ic']) >= IC_FLOOR}
        if name == 'all16':
            return set(ok)
        if name == 'strong':
            return {fid for fid, st in ok.items() if abs(st['ic']) >= 0.03}
        return set(sorted(ok, key=lambda f: -abs(ok[f]['icir']))[:8])

    def composite(sig_date, freq, fids):
        """ICIR 加权 (前两轮一致胜出, 固定); 符号/权重均来自训练期"""
        sc = None
        for fid in fids:
            f = fac_ff[fid]
            st = ic_stats[freq][fid]
            sign = 1.0 if st['ic'] > 0 else -1.0
            wgt = abs(st['icir'])
            row = f.loc[sig_date] if sig_date in f.index else None
            if row is None:
                continue
            contrib = (row.rank(pct=True) - 0.5).fillna(0.0) * sign * wgt
            sc = contrib if sc is None else sc.add(contrib, fill_value=0.0)
        return sc

    def run_variant(score_by_sig, cost, buffer=50):
        nav, ret, turn, nhold = simulate(score_by_sig, cal, open_d, close_d,
                                         tradable_buy, yizi_dn, cost, fe.BT_END, buffer)
        b_ret = bench.reindex(ret.index)
        st, pnav, bnav, enav = perf_stats(ret, b_ret, turn)
        tr, va = ret.index <= TRAIN_END, ret.index >= VALID_START
        st_tr, *_ = perf_stats(ret[tr], b_ret[tr], turn[turn.index <= TRAIN_END])
        st_va, *_ = perf_stats(ret[va], b_ret[va], turn[turn.index >= VALID_START])
        # 纯中证2000 口径子窗口 (2014 起 ZZ2000 才有真实数据)
        s14 = ret.index >= pd.Timestamp('2014-01-01')
        b14 = zz2000_ret.reindex(ret.index)
        st_14, *_ = perf_stats(ret[s14], b14[s14], turn[turn.index >= pd.Timestamp('2014-01-01')])
        return {'full': st, 'train': st_tr, 'valid': st_va, 'since2014': st_14,
                'nav': pnav, 'bnav': bnav, 'enav': enav, 'turn': turn, 'nhold': nhold,
                'ret': ret}

    FREQ_SIGS = {'weekly': week_ends, 'monthly': month_ends, 'quarterly': quarter_ends}
    grid = {}
    for freq, sigs in FREQ_SIGS.items():
        sig_dates = [d for d in sigs
                     if d >= warmup_end and d >= pd.Timestamp(fe.BT_START)]
        for sub in ('all16', 'strong', 'top8'):
            fids = make_subset(freq, sub)
            if not fids:
                continue
            t0 = time.time()
            score = {d: composite(d, freq, fids) for d in sig_dates}
            for buf in (80, 120):
                key = f'{freq}_{sub}_b{buf}'
                r = run_variant(score, args.cost, buffer=buf)
                r['fids'] = sorted(fids)
                grid[key] = r
                st, st_tr, st_va = r['full'], r['train'], r['valid']
                print(f"  {key:22s} 全期超额 {st['excess_annual']:+.1%} IR {st['IR']:.2f} "
                      f"换手 {st.get('avg_monthly_turnover', 0):.0%} | "
                      f"train IR {st_tr['IR']:.2f} / valid IR {st_va['IR']:.2f} "
                      f"({time.time()-t0:.0f}s)", flush=True)

    best_key = max(grid, key=lambda k: grid[k]['train']['IR'])
    # 验证期闸门 (与 refine 的退化否决同哲学): train IR 再高, valid IR<=0 判过拟合出局
    gated = {k: v for k, v in grid.items() if v['valid']['IR'] > 0}
    if gated and grid[best_key]['valid']['IR'] <= 0:
        best_key = max(gated, key=lambda k: gated[k]['train']['IR'])
        print(f'  [验证闸门] 原train最优 valid IR<=0 出局, 改选 {best_key}', flush=True)
    final = grid[best_key]
    best_freq, best_sub = best_key.split('_')[0], best_key.split('_')[1]
    best_buf = int(best_key.rsplit('_b', 1)[1])
    print(f'[4/4] 训练期 IR 择优: {best_key} ({len(final["fids"])} 因子); 留一消融 ...', flush=True)

    # 留一消融 (LOO): 赢家配置上逐因子剔除, 看训练期 IR 变化 — 回答"16个因子是否都需要"
    sig_dates_w = [d for d in FREQ_SIGS[best_freq]
                   if d >= warmup_end and d >= pd.Timestamp(fe.BT_START)]
    base_ir = final['train']['IR']
    loo = {}
    for fid in final['fids']:
        sub = set(final['fids']) - {fid}
        score = {d: composite(d, best_freq, sub) for d in sig_dates_w}
        r = run_variant(score, args.cost, buffer=best_buf)
        loo[fid] = {'train_IR': r['train']['IR'], 'delta_ir': r['train']['IR'] - base_ir,
                    'full_excess': r['full']['excess_annual'],
                    'valid_IR': r['valid']['IR']}
        print(f"  剔除 {fid:12s} train IR {r['train']['IR']:.2f} (Δ{r['train']['IR']-base_ir:+.2f}) "
              f"全期超额 {r['full']['excess_annual']:+.1%}", flush=True)

    # 敏感性/多基准需要赢家的 score: 用完整子集重建一次
    final['score_by_sig'] = {d: composite(d, best_freq, set(final['fids'])) for d in sig_dates_w}
    results = grid
    buf_results = {}

    # 费率敏感性: 最终组合在 0 / 千1.5 / 千3(基准) / 千6 每边
    sens = {}
    for c in (0.0, 0.0015, 0.003, 0.006):
        if abs(c - args.cost) < 1e-12:
            sens[c] = final['full']
            continue
        r = run_variant(final['score_by_sig'], c, buffer=best_buf)
        sens[c] = r['full']
        print(f"  单边 {c:.2%}: 全期超额 {r['full']['excess_annual']:+.1%} IR {r['full']['IR']:.2f}", flush=True)

    # 多基准对照 (最终组合): 国证2000(2009底起全程)/中证1000/沪深300
    multi = {}
    for name, code in (('国证2000', 'GZ2000'), ('中证1000', 'ZZ1000'), ('沪深300', 'HS300')):
        bs = ctx['bench_ret'].get(code)
        if bs is None:
            continue
        br = bs.reindex(trade_dates).fillna(0.0).reindex(final['ret'].index)
        st_f, *_ = perf_stats(final['ret'], br)
        va = final['ret'].index >= VALID_START
        st_v, *_ = perf_stats(final['ret'][va], br[va])
        multi[name] = {'full': st_f, 'valid': st_v}
        print(f"  vs {name}: 全期超额 {st_f['excess_annual']:+.1%} IR {st_f['IR']:.2f} | "
              f"valid {st_v['excess_annual']:+.1%} IR {st_v['IR']:.2f}", flush=True)

    write_outputs(results, best_key, best_buf, final, loo, ic_stats, args.cost, sens, multi,
                  tag=args.tag)


def write_outputs(results, best_key, best_buf, final, loo, ic_stats, cost, sens, multi=None,
                  tag=''):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif'] = ['Hiragino Sans GB', 'Arial Unicode MS', 'sans-serif']
    plt.rcParams['axes.unicode_minus'] = False

    OUT.mkdir(parents=True, exist_ok=True)
    nav, bnav, enav = final['nav'], final['bnav'], final['enav']
    combo = best_key
    n_fac = len(final['fids'])

    fig, axes = plt.subplots(3, 1, figsize=(11, 9), sharex=True,
                             gridspec_kw={'height_ratios': [3, 2, 1]})
    axes[0].plot(nav.index, nav.values, label=f'组合 ({combo})', lw=1.2)
    axes[0].plot(bnav.index, bnav.values, label='中证2000', lw=1.0, alpha=0.8)
    axes[0].set_yscale('log')
    axes[0].legend()
    axes[0].set_title(f'50只等权组合 vs 中证2000 (双边千3: 单边{cost:.1%}) — {n_fac}个精选截面因子合成')
    axes[1].plot(enav.index, enav.values, color='#C0392B', lw=1.2)
    axes[1].set_title('超额净值 (组合/基准)')
    edd = enav / enav.cummax() - 1
    axes[2].fill_between(edd.index, edd.values, 0, color='#7F8C8D', alpha=0.6)
    axes[2].set_title('超额回撤')
    for ax in axes:
        ax.grid(alpha=0.3)
    plt.tight_layout()
    suffix = f"_{tag}" if tag else ''
    fig.savefig(OUT / f'portfolio_nav{suffix}.png', dpi=150)
    plt.close(fig)

    outj = {'best': best_key, 'cost_per_side': cost, 'ic_stats': ic_stats, 'loo': loo,
            'sensitivity': {str(c): {k: v for k, v in s.items() if k != 'yearly_excess'}
                            for c, s in sens.items()},
            'multi_bench': {k: {'full': v['full'], 'valid': v['valid']}
                            for k, v in (multi or {}).items()},
            'grid': {k: {'full': v['full'], 'train': v['train'], 'valid': v['valid'],
                         'fids': v['fids']}
                     for k, v in results.items()}}
    (OUT / f'portfolio_metrics{suffix}.json').write_text(
        json.dumps(outj, ensure_ascii=False, indent=1, default=str), encoding='utf-8')

    freq_cn = {'weekly': '周度', 'monthly': '月度', 'quarterly': '季度'}[best_key.split('_')[0]]
    L = ['# 精选截面因子 50 只组合报告', '']
    L.append(f'- 因子: {n_fac} 个精选截面因子 (子集方案见下), 训练期(2010~2023) IC 定符号与 ICIR 权重, 无前视')
    L.append(f'- 调仓: **{freq_cn}** 信号, T+1 开盘成交, 排名缓冲区 **前{best_buf}名内持有**')
    L.append(f'- 费用: 双边千3 (单边 {cost:.1%}, 往返 {2*cost:.1%}); 基准 中证2000')
    L.append('- **基准拼接说明**: 中证2000 指数 2013-12-31 才发布, 全期/训练窗口在 2014 年前'
             '用中证1000 (同为小盘宽基, 2004 年起有数据) 承接; "2014至今" 行为纯中证2000 口径')
    L.append('- 可成交口径: 剔除上市<120日/停牌/一字涨停不可买; 一字跌停递延卖出')
    L.append(f'- 最终组合因子 ({n_fac}): {", ".join(final["fids"])}')
    L.append('')
    L.append(f'## 最终组合 {combo} (训练期 IR 择优)')
    L.append('')
    L.append('| 窗口 | 年化 | 基准 | 超额 | IR | 超额最大回撤 | Calmar | 月胜率 | 月均换手 |')
    L.append('|---|---|---|---|---|---|---|---|---|')
    for name, st in [('全期 2010-2026', final['full']),
                     ('2014至今 (纯中证2000)', final['since2014']),
                     ('训练 2010-2023', final['train']),
                     ('验证 2025-2026.07', final['valid'])]:
        L.append(f"| {name} | {st['annual_ret']:.1%} | {st['bench_annual']:.1%} | "
                 f"**{st['excess_annual']:+.1%}** | {st['IR']:.2f} | {st['excess_max_dd']:.1%} | "
                 f"{st.get('calmar_excess') or 0:.2f} | {st['monthly_win']:.0%} | "
                 f"{st.get('avg_monthly_turnover', 0):.0%} |")
    L.append('')
    L.append('## 网格扫描 (子集×频率×缓冲区, 按 train IR 排序)')
    L.append('')
    L.append('| 组合 | 全期超额 | 全期IR | 超额回撤 | 月均换手 | train IR | valid IR |')
    L.append('|---|---|---|---|---|---|---|')
    for k, v in sorted(results.items(), key=lambda kv: -kv[1]['train']['IR']):
        mark = ' ←' if k == best_key else ''
        L.append(f"| {k} | {v['full']['excess_annual']:+.1%} | {v['full']['IR']:.2f} | "
                 f"{v['full']['excess_max_dd']:.1%} | {v['full'].get('avg_monthly_turnover', 0):.0%} | "
                 f"{v['train']['IR']:.2f} | {v['valid']['IR']:.2f} |{mark}")
    L.append('')
    L.append('## 留一消融 (LOO): 逐因子剔除对训练期 IR 的影响')
    L.append('')
    L.append('ΔIR < 0 = 剔除后变差 = 该因子有正贡献; ΔIR > 0 = 剔除后更好 = 该因子在拖累。')
    L.append('')
    L.append('| 剔除因子 | train IR | ΔIR | 全期超额 | valid IR |')
    L.append('|---|---|---|---|---|')
    base_ir = final['train']['IR']
    L.append(f'| (基准 {n_fac} 因子) | {base_ir:.2f} | — | {final["full"]["excess_annual"]:+.1%} | '
             f'{final["valid"]["IR"]:.2f} |')
    for fid, v in sorted(loo.items(), key=lambda kv: kv[1]['delta_ir']):
        L.append(f"| {fid} | {v['train_IR']:.2f} | {v['delta_ir']:+.2f} | "
                 f"{v['full_excess']:+.1%} | {v['valid_IR']:.2f} |")
    L.append('')
    L.append('## 费率敏感性 (最终组合, 全期)')
    L.append('')
    L.append('| 单边费率 | 超额年化 | IR | 超额回撤 |')
    L.append('|---|---|---|---|')
    for c, s in sorted(sens.items()):
        L.append(f"| {c:.2%} | {s['excess_annual']:+.1%} | {s['IR']:.2f} | {s['excess_max_dd']:.1%} |")
    L.append('')
    if multi:
        L.append('## 多基准对照 (最终组合)')
        L.append('')
        L.append('| 基准 | 全期超额 | 全期IR | 验证期超额 | 验证期IR |')
        L.append('|---|---|---|---|---|')
        L.append(f"| 中证2000(拼接) | {final['full']['excess_annual']:+.1%} | {final['full']['IR']:.2f} | "
                 f"{final['valid']['excess_annual']:+.1%} | {final['valid']['IR']:.2f} |")
        for name, v in multi.items():
            L.append(f"| {name} | {v['full']['excess_annual']:+.1%} | {v['full']['IR']:.2f} | "
                     f"{v['valid']['excess_annual']:+.1%} | {v['valid']['IR']:.2f} |")
        L.append('')
    L.append('## 分年超额 (最终组合)')
    L.append('')
    L.append('| 年份 | 超额 |')
    L.append('|---|---|')
    for y, e in final['full']['yearly_excess'].items():
        L.append(f'| {y} | {e:+.1%} |')
    L.append('')
    L.append(f'![净值](portfolio_nav{suffix}.png)')
    (OUT / f'组合报告{suffix}.md').write_text('\n'.join(L), encoding='utf-8')
    print(f'产出: {OUT}/portfolio_nav{suffix}.png, 组合报告{suffix}.md, portfolio_metrics{suffix}.json')


if __name__ == '__main__':
    main()
