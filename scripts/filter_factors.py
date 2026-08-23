#!/usr/bin/env python3
"""
精选因子导出 — 把回测表现好的因子归集到 results/filtered/

- 从 results/chart_perf.json 读全量绩效, 按类型阈值筛选
- 拷贝图表到 results/filtered/charts/
- 生成 results/filtered/精选因子.md (内嵌图表, 按类型分组, 按关键指标排序)

筛选标准（可按需调整）：
  timing        : 年化超额 >= 3% 且 夏普 >= 0.3
  cross_section : |IC| >= 0.03 且 |ICIR| >= 0.5 且 有效期数 >= 30
  rotation      : 年化超额 >= 3% 且 胜率 >= 0.48
  screening     : 年化超额 >= 5% 且 总笔数 >= 30 且 持仓覆盖率 >= 5% (防小样本)

人工精选：项目根目录 `curated_factors.txt`（每行一个 factor_id，# 注释）
存在时与自动阈值筛选取**并集**，报告中标注 [人工精选]。

用法: /usr/bin/python3 scripts/filter_factors.py
"""

import json
import shutil
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = PROJECT_ROOT / 'results' / 'charts'
OUT_DIR = PROJECT_ROOT / 'results' / 'filtered'
OUT_CHARTS = OUT_DIR / 'charts'

# 筛选阈值
CRITERIA = {
    'timing':        {'excess': 0.03, 'sharpe': 0.3},
    'cross_section': {'ic_abs': 0.03, 'icir_abs': 0.5, 'n_periods': 30},
    'rotation':      {'excess_ret': 0.03, 'win_rate': 0.48},
    'screening':     {'excess_annual_ret': 0.05, 'total_trades': 30, 'hold_coverage': 0.05},
}

TYPE_CN = {'timing': '择时', 'cross_section': '截面', 'rotation': '轮动', 'screening': '筛选'}
SORT_KEY = {'timing': 'excess', 'cross_section': 'icir', 'rotation': 'excess_ret',
            'screening': 'excess_annual_ret'}


def pass_criteria(r):
    t = r['type']
    if t == 'timing':
        c = CRITERIA['timing']
        return (r.get('excess') or 0) >= c['excess'] and (r.get('sharpe') or 0) >= c['sharpe']
    if t == 'cross_section':
        c = CRITERIA['cross_section']
        return (abs(r.get('ic_mean') or 0) >= c['ic_abs']
                and abs(r.get('icir') or 0) >= c['icir_abs']
                and (r.get('n_periods') or 0) >= c['n_periods'])
    if t == 'rotation':
        c = CRITERIA['rotation']
        return (r.get('excess_ret') or 0) >= c['excess_ret'] and (r.get('win_rate') or 0) >= c['win_rate']
    if t == 'screening':
        c = CRITERIA['screening']
        return ((r.get('excess_annual_ret') or 0) >= c['excess_annual_ret']
                and (r.get('total_trades') or 0) >= c['total_trades']
                and (r.get('hold_coverage') or 0) >= c['hold_coverage'])
    return False


def metric_line(r, pool_tags=None):
    pool_tags = pool_tags or {}
    t = r['type']
    if t == 'timing':
        return (f"策略年化 **{r['annual_ret']:.1%}** | 基准 {r['benchmark_annual_ret']:.1%} | "
                f"超额 **{r['excess']:+.1%}** | 夏普 {r['sharpe']:.2f} | 最大回撤 {r['max_dd']:.1%} | "
                f"多头占比 {r.get('long_days_pct', 0):.0%}")
    if t == 'cross_section':
        pool = f" | 池子：{pool_tags.get(r['factor_id'], '未统计')}" if pool_tags else ''
        return (f"IC **{r['ic_mean']:+.3f}** | ICIR **{r['icir']:+.2f}** | IC>0占比 {r['ic_pos_pct']:.0%} | "
                f"G1年化 {r['g1_ann']:.1%} / G10年化 {r['g10_ann']:.1%} | 有效期数 {r['n_periods']}{pool}")
    if t == 'rotation':
        return (f"多头年化 **{r['long_annual_ret']:.1%}** | 超额 **{r['excess_ret']:+.1%}** | "
                f"胜率 {r['win_rate']:.0%} | 最大回撤 {r['max_dd']:.1%} | IC {r.get('ic_mean', 0):+.3f}")
    if t == 'screening':
        return (f"组合年化 **{r['annual_ret']:.1%}** | 超额 **{r['excess_annual_ret']:+.1%}** | "
                f"夏普 {r['sharpe']:.2f} | 回撤 {r['max_dd']:.1%} | 笔数 {r['total_trades']} | "
                f"覆盖率 {r.get('hold_coverage', 0):.0%}")
    return ''


def load_curated():
    """读取人工精选名单 (curated_factors.txt)"""
    path = PROJECT_ROOT / 'curated_factors.txt'
    if not path.exists():
        return set()
    return {ln.strip() for ln in path.read_text(encoding='utf-8').splitlines()
            if ln.strip() and not ln.startswith('#')}


