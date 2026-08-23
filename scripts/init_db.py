#!/usr/bin/env python3
"""初始化 pdf2fac 因子数据库表结构 (SQLite, 默认 db/factor.db)

轻量级单文件存储, 免安装免服务。表结构与旧 MySQL 版一致。
DB 路径可用环境变量 PDF2FAC_DB 覆盖。
"""

import os
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / 'db' / 'factor.db'


def get_db_path():
    return os.environ.get('PDF2FAC_DB', str(DEFAULT_DB))


def connect(db_path=None):
    """新建连接(启用外键约束)。"""
    path = db_path or get_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


TABLES = {
    'factor_meta': """
    CREATE TABLE IF NOT EXISTS factor_meta (
        factor_id       TEXT PRIMARY KEY,                -- 因子ID, 如 E01/S01/P01/B01
        factor_name     TEXT NOT NULL,                   -- 因子名称
        factor_type     TEXT NOT NULL
                        CHECK (factor_type IN ('timing','cross_section','rotation','screening')),
        source_book     TEXT NOT NULL,                   -- 来源书籍
        source_author   TEXT,                            -- 来源作者
        definition      TEXT NOT NULL,                   -- 因子定义
        construction    TEXT NOT NULL,                   -- 构建方法/公式
        frequency       TEXT NOT NULL
                        CHECK (frequency IN ('daily','weekly','monthly')),
        data_requirement TEXT,                           -- 数据需求
        data_available  INTEGER DEFAULT 1,               -- 1=可用 0=缺失
        original_text   TEXT,                            -- 原文依据
        backtest_start  TEXT,                            -- DATE, ISO 格式
        backtest_end    TEXT,
        status          TEXT DEFAULT 'draft'
                        CHECK (status IN ('draft','backtested','validated','deprecated')),
        created_at      TEXT DEFAULT (datetime('now', 'localtime')),
        updated_at      TEXT DEFAULT (datetime('now', 'localtime')),
        notes           TEXT
    )
    """,
    'factor_timing_perf': """
    CREATE TABLE IF NOT EXISTS factor_timing_perf (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        factor_id       TEXT NOT NULL,
        index_code      TEXT NOT NULL,                   -- HS300/ZZ500/ZZ1000/GZ2000
        index_name      TEXT NOT NULL,
        backtest_start  TEXT NOT NULL,
        backtest_end    TEXT NOT NULL,
        long_annual_ret     REAL,
        long_max_dd         REAL,
        long_sharpe         REAL,
        long_win_rate       REAL,
        short_annual_ret    REAL,
        benchmark_annual_ret REAL,
        long_short_ret      REAL,
        long_short_sharpe   REAL,
        signal_count        INTEGER,
        long_days_pct       REAL,
        avg_holding_days    REAL,
        created_at      TEXT DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (factor_id) REFERENCES factor_meta(factor_id)
    )
    """,
    'factor_cs_perf': """
    CREATE TABLE IF NOT EXISTS factor_cs_perf (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        factor_id       TEXT NOT NULL,
        universe        TEXT NOT NULL,                   -- 全A/HS300/ZZ500/ZZ1000
        frequency       TEXT NOT NULL
                        CHECK (frequency IN ('daily','weekly','monthly')),
        backtest_start  TEXT NOT NULL,
        backtest_end    TEXT NOT NULL,
        ic_mean         REAL,
        ic_std          REAL,
        icir            REAL,
        ic_positive_pct REAL,
        coverage_mean   REAL,
        coverage_min    REAL,
        g1_annual_ret   REAL,
        g2_annual_ret   REAL,
        g3_annual_ret   REAL,
        g4_annual_ret   REAL,
        g5_annual_ret   REAL,
        long_short_ret  REAL,
        long_short_sharpe REAL,
        long_excess_ret REAL,
        long_max_dd     REAL,
        monotonicity    REAL,
        created_at      TEXT DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (factor_id) REFERENCES factor_meta(factor_id)
    )
    """,
    'factor_rotation_perf': """
    CREATE TABLE IF NOT EXISTS factor_rotation_perf (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        factor_id       TEXT NOT NULL,
        industry_cls    TEXT NOT NULL,                   -- CITIC_L1/SW_L1/SW_L2
        frequency       TEXT NOT NULL
                        CHECK (frequency IN ('daily','weekly','monthly')),
        backtest_start  TEXT NOT NULL,
        backtest_end    TEXT NOT NULL,
        ic_mean         REAL,
        icir            REAL,
        top_n           INTEGER,
        bottom_n        INTEGER,
        long_annual_ret REAL,
        short_annual_ret REAL,
        long_short_ret  REAL,
        benchmark_ret   REAL,
        excess_ret      REAL,
        max_dd          REAL,
        win_rate        REAL,
        created_at      TEXT DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (factor_id) REFERENCES factor_meta(factor_id)
    )
    """,
    'factor_screening_perf': """
    CREATE TABLE IF NOT EXISTS factor_screening_perf (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        factor_id       TEXT NOT NULL,
        benchmark       TEXT NOT NULL,                   -- HS300/ZZ500/ZZ1000/等权全A
        frequency       TEXT NOT NULL
                        CHECK (frequency IN ('daily','weekly','monthly')),
        backtest_start  TEXT NOT NULL,
        backtest_end    TEXT NOT NULL,
        annual_ret      REAL,
        annual_vol      REAL,
        sharpe          REAL,
        max_dd          REAL,
        calmar          REAL,
        excess_annual_ret REAL,
        excess_max_dd   REAL,
        excess_sharpe   REAL,
        win_rate        REAL,
        win_rate_vs_bench REAL,
        avg_holding     INTEGER,
        avg_turnover    REAL,
        total_trades    INTEGER,
        created_at      TEXT DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (factor_id) REFERENCES factor_meta(factor_id)
    )
    """,
    'factor_values': """
    CREATE TABLE IF NOT EXISTS factor_values (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        factor_id       TEXT NOT NULL,
        date            TEXT NOT NULL,
        stock_id        TEXT,                            -- 个股代码(截面), NULL=市场级
        industry        TEXT,                            -- 行业(轮动因子)
        factor_value    REAL NOT NULL,
        created_at      TEXT DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (factor_id) REFERENCES factor_meta(factor_id)
    )
    """,
}

INDEXES = [
    'CREATE INDEX IF NOT EXISTS idx_timing_factor ON factor_timing_perf(factor_id)',
    'CREATE INDEX IF NOT EXISTS idx_timing_index ON factor_timing_perf(index_code)',
    'CREATE INDEX IF NOT EXISTS idx_cs_factor ON factor_cs_perf(factor_id)',
    'CREATE INDEX IF NOT EXISTS idx_cs_universe ON factor_cs_perf(universe)',
    'CREATE INDEX IF NOT EXISTS idx_rotation_factor ON factor_rotation_perf(factor_id)',
    'CREATE INDEX IF NOT EXISTS idx_screening_factor ON factor_screening_perf(factor_id)',
    'CREATE INDEX IF NOT EXISTS idx_values_factor_date ON factor_values(factor_id, date)',
    'CREATE INDEX IF NOT EXISTS idx_values_date_stock ON factor_values(date, stock_id)',
]


def main():
    db_path = get_db_path()
    conn = connect(db_path)
    cur = conn.cursor()

    for name, ddl in TABLES.items():
        print(f'Creating table: {name} ...', end=' ')
        cur.execute(ddl)
        print('OK')
    for sql in INDEXES:
        cur.execute(sql)

    conn.commit()
    cur.close()
    conn.close()
    print(f'\nAll tables created: {db_path}')


if __name__ == '__main__':
    main()
