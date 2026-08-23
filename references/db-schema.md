# 因子数据库表结构 (SQLite, 默认 db/factor.db)

> 轻量级单文件存储，免安装免服务。路径可用环境变量 `PDF2FAC_DB` 覆盖。
> 初始化: `python3 scripts/init_db.py`；所有连接自动 `PRAGMA foreign_keys=ON`。

## factor_meta — 因子元信息

```sql
CREATE TABLE IF NOT EXISTS factor_meta (
    factor_id       TEXT PRIMARY KEY,                -- 因子ID, 如 E01/S01/P01/B01
    factor_name     TEXT NOT NULL,                   -- 因子名称
    factor_type     TEXT NOT NULL
                    CHECK (factor_type IN ('timing','cross_section','rotation','screening')),
    source_book     TEXT NOT NULL,                   -- 来源书籍
    source_author   TEXT,                            -- 来源作者/大佬
    definition      TEXT NOT NULL,                   -- 因子定义
    construction    TEXT NOT NULL,                   -- 构建方法/公式
    frequency       TEXT NOT NULL
                    CHECK (frequency IN ('daily','weekly','monthly')),
    data_requirement TEXT,                           -- 数据需求
    data_available  INTEGER DEFAULT 1,               -- 1=可用 0=缺失
    original_text   TEXT,                            -- 原文依据
    backtest_start  TEXT,                            -- 回测起始日 (ISO)
    backtest_end    TEXT,                            -- 回测截止日
    status          TEXT DEFAULT 'draft'
                    CHECK (status IN ('draft','backtested','validated','deprecated')),
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    updated_at      TEXT DEFAULT (datetime('now','localtime')),
    notes           TEXT
);
```

## factor_timing_perf — 择时因子绩效

```sql
CREATE TABLE IF NOT EXISTS factor_timing_perf (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_id       TEXT NOT NULL REFERENCES factor_meta(factor_id),
    index_code      TEXT NOT NULL,                   -- HS300/ZZ500/ZZ1000/GZ2000
    index_name      TEXT NOT NULL,
    backtest_start  TEXT NOT NULL,
    backtest_end    TEXT NOT NULL,
    -- 多头(策略)绩效
    long_annual_ret     REAL,                        -- 策略年化收益率
    long_max_dd         REAL,                        -- 策略最大回撤
    long_sharpe         REAL,                        -- 策略夏普比
    long_win_rate       REAL,                        -- 在场日胜率
    -- 空头/基准
    short_annual_ret    REAL,                        -- 空头年化(择时策略一般为空)
    benchmark_annual_ret REAL,                       -- 基准年化收益率
    -- 多空/超额
    long_short_ret      REAL,                        -- 策略-基准 年化差
    long_short_sharpe   REAL,
    -- 信号统计
    signal_count        INTEGER,                     -- 仓位切换次数
    long_days_pct       REAL,                        -- 持多仓天数占比
    avg_holding_days    REAL,
    created_at      TEXT DEFAULT (datetime('now','localtime'))
);
-- INDEX idx_timing_factor(factor_id), idx_timing_index(index_code)
```

## factor_cs_perf — 截面选股因子绩效

```sql
CREATE TABLE IF NOT EXISTS factor_cs_perf (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_id       TEXT NOT NULL REFERENCES factor_meta(factor_id),
    universe        TEXT NOT NULL,                   -- 全A/HS300/ZZ500/ZZ1000
    frequency       TEXT NOT NULL CHECK (frequency IN ('daily','weekly','monthly')),
    backtest_start  TEXT NOT NULL,
    backtest_end    TEXT NOT NULL,
    -- IC指标
    ic_mean         REAL,                            -- Rank IC 均值
    ic_std          REAL,
    icir            REAL,                            -- ic_mean/ic_std
    ic_positive_pct REAL,                            -- IC>0 占比
    -- 覆盖率
    coverage_mean   REAL,
    coverage_min    REAL,
    -- 分组收益 (5组)
    g1_annual_ret   REAL,                            -- 第1组(因子值最小)年化
    g2_annual_ret   REAL,
    g3_annual_ret   REAL,
    g4_annual_ret   REAL,
    g5_annual_ret   REAL,                            -- 第5组(最大)年化
    long_short_ret  REAL,                            -- G5-G1 年化
    long_short_sharpe REAL,
    -- 多头超额
    long_excess_ret REAL,
    long_max_dd     REAL,
    -- 单调性
    monotonicity    REAL,                            -- 组序与收益的 Spearman 相关
    created_at      TEXT DEFAULT (datetime('now','localtime'))
);
-- INDEX idx_cs_factor(factor_id), idx_cs_universe(universe)
```

## factor_rotation_perf — 板块轮动因子绩效

```sql
CREATE TABLE IF NOT EXISTS factor_rotation_perf (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_id       TEXT NOT NULL REFERENCES factor_meta(factor_id),
    industry_cls    TEXT NOT NULL,                   -- CITIC_L1/SW_L1/SW_L2
    frequency       TEXT NOT NULL CHECK (frequency IN ('daily','weekly','monthly')),
    backtest_start  TEXT NOT NULL,
    backtest_end    TEXT NOT NULL,
    ic_mean         REAL,
    icir            REAL,
    top_n           INTEGER,                         -- 做多行业数
    bottom_n        INTEGER,                         -- 做空行业数
    long_annual_ret REAL,
    short_annual_ret REAL,
    long_short_ret  REAL,
    benchmark_ret   REAL,                            -- 等权基准年化
    excess_ret      REAL,
    max_dd          REAL,
    win_rate        REAL,
    created_at      TEXT DEFAULT (datetime('now','localtime'))
);
-- INDEX idx_rotation_factor(factor_id)
```

## factor_screening_perf — 个股筛选因子绩效

```sql
CREATE TABLE IF NOT EXISTS factor_screening_perf (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_id       TEXT NOT NULL REFERENCES factor_meta(factor_id),
    benchmark       TEXT NOT NULL,                   -- HS300/ZZ500/ZZ1000/等权全A
    frequency       TEXT NOT NULL CHECK (frequency IN ('daily','weekly','monthly')),
    backtest_start  TEXT NOT NULL,
    backtest_end    TEXT NOT NULL,
    -- 绝对绩效
    annual_ret      REAL,
    annual_vol      REAL,
    sharpe          REAL,
    max_dd          REAL,
    calmar          REAL,
    -- 相对绩效
    excess_annual_ret REAL,
    excess_max_dd   REAL,
    excess_sharpe   REAL,                            -- 信息比率
    -- 胜率
    win_rate        REAL,
    win_rate_vs_bench REAL,
    -- 统计
    avg_holding     INTEGER,
    avg_turnover    REAL,
    total_trades    INTEGER,
    created_at      TEXT DEFAULT (datetime('now','localtime'))
);
-- INDEX idx_screening_factor(factor_id)
```

## factor_values — 因子值时间序列 (按需存储)

```sql
CREATE TABLE IF NOT EXISTS factor_values (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_id       TEXT NOT NULL REFERENCES factor_meta(factor_id),
    date            TEXT NOT NULL,
    stock_id        TEXT,                            -- 个股代码(截面), NULL=市场级
    industry        TEXT,                            -- 行业(轮动因子)
    factor_value    REAL NOT NULL,
    created_at      TEXT DEFAULT (datetime('now','localtime'))
);
-- INDEX idx_values_factor_date(factor_id, date), idx_values_date_stock(date, stock_id)
```

