#!/usr/bin/env python3
"""
因子精修执行器 — 参数邻域敏感性扫描 + 分样本防过拟合验证

流程：
  1. refine/plans/<FID>.json        精修计划（由 LLM 计划代理生成：参数化 builder + 候选参数）
  2. --run <FID> 打印计划等人工确认; --confirm 才真正执行（人工确认门）
  3. 执行: 参数邻域（默认±30%内）逐一扰动(OAT) → 训练期(2010-2019)扫描
     → 验证期(2020-2026)对照 → 孤立峰检测 → 精修报告

用法：
  /usr/bin/python3 scripts/refine_factor.py --list
  /usr/bin/python3 scripts/refine_factor.py --run BFT-I05            # 查看计划(不执行)
  /usr/bin/python3 scripts/refine_factor.py --run BFT-I05 --confirm  # 确认执行
"""

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ / 'scripts'))
import factor_engine as fe  # noqa: E402

PLANS_DIR = PROJ / 'refine' / 'plans'
BUILDERS_DIR = PROJ / 'scripts' / 'builders' / 'refined'
REPORT_DIR = PROJ / 'refine' / 'reports'

TRAIN = ('2010-01-01', '2023-12-31')   # 训练期(调参): 2024 年以前
VALID = ('2025-01-01', '2026-07-31')   # 对照期: 2025-2026 (2024 留作隔离缓冲年, 防泄漏)

METRIC_KEY = {'timing': 'excess', 'cross_section': 'ic_mean',
              'rotation': 'excess_ret', 'screening': 'excess_annual_ret'}


