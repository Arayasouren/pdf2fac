#!/usr/bin/env python3
"""将因子元信息和回测结果写入 SQLite 因子库 (默认 db/factor.db)

用法:
    python3 save_to_db.py <results.json>     # 批量导入
    PDF2FAC_DB=/path/to/other.db python3 save_to_db.py results.json

JSON 格式 (backtest_yangj.py 的 all_results.json 即此格式):
{
    "factors":       [{ factor_meta 字段... }],      # 可选, 但绩效有外键依赖, 需先入库
    "timing_perf":   [{ factor_timing_perf 字段... }],
    "cs_perf":       [{ factor_cs_perf 字段... }],
    "rotation_perf": [{ factor_rotation_perf 字段... }],
    "screening_perf":[{ factor_screening_perf 字段... }]
}
"""

import json
import sys

from init_db import connect


def save_factor_meta(factor, db_path=None):
    """保存因子元信息 (存在则更新)。

    factor: dict with keys: factor_id, factor_name, factor_type, source_book,
            definition, construction, frequency, data_requirement, ...
    """
    conn = connect(db_path)
    sql = """
    INSERT INTO factor_meta
        (factor_id, factor_name, factor_type, source_book, source_author,
         definition, construction, frequency, data_requirement, data_available,
         original_text, backtest_start, backtest_end, status, notes)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(factor_id) DO UPDATE SET
        factor_name=excluded.factor_name, definition=excluded.definition,
        construction=excluded.construction, frequency=excluded.frequency,
        data_requirement=excluded.data_requirement, data_available=excluded.data_available,
        backtest_start=excluded.backtest_start, backtest_end=excluded.backtest_end,
        status=excluded.status, notes=excluded.notes,
        updated_at=datetime('now', 'localtime')
    """
    conn.execute(sql, (
        factor.get('factor_id'),
        factor.get('factor_name'),
        factor.get('factor_type'),
        factor.get('source_book'),
        factor.get('source_author'),
        factor.get('definition'),
        factor.get('construction'),
        factor.get('frequency'),
        factor.get('data_requirement'),
        factor.get('data_available', 1),
        factor.get('original_text'),
        factor.get('backtest_start'),
        factor.get('backtest_end'),
        factor.get('status', 'draft'),
        factor.get('notes'),
    ))
    conn.commit()
    conn.close()


def save_timing_perf(perf, db_path=None):
    """保存择时因子绩效"""
    conn = connect(db_path)
    # 幂等: 一因子一行, 重跑先删旧值
    conn.execute('DELETE FROM factor_timing_perf WHERE factor_id=?', (perf['factor_id'],))
    sql = """
    INSERT INTO factor_timing_perf
        (factor_id, index_code, index_name, backtest_start, backtest_end,
         long_annual_ret, long_max_dd, long_sharpe, long_win_rate,
         short_annual_ret, benchmark_annual_ret, long_short_ret, long_short_sharpe,
         signal_count, long_days_pct, avg_holding_days)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    conn.execute(sql, (
        perf['factor_id'], perf['index_code'], perf['index_name'],
        perf['backtest_start'], perf['backtest_end'],
        perf.get('long_annual_ret'), perf.get('long_max_dd'), perf.get('long_sharpe'),
        perf.get('long_win_rate'), perf.get('short_annual_ret'),
        perf.get('benchmark_annual_ret'), perf.get('long_short_ret'),
        perf.get('long_short_sharpe'), perf.get('signal_count'),
        perf.get('long_days_pct'), perf.get('avg_holding_days'),
    ))
    conn.commit()
    conn.close()


def save_cs_perf(perf, db_path=None):
    """保存截面因子绩效"""
    conn = connect(db_path)
    # 幂等: 一因子一行, 重跑先删旧值
    conn.execute('DELETE FROM factor_cs_perf WHERE factor_id=?', (perf['factor_id'],))
    sql = """
    INSERT INTO factor_cs_perf
        (factor_id, universe, frequency, backtest_start, backtest_end,
         ic_mean, ic_std, icir, ic_positive_pct, coverage_mean, coverage_min,
         g1_annual_ret, g2_annual_ret, g3_annual_ret, g4_annual_ret, g5_annual_ret,
         long_short_ret, long_short_sharpe, long_excess_ret, long_max_dd, monotonicity)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    conn.execute(sql, (
        perf['factor_id'], perf['universe'], perf['frequency'],
        perf['backtest_start'], perf['backtest_end'],
        perf.get('ic_mean'), perf.get('ic_std'), perf.get('icir'),
        perf.get('ic_positive_pct'), perf.get('coverage_mean'), perf.get('coverage_min'),
        perf.get('g1_annual_ret'), perf.get('g2_annual_ret'), perf.get('g3_annual_ret'),
        perf.get('g4_annual_ret'), perf.get('g5_annual_ret'),
        perf.get('long_short_ret'), perf.get('long_short_sharpe'),
        perf.get('long_excess_ret'), perf.get('long_max_dd'), perf.get('monotonicity'),
    ))
    conn.commit()
    conn.close()