def load_pool_tags():
    """截面因子的池子规模标注 (来自 pool_stats.json; 无该文件则返回空)"""
    path = PROJECT_ROOT / 'results' / 'pool_stats.json'
    if not path.exists():
        return {}
    ps = json.load(open(path))
    tags = {}
    for s in ps['stats']:
        if s.get('landed_type') != 'cross_section':
            continue
        med = s.get('pool_median')
        if med is None:
            continue
        if med >= 2000:
            tag = f'全A({med:.0f}只)'
        elif med >= 200:
            tag = f'中池({med:.0f}只)'
        elif med >= 30:
            tag = f'事件小池({med:.0f}只)'
        else:
            tag = f'迷你池({med:.0f}只,改判筛选)'
        tags[s['factor_id']] = tag
    return tags


def main():
    data = json.load(open(PROJECT_ROOT / 'results' / 'chart_perf.json'))
    results = data['results']
    pool_tags = load_pool_tags()

    conn = sqlite3.connect(PROJECT_ROOT / 'db' / 'factor.db')
    meta = {r[0]: {'name': r[1], 'book': r[2], 'definition': r[3], 'construction': r[4]}
            for r in conn.execute('SELECT factor_id, factor_name, source_book, definition, '
                                  'construction FROM factor_meta').fetchall()}
    conn.close()

    curated = load_curated()
    auto_ids = {r['factor_id'] for r in results if pass_criteria(r)}
    # 并集: 自动阈值 OR 人工名单
    picked = [r for r in results
              if r['factor_id'] in auto_ids or r['factor_id'] in curated]
    n_auto_only = len(auto_ids - curated)
    n_curated_only = len(curated - auto_ids)
    # 按类型+关键指标排序
    picked.sort(key=lambda r: (list(TYPE_CN).index(r['type']),
                               -abs(r.get(SORT_KEY[r['type']]) or 0)))

    OUT_CHARTS.mkdir(parents=True, exist_ok=True)
    counts = {t: 0 for t in TYPE_CN}
    lines = [
        '# 精选因子（回测表现优秀）',
        '',
        f'> 生成自 `scripts/filter_factors.py`，数据源 `results/chart_perf.json`。',
        '> 筛选标准：择时 超额≥3%且夏普≥0.3 ｜ 截面 |IC|≥0.03且|ICIR|≥0.5且期数≥30 ｜ '
        '轮动 超额≥3%且胜率≥48% ｜ 筛选 超额≥5%且笔数≥30且覆盖率≥5%。',
        '>',
        '> ⚠️ **口径警示**：回测不含手续费滑点；打板/筛选类因子的 T+1 全天收益包含封板隔夜溢价，'
        '实盘可成交性受限，筛选类年化数字建议按 3~5 折解读；截面负 IC 因子为回避/反向有效。',
        '',
    ]

    cur_type = None
    for r in picked:
        t = r['type']
        if t != cur_type:
            cur_type = t
            lines.append(f'\n## {TYPE_CN[t]}因子\n')
        fid = r['factor_id']
        m = meta.get(fid, {'name': fid, 'book': '?', 'definition': '', 'construction': ''})
        name, book = m['name'], m['book']
        png_src = CHART_DIR / f'{fid}.png'
        type_dir = OUT_CHARTS / t
        type_dir.mkdir(parents=True, exist_ok=True)
        if png_src.exists():
            shutil.copy2(png_src, type_dir / f'{fid}.png')
        counts[t] += 1
        tag = ' 🏷️人工精选' if (fid in curated and fid not in auto_ids) else ''
        lines.append(f'### {fid} {name}{tag}')
        lines.append(f'- 来源：{book} ｜ {metric_line(r, pool_tags)}')
        if m['definition']:
            lines.append(f'- **定义**：{m["definition"]}')
        if m['construction']:
            lines.append(f'- **构造方法**：')
            for cl in str(m['construction']).splitlines():
                cl = cl.strip()
                if cl:
                    lines.append(f'  {cl}')
        if png_src.exists():
            lines.append(f'\n![{fid}](charts/{t}/{fid}.png)\n')
        else:
            lines.append('\n（图缺失）\n')

    summary = ', '.join(f'{TYPE_CN[t]}{n}' for t, n in counts.items())
    lines.insert(4, f'> 本批共 **{len(picked)}** 个精选因子：{summary}。'
                    f'其中自动阈值 {n_auto_only} 个、人工精选 {n_curated_only} 个、交集 '
                    f'{len(auto_ids & curated)} 个（🏷️ = 仅人工名单）。')

    md_path = OUT_DIR / '精选因子.md'
    md_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    print(f'精选完成: {len(picked)}/{len(results)} 个因子入选 ({summary})')
    print(f'图表 -> {OUT_CHARTS}/ ({len(list(OUT_CHARTS.rglob("*.png")))} 张, 按类型分目录)')
    print(f'报告 -> {md_path}')


if __name__ == '__main__':
    main()
