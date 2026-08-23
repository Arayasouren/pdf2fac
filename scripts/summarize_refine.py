#!/usr/bin/env python3
"""汇总 refine/reports/*.json → 全库精修采纳/否决表。

用法: /usr/bin/python3 scripts/summarize_refine.py [--csv]
输出: refine/精修汇总.md (+ 可选 CSV 到 stdout)
"""
import json
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
REPORT_DIR = PROJ / 'refine' / 'reports'


def load_all():
    reps = []
    for p in sorted(REPORT_DIR.glob('*.json')):
        try:
            reps.append(json.load(open(p)))
        except Exception as e:
            print(f'跳过损坏报告 {p.name}: {e}', file=sys.stderr)
    return reps


def row(r):
    fid = r['factor_id']
    typ = r.get('type', '?')
    metric = r.get('metric', '?')
    d, b = r.get('default', {}), r.get('best_train', {})
    rec = r.get('recommendation', '?')
    fragile = r.get('flags', {}).get('fragile_params', [])
    degraded = r.get('flags', {}).get('validation_degraded', False)
    best_ps = b.get('参数集', '默认')
    return {
        'fid': fid, 'type': typ, 'metric': metric,
        'd_train': d.get('train'), 'd_valid': d.get('valid'),
        'best_ps': best_ps, 'b_train': b.get('train'), 'b_valid': b.get('valid'),
        'rec': rec, 'fragile': fragile, 'degraded': degraded,
    }


def fmt(v):
    return f'{v:+.3f}' if isinstance(v, (int, float)) else '—'


def main():
    as_csv = '--csv' in sys.argv
    rows = [row(r) for r in load_all()]
    if not rows:
        print('无报告。')
        return

    if as_csv:
        print('factor_id,type,metric,def_train,def_valid,best_ps,best_train,best_valid,'
              'recommendation,fragile_params,validation_degraded')
        for x in rows:
            print(f"{x['fid']},{x['type']},{x['metric']},{x['d_train']},{x['d_valid']},"
                  f"\"{x['best_ps']}\",{x['b_train']},{x['b_valid']},{x['rec']},"
                  f"\"{';'.join(x['fragile'])}\",{x['degraded']}")
        return

    adopt = [x for x in rows if '采纳' in x['rec']]
    keep = [x for x in rows if '采纳' not in x['rec']]
    n_degraded = sum(1 for x in rows if x['degraded'])
    n_fragile = sum(1 for x in rows if x['fragile'])

    lines = ['# 因子精修汇总', '',
             f'- 报告总数 **{len(rows)}**：建议采纳 **{len(adopt)}** / 保留默认 **{len(keep)}**',
             f'- 验证期退化 {n_degraded} 个；存在孤立峰参数 {n_fragile} 个',
             f'- 训练期 2010~2023，验证期 2025-01~2026-07（2024 缓冲带）', '']
    for title, grp in [('## 建议采纳（训练最优参数通过验证期对照）', adopt),
                       ('## 保留默认参数（调优未通过防过拟合闸）', keep)]:
        lines.append(title)
        lines.append('')
        lines.append('| 因子 | 类型 | 默认 train/valid | 最优参数集 | 最优 train/valid | 备注 |')
        lines.append('|---|---|---|---|---|---|')
        for x in sorted(grp, key=lambda y: y['fid']):
            notes = []
            if x['degraded']:
                notes.append('验证退化')
            if x['fragile']:
                notes.append('孤立峰:' + ','.join(x['fragile']))
            lines.append(f"| {x['fid']} | {x['type']} | {fmt(x['d_train'])} / {fmt(x['d_valid'])} "
                         f"| {x['best_ps']} | {fmt(x['b_train'])} / {fmt(x['b_valid'])} "
                         f"| {'；'.join(notes) or '—'} |")
        lines.append('')
    out = PROJ / 'refine' / '精修汇总.md'
    out.write_text('\n'.join(lines), encoding='utf-8')
    print(f'汇总 -> {out}  (采纳 {len(adopt)} / 保留默认 {len(keep)} / 共 {len(rows)})')


if __name__ == '__main__':
    main()