def save_rotation_perf(perf, db_path=None):
    """保存轮动因子绩效"""
    conn = connect(db_path)
    # 幂等: 一因子一行, 重跑先删旧值
    conn.execute('DELETE FROM factor_rotation_perf WHERE factor_id=?', (perf['factor_id'],))
    sql = """
    INSERT INTO factor_rotation_perf
        (factor_id, industry_cls, frequency, backtest_start, backtest_end,
         ic_mean, icir, top_n, bottom_n,
         long_annual_ret, short_annual_ret, long_short_ret,
         benchmark_ret, excess_ret, max_dd, win_rate)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    conn.execute(sql, (
        perf['factor_id'], perf['industry_cls'], perf['frequency'],
        perf['backtest_start'], perf['backtest_end'],
        perf.get('ic_mean'), perf.get('icir'), perf.get('top_n'), perf.get('bottom_n'),
        perf.get('long_annual_ret'), perf.get('short_annual_ret'), perf.get('long_short_ret'),
        perf.get('benchmark_ret'), perf.get('excess_ret'), perf.get('max_dd'), perf.get('win_rate'),
    ))
    conn.commit()
    conn.close()


def save_screening_perf(perf, db_path=None):
    """保存筛选因子绩效"""
    conn = connect(db_path)
    # 幂等: 一因子一行, 重跑先删旧值
    conn.execute('DELETE FROM factor_screening_perf WHERE factor_id=?', (perf['factor_id'],))
    sql = """
    INSERT INTO factor_screening_perf
        (factor_id, benchmark, frequency, backtest_start, backtest_end,
         annual_ret, annual_vol, sharpe, max_dd, calmar,
         excess_annual_ret, excess_max_dd, excess_sharpe,
         win_rate, win_rate_vs_bench, avg_holding, avg_turnover, total_trades)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    conn.execute(sql, (
        perf['factor_id'], perf['benchmark'], perf['frequency'],
        perf['backtest_start'], perf['backtest_end'],
        perf.get('annual_ret'), perf.get('annual_vol'), perf.get('sharpe'),
        perf.get('max_dd'), perf.get('calmar'),
        perf.get('excess_annual_ret'), perf.get('excess_max_dd'), perf.get('excess_sharpe'),
        perf.get('win_rate'), perf.get('win_rate_vs_bench'),
        perf.get('avg_holding'), perf.get('avg_turnover'), perf.get('total_trades'),
    ))
    conn.commit()
    conn.close()


def batch_save_from_json(json_path, db_path=None):
    """从 JSON 文件批量导入因子和绩效 (先 meta 后 perf, 满足外键依赖)。"""
    with open(json_path) as f:
        data = json.load(f)

    for factor in data.get('factors', []):
        save_factor_meta(factor, db_path)
        print(f"  Saved factor: {factor['factor_id']} {factor['factor_name']}")

    for perf in data.get('timing_perf', []):
        save_timing_perf(perf, db_path)
    for perf in data.get('cs_perf', []):
        save_cs_perf(perf, db_path)
    for perf in data.get('rotation_perf', []):
        save_rotation_perf(perf, db_path)
    for perf in data.get('screening_perf', []):
        save_screening_perf(perf, db_path)

    n = {k: len(data.get(k, [])) for k in ('factors', 'timing_perf', 'cs_perf',
                                           'rotation_perf', 'screening_perf')}
    print(f'Batch save completed: {n}')


if __name__ == '__main__':
    if len(sys.argv) > 1:
        batch_save_from_json(sys.argv[1])
    else:
        print('Usage: python save_to_db.py <results.json>')