def load_refined_builder(fid):
    path = BUILDERS_DIR / f'{fid}.py'
    spec = importlib.util.spec_from_file_location(f'refined_{fid}', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build  # 签名: build(ctx, params: dict|None) -> spec


def eval_spec(spec, ctx, start, end):
    fe.BT_START, fe.BT_END = start, end
    t = spec['type']
    if t == 'timing':
        bench = fe.BENCH_REMAP.get(spec.get('bench', 'ZZ1000'), spec.get('bench', 'ZZ1000'))
        perf, _ = fe.bt_timing(spec.get('signal'), ctx['bench_ret'][bench],
                               spec.get('long_thresh'), spec.get('short_thresh'),
                               spec.get('direction', 'high'), position=spec.get('position'),
                               bench_px=ctx['bench_px'].get(bench),
                               exec_mode=fe.TIMING_EXEC, fee_one_side=fe.TIMING_FEE_ONE_SIDE)
    elif t == 'cross_section':
        perf, _, _ = fe.bt_cross_section(spec['factor'], ctx['ret'], ctx['piv']['open'],
                                         ctx['piv']['is_yizi'], freq=spec.get('freq', 'monthly'),
                                         min_stocks=spec.get('min_stocks', 30))
    elif t == 'rotation':
        perf, _ = fe.bt_rotation(spec['factor'], ctx['citic_ret'], spec.get('top_n', 5),
                                 spec.get('bottom_n', 5), min_common=spec.get('min_common'))
    elif t == 'screening':
        bench = fe.BENCH_REMAP.get(spec.get('bench', 'ZZ1000'), spec.get('bench', 'ZZ1000'))
        perf, _ = fe.bt_screening(spec['signals'], ctx['ret'], ctx['bench_ret'][bench],
                                  ctx['piv']['is_yizi'], ctx['piv']['open'], ctx['piv']['close'],
                                  spec.get('hold_days', 5), spec.get('max_stocks', 50))
    else:
        raise ValueError(f'未知类型 {t}')
    return perf


def fragility(values):
    """孤立峰检测: (最优-中位)/(|中位|+eps) 越大越脆"""
    v = np.array([x for x in values if x is not None and not np.isnan(x)], dtype=float)
    if len(v) < 3:
        return np.nan
    med = np.median(v)
    return (v.max() - med) / (abs(med) + 1e-9)


def run_refine(fid, plan):
    build = load_refined_builder(fid)
    ctx = fe.get_ctx()
    t0 = time.time()

    # 参数候选集: 默认 + 每个参数逐候选扰动 (OAT)
    psets = [{'name': '默认', 'params': {}}]
    for p in plan['params']:
        for c in p['candidates']:
            if c == p['default']:
                continue
            psets.append({'name': f"{p['name']}={c}", 'params': {p['name']: c}})

    print(f'[refine] {fid}: {len(plan["params"])} 个参数, {len(psets)} 个参数集', flush=True)
    rows = []
    ftype = None
    for ps in psets:
        t1 = time.time()
        try:
            spec = build(ctx, ps['params'])
            ftype = ftype or spec['type']
            key = METRIC_KEY[spec['type']]
            perf_tr = eval_spec(spec, ctx, *TRAIN)
            m = perf_tr.get(key)
        except Exception as e:
            # 稀疏候选集在训练窗口可能无有效样本: 记 None 并跳过, 不拖垮整个因子
            rows.append({'参数集': ps['name'], 'params': ps['params'], 'type': ftype,
                         'train_metric': None, 'train_perf': None,
                         'error': f'{type(e).__name__}: {e}'})
            print(f"  {ps['name']:<28} train 失败(跳过): {type(e).__name__}: {e} "
                  f"({time.time()-t1:.0f}s)", flush=True)
            continue
        rows.append({'参数集': ps['name'], 'params': ps['params'], 'type': ftype,
                     'train_metric': m, 'train_perf': perf_tr})
        mstr = f'{m:+.4f}' if m is not None else 'None'
        print(f"  {ps['name']:<28} train {key}={mstr} ({time.time()-t1:.0f}s)", flush=True)

    if ftype is None or all(r['train_metric'] is None for r in rows):
        raise RuntimeError(f'{fid}: 训练期全部参数集均无有效样本, 无法精修')
    key = METRIC_KEY[ftype]

    # 找训练期最优参数集 & 逐参数孤立峰
    best = max(rows, key=lambda r: r['train_metric'] if r['train_metric'] is not None else -np.inf)
    default = rows[0]

    # 验证期: 默认 vs 训练最优 (OAT 下最优只改一个参数, 从行记录直接取参数字典)
    try:
        spec_d = build(ctx, {})
        perf_d_v = eval_spec(spec_d, ctx, *VALID)
    except Exception as e:
        print(f'  验证期默认参数评估失败: {type(e).__name__}: {e}', flush=True)
        perf_d_v = {}
    if best['参数集'] != '默认':
        try:
            spec_b = build(ctx, best['params'])
            perf_b_v = eval_spec(spec_b, ctx, *VALID)
        except Exception as e:
            print(f'  验证期最优参数评估失败: {type(e).__name__}: {e}', flush=True)
            perf_b_v = {}
    else:
        perf_b_v = perf_d_v

    # 孤立峰: 对每个参数, 其候选序列(含默认)的脆弱度
    frag = {}
    for p in plan['params']:
        seq = [default['train_metric']] + [r['train_metric'] for r in rows
                                           if r['参数集'].startswith(p['name'] + '=')]
        frag[p['name']] = fragility(seq)

    report = {
        'factor_id': fid, 'type': ftype, 'metric': key,
        'train_window': TRAIN, 'valid_window': VALID,
        'default': {'params': {p['name']: p['default'] for p in plan['params']},
                    'train': default['train_metric'], 'valid': perf_d_v.get(key)},
        'best_train': {'参数集': best['参数集'], 'train': best['train_metric'],
                       'valid': perf_b_v.get(key)},
        'scan': [{'参数集': r['参数集'], 'train': r['train_metric'],
                  **({'error': r['error']} if r.get('error') else {})} for r in rows],
        'fragility': frag,
        'elapsed_s': round(time.time() - t0),
    }
    # 采纳建议: 验证期不退化(>=默认*0.9) 且 无高脆弱参数
    degra = perf_b_v.get(key) is not None and perf_d_v.get(key) is not None and \
        perf_b_v[key] < perf_d_v[key] * 0.9
    fragile_params = [k for k, v in frag.items() if v is not None and not np.isnan(v) and v > 0.5]
    report['recommendation'] = ('建议采纳' if (best['参数集'] != '默认' and not degra and not fragile_params)
                                else '建议保留默认参数')
    report['flags'] = {'validation_degraded': bool(degra), 'fragile_params': fragile_params}
    return report


def _f(v):
    return f'{v:+.4f}' if isinstance(v, (int, float)) else '—'


def write_report_md(rep):
    lines = [f"# 精修报告：{rep['factor_id']}（{rep['type']}）", '']
    lines.append(f"- 训练期 {rep['train_window'][0]}~{rep['train_window'][1]}，"
                 f"验证期 {rep['valid_window'][0]}~{rep['valid_window'][1]}，指标 `{rep['metric']}`")
    lines.append(f"- 默认参数：train **{_f(rep['default']['train'])}** / valid **{_f(rep['default']['valid'])}**")
    lines.append(f"- 训练最优参数集 `{rep['best_train']['参数集']}`："
                 f"train **{_f(rep['best_train']['train'])}** / valid **{_f(rep['best_train']['valid'])}**")
    lines.append(f"- 验证期退化：{'⚠️ 是' if rep['flags']['validation_degraded'] else '否'}；"
                 f"孤立峰参数：{rep['flags']['fragile_params'] or '无'}")
    lines.append(f"\n**结论：{rep['recommendation']}**\n")
    lines.append('| 参数集 | train ' + rep['metric'] + ' |')
    lines.append('|---|---|')
    for s in rep['scan']:
        err = f"（跳过：{s['error']}）" if s.get('error') else ''
        lines.append(f"| {s['参数集']} | {_f(s['train'])}{err} |")
    path = REPORT_DIR / f"{rep['factor_id']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    (REPORT_DIR / f"{rep['factor_id']}.json").write_text(
        json.dumps(rep, indent=1, ensure_ascii=False, default=str), encoding='utf-8')
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--run', metavar='FID')
    ap.add_argument('--all', action='store_true', help='批量执行全部计划 (共享 ctx, 一次加载)')
    ap.add_argument('--confirm', action='store_true', help='人工确认后真正执行')
    args = ap.parse_args()

    plans = sorted(PLANS_DIR.glob('*.json'))
    if args.list or (not args.run and not args.all):
        print(f'{len(plans)} 个精修计划:')
        for p in plans:
            plan = json.load(open(p))
            print(f"  {plan['factor_id']:14s} 参数: "
                  + ', '.join(f"{q['name']}({len(q['candidates'])}候选)" for q in plan['params']))
        if not args.run and not args.all:
            return

    if args.all:
        # 批量模式: 需 --confirm; 已有报告的跳过(幂等续跑)
        plan_objs = [json.load(open(p)) for p in plans]
        todo = [p for p in plan_objs
                if not (REPORT_DIR / f"{p['factor_id']}.json").exists()]
        if not args.confirm:
            print(f'批量精修计划: 共 {len(plan_objs)} 个, 待执行 {len(todo)} 个 (已有报告的跳过)')
            for p in todo:
                print(f"  {p['factor_id']}: " + ', '.join(f"{q['name']}" for q in p['params']))
            print('\n确认执行: /usr/bin/python3 scripts/refine_factor.py --all --confirm')
            return
        ctx = fe.get_ctx()  # 一次性加载
        ok, fail = 0, 0
        for i, plan in enumerate(todo):
            fid = plan['factor_id']
            t0 = time.time()
            try:
                rep = run_refine(fid, plan)
                path = write_report_md(rep)
                ok += 1
                print(f"[{i+1}/{len(todo)}] {fid} -> {rep['recommendation']} ({time.time()-t0:.0f}s)", flush=True)
            except Exception as e:
                fail += 1
                print(f'[{i+1}/{len(todo)}] {fid} 失败: {type(e).__name__}: {e}', flush=True)
        print(f'\n批量精修完成 {ok}, 失败 {fail}, 报告在 refine/reports/')
        return

    plan_path = PLANS_DIR / f'{args.run}.json'
    if not plan_path.exists():
        raise SystemExit(f'无精修计划: {plan_path}（先让计划代理生成）')
    plan = json.load(open(plan_path))

    if not args.confirm:
        # 人工确认门: 打印计划, 不执行
        print(f'=== 精修计划: {plan["factor_id"]} ===')
        print(f'builder: builders/refined/{plan["factor_id"]}.py')
        for q in plan['params']:
            print(f"  参数 {q['name']}: 默认 {q['default']} → 候选 {q['candidates']}"
                  + (f"  # {q.get('note')}" if q.get('note') else ''))
        if plan.get('notes'):
            print(f'备注: {plan["notes"]}')
        print(f'\n训练期 {TRAIN[0]}~{TRAIN[1]} 调参, 验证期 {VALID[0]}~{VALID[1]} 对照, '
              f'参数邻域内逐一扰动(OAT), 孤立峰与验证退化自动标记。')
        print(f'\n确认执行: /usr/bin/python3 scripts/refine_factor.py --run {args.run} --confirm')
        return

    rep = run_refine(args.run, plan)
    path = write_report_md(rep)
    print(f'\n=== {rep["recommendation"]} ===')
    print(f'报告 -> {path}')


if __name__ == '__main__':
    main()
