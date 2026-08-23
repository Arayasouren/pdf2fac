#!/usr/bin/env python3
"""
汇总 factor_gen/*/alpha/因子列表.md → SQLite factor_meta + 总清单

- 解析每本书的因子条目 (### Fnn. 名称 + 子弹字段)
- 全局唯一 factor_id = <书码>-F<nn>（书码见 BOOK_CODES，新增书在此登记）
- upsert 进 factor_meta（幂等，可反复跑）
- 输出总清单 results/factor_inventory.md

用法: /usr/bin/python3 scripts/aggregate_factors.py
"""

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / 'scripts'))
from save_to_db import save_factor_meta  # noqa: E402

# 书码登记（factor_id 前缀; 新书完成提炼后在此追加）
BOOK_CODES = {
    # 在此登记: '书名(factor_gen/<书名>/ 目录名)': '因子ID前缀'
    '示例书籍': 'DEMO',
}

VALID_TYPES = {'timing', 'cross_section', 'rotation', 'screening'}
VALID_FREQ = {'daily', 'weekly', 'monthly'}

FIELD_MAP = {
    '类型': 'factor_type',
    '定义': 'definition',
    '构建': 'construction',
    '频率': 'frequency',
    '数据需求': 'data_requirement',
    '原文依据': 'original_text',
    '来源类型': '_source_type',   # 明示|推断 (深度推断模式)
    '推断逻辑': '_infer_logic',   # 推断型因子的刻画说明
}


def parse_alpha_file(path):
    """解析 因子列表.md → [{local_id, factor_name, fields...}]"""
    text = path.read_text(encoding='utf-8')
    factors = []
    # 按 ### 标题切分
    blocks = re.split(r'\n(?=###\s)', text)
    for blk in blocks:
        m = re.match(r'###\s+([A-Za-z]?\d+)[.、\s]+(.+)', blk.strip())
        if not m:
            continue
        local_id, name = m.group(1), m.group(2).strip()
        entry = {'local_id': local_id, 'factor_name': name}
        # 子弹字段: - **字段**: 值 (值可能跨多行, 到下一个子弹或块尾)
        cur_field = None
        for line in blk.splitlines()[1:]:
            fm = re.match(r'\s*-\s*\*\*(.+?)\*\*[:：]\s*(.*)', line)
            if fm:
                key, val = fm.group(1).strip(), fm.group(2).strip()
                cur_field = FIELD_MAP.get(key)
                if cur_field:
                    entry[cur_field] = val
            elif cur_field and line.strip() and not line.strip().startswith('#'):
                # 续行拼接
                entry[cur_field] = entry.get(cur_field, '') + '\n' + line.strip()
        factors.append(entry)
    return factors


def normalize(entry):
    """字段值规范化到 factor_meta 约束内"""
    ft = entry.get('factor_type', '').strip().lower()
    if ft not in VALID_TYPES:
        # 容忍中文/别名
        alias = {'择时': 'timing', '截面': 'cross_section', '截面选股': 'cross_section',
                 '轮动': 'rotation', '板块轮动': 'rotation', '筛选': 'screening',
                 '选股': 'cross_section', '个股筛选': 'screening'}
        ft = alias.get(ft, None)
        if ft is None:
            for k, v in alias.items():
                if k in entry.get('factor_type', ''):
                    ft = v
                    break
    if ft not in VALID_TYPES:
        ft = 'cross_section'  # 兜底

    freq = entry.get('frequency', '').strip().lower()
    if freq not in VALID_FREQ:
        freq = 'daily'

    return ft, freq


def main():
    inventory = []
    total_new = 0

    for alpha in sorted(PROJECT_ROOT.glob('factor_gen/*/alpha/因子列表.md')):
        book = alpha.parent.parent.name
        code = BOOK_CODES.get(book)
        if not code:
            print(f'[warn] 未登记书码: {book}, 跳过（在 BOOK_CODES 中追加后重跑）')
            continue

        entries = parse_alpha_file(alpha)
        if not entries:
            print(f'[info] {book}: 无因子条目（可能为零因子报告）')
            continue

        n = 0
        for e in entries:
            fid = f'{code}-{e["local_id"].upper()}'
            ftype, freq = normalize(e)
            if not e.get('definition') or not e.get('construction'):
                print(f'  [warn] {fid} 缺定义或构建, 仍入库但请人工复核')
            # 深度推断模式: 证据链<2处拒收; 来源类型/推断逻辑进 notes
            notes_parts = []
            if e.get('_source_type'):
                st = e['_source_type'].strip()
                if st.startswith('推断'):
                    ev = e.get('original_text') or ''
                    n_ev = max(ev.count('；'), ev.count(';'), 1) if ('；' in ev or ';' in ev) else \
                        len(re.findall(r'(第?\s*[Pp０-９0-9]+\s*页|\d{4}\s*年)', ev))
                    if n_ev < 2:
                        print(f'  [reject] {fid} 推断型因子证据链不足(<2处), 不入库')
                        continue
                notes_parts.append(f'来源类型: {st}')
            if e.get('_infer_logic'):
                notes_parts.append(f'推断逻辑: {e["_infer_logic"].strip()}')
            save_factor_meta({
                'factor_id': fid,
                'factor_name': e['factor_name'][:120],
                'factor_type': ftype,
                'source_book': book,
                'source_author': None,
                'definition': e.get('definition') or e['factor_name'],
                'construction': e.get('construction') or '见原文依据',
                'frequency': freq,
                'data_requirement': e.get('data_requirement'),
                'data_available': 1,
                'original_text': e.get('original_text'),
                'status': 'draft',
                'notes': '\n'.join(notes_parts) if notes_parts else None,
            })
            n += 1
            inventory.append({
                'factor_id': fid, 'factor_name': e['factor_name'],
                'factor_type': ftype, 'frequency': freq, 'source_book': book,
            })
        total_new += n
        print(f'[ok] {book} ({code}): {n} 个因子入库')

    # 输出总清单
    out = PROJECT_ROOT / 'results' / 'factor_inventory.md'
    out.parent.mkdir(exist_ok=True)
    type_cn = {'timing': '择时', 'cross_section': '截面', 'rotation': '轮动', 'screening': '筛选'}
    lines = ['# Alpha 因子总清单', '',
             f'> 共 {len(inventory)} 个因子, {len(set(i["source_book"] for i in inventory))} 本书', '',
             '| factor_id | 名称 | 类型 | 频率 | 来源 |', '|---|---|---|---|---|']
    for i in inventory:
        lines.append(f'| {i["factor_id"]} | {i["factor_name"]} | '
                     f'{type_cn[i["factor_type"]]} | {i["frequency"]} | {i["source_book"]} |')
    out.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'\n总清单 -> {out}')
    print(f'合计 {total_new} 个因子')


if __name__ == '__main__':
    main()
