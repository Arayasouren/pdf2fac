---
name: pdf2fac
description: |
  PDF投资书籍→Alpha因子提炼→自动回测的全流程技能。当用户提供投资类PDF书籍并要求提炼Alpha因子、进行因子回测时触发。
  覆盖：(1) PDF读取与逐章AI总结 (2) 从总结中提炼可量化Alpha因子 (3) 按因子类型自动回测
  (4) 回测结果存入SQLite因子数据库 (5) 精选导出与防过拟合精修 (6) 组合构建。
  触发词：读书提炼alpha、因子回测、pdf2fac。
---

# pdf2fac — 投资书籍 → Alpha 因子 → 回测 → 组合

## 全流程概览

```
PDF书籍 → 文本化 → AI提炼因子卡 → builder代码 → 回测出图 → 精选 → 精修 → 组合
   ↓         ↓           ↓              ↓           ↓         ↓       ↓       ↓
factor_gen/ ocr/      alpha/       builders/    chart_perf  filtered/ refine/ portfolio/
 <书名>/    full.txt   因子列表.md   <书码>.py      .json+PNG   精选.md   reports/ 组合报告
```

数据接口：`scripts/data_source.py`（由使用者实现的 7 个接口函数，schema 见其 docstring），
`scripts/data_loader.py` 负责按月 pickle 缓存。引擎只经 data_loader 取数，不直接依赖任何数据源。

## 一、PDF 读取与文本化

```bash
python3 scripts/ocr_pdf.py <pdf_path> --out factor_gen/<书名>/ocr            # Vision OCR, 按页缓存断点续跑
python3 scripts/ocr_pdf.py <pdf_path> --out factor_gen/<书名>/ocr --text-only # 有文字层直接抽取(秒级)
```

- macOS Vision 框架本地 OCR（中英文，约 1~3 秒/页）；输出 `ocr/pages/page_XXX.txt` + `ocr/full.txt`
- 假文字层陷阱：抽取后按页均字符量抽样校验，异常者降级为 OCR
- AI 读 full.txt 写逐章总结 → `factor_gen/<书名>/summary/AI总结.md`

## 二、Alpha 因子提炼

提炼提示词模板见 `references/extraction-prompt.md`（七结构块 + 按书变体 + 深度推断模式）。
每本书一个提炼子代理，产出六字段硬格式因子卡：

```markdown
### F01. 因子名称
- **类型**: timing | cross_section | rotation | screening
- **定义**: 一句话
- **构建**: 具体公式/逻辑（含明确阈值）
- **频率**: daily | weekly | monthly
- **数据需求**: 所需字段列表
- **原文依据**: 引用原文（页码或段落）
```

类型说明：timing=大盘/情绪择时信号；cross_section=个股打分排序；rotation=行业轮动；
screening=条件筛选股票池。推断型因子须另附 来源类型/推断逻辑，且证据链 ≥2 处独立引用
（聚合器对不足者硬拒入库）。

提炼纪律：只提炼可量化规则；纯心态哲学不提炼；无数据支撑不硬凑；允许零因子报告。

## 三、回测执行

**架构：引擎 + builder 分离**——`scripts/factor_engine.py` 统一数据上下文（ctx）与四类内核；
`scripts/builders/<书码>.py` 每书一个文件、每因子一个 `@register('FID')` 函数，
编写规范必读 `references/factor-builder-api.md`，模板见 `scripts/builders/example.py`。

```bash
python3 scripts/factor_engine.py --list                # 列出已注册 builder
python3 scripts/factor_engine.py --only DEMO-F01 --save   # 单因子回测+出图+入库
python3 scripts/factor_engine.py --save                # 全量 (逐因子落盘, 幂等续跑)
```

**四类内核与统一口径：**

| 类型 | 构造 | 核心指标 | 图 |
|---|---|---|---|
| timing 择时 | 信号滞后一日映射为基准仓位 | 年化/超额/夏普/回撤/多头占比 | 净值对比+回撤 |
| cross_section 截面 | 全市场排序分十组 | Rank IC/ICIR/IC>0占比/分组年化 | IC序列+累计IC+十分组 |
| rotation 轮动 | 多 top_n/空 bottom_n 行业 | 多头年化/超额/胜率 | 多头净值+超额净值 |
| screening 筛选 | 信号次日开盘进场持有 N 日 | 组合年化/超额/夏普/笔数/覆盖率 | 净值+超额+回撤 |

**可成交口径（四类共享，统一执行、builder 无权绕过）：**

- T+1 开盘价进场；剔除 T+1 一字涨停（全天封死买不进）与停牌股；持仓遇一字跌停递延卖出、逐日重试
- 剔除 ST 特殊处理股（信号日判定）、上市不足 120 交易日的新股
- 费用双边千 3（单边 0.15%）：择时按 |Δ仓位| 计、轮动按行业替换比例计、筛选按进出场笔数计、截面按分组换手计
- 防前视：builder 只用 T 日及之前数据；TTM 财务按公告日 ann_date 时点正确对齐
- warmup：回测起点前推 500 自然日缓冲；基准缺史段用同类指数承接（如中证2000 起于 2013 年末，此前以中证1000 承接），不得填零

无法日频落地的因子（需龙虎榜/封单/分钟级数据）：builder 内 `raise NotImplementedError('原因')`，
引擎记入失败清单不出图。

## 四、精选导出

```bash
python3 scripts/filter_factors.py    # 阈值筛选 ∪ curated_factors.txt 人工并集 → results/filtered/
```

类型化阈值（脚本顶部 CRITERIA 可调）：择时 超额≥3%且夏普≥0.3 ｜ 截面 |IC|≥0.03且|ICIR|≥0.5且期数≥30 ｜
轮动 超额≥3%且胜率≥48% ｜ 筛选 超额≥5%且笔数≥30且覆盖率≥5%。
人工并集名单（项目根 `curated_factors.txt`，每行一个 factor_id）与自动阈值取并集，标注 🏷️。
迷你池（中位在池 <30 只/日）截面因子改判筛选口径重测；池子规模标注进精选报告。
产出：`results/filtered/charts/<类型>/*.png` + `精选因子.md`（嵌入图、定义、构造公式）。

## 五、因子精修（人工确认门）

```bash
python3 scripts/refine_factor.py --list                    # 看待精修计划
python3 scripts/refine_factor.py --run <FID>               # 打印计划(不执行)
python3 scripts/refine_factor.py --run <FID> --confirm     # 人工确认后执行
python3 scripts/refine_factor.py --all --confirm           # 批量 (共享 ctx, 幂等续跑)
python3 scripts/summarize_refine.py                        # 汇总采纳/否决表
```

- **计划生成（LLM 计划代理）**：读 builder 源码 → 参数化变体 `builders/refined/<FID>.py`
  （`build(ctx, params)`，params=None 时与原逻辑一致）+ 候选清单 `refine/plans/<FID>.json`
  （2~5 个关键参数，各 4~5 候选，按物理意义放宽）
- **执行**：OAT 单参数扰动（每次只动一个参数，不做组合寻优）；训练期调参、验证期对照，
  中间留一个自然年作隔离缓冲防泄漏
- **防过拟合三件套**：①验证期退化 >10% 自动否决 ②孤立峰检测（邻域绩效突刺的参数标记脆弱）
  ③OAT 不做多参数组合
- 稀疏容错：某候选集训练窗口无有效样本记 None 跳过，不拖垮整个因子
- 采纳回写：`建议采纳` 的参数回写生产 builder（行末注释标注），重跑回测+出图+入库，
  最后 `filter_factors.py` 刷新精选集

## 六、组合构建

`scripts/portfolio_cs.py`：精选截面因子 → N 只等权组合。

- 因子符号/权重仅用训练期数据估计（IC 定号、|ICIR| 加权），无前视；|IC|<0.003 判噪声剔除
- 合成：调仓信号日截面 rank 标准化（NaN→中性 0）→ 符号对齐 → 加权求和 → 取前 N
- 交易：T+1 开盘、双边千 3、排名缓冲区（跌出前 buffer 名才卖）、最短持有期、一字跌停递延
- 频率与持仓宽度是最大旋钮：事件/情绪类 alpha 衰减慢，季度调仓+缓冲显著优于高频
- 组合评估需同时报：分窗口（训练/验证）超额、IR、跟踪误差、超额回撤、月胜率、月均换手、费率敏感性

## 七、因子数据库

SQLite 单文件 `db/factor.db`（免服务，`PDF2FAC_DB` 环境变量可覆盖路径）。
表结构见 `references/db-schema.md`：factor_meta + 四张分类绩效表 + factor_values。
绩效表幂等（一因子一行，重跑先删旧值），截面因子值 pivot 自动落盘 `results/factor_values/<FID>.pkl`。

## 八、标准工作流

1. 数据接口实现（data_source.py 七函数）+ 预热缓存
2. PDF 入库 `factor_gen/<书名>/pdf/` 并文本化
3. 提炼子代理产因子卡 + AI 总结
4. `aggregate_factors.py` 入库（推断型证据链 <2 硬拒）
5. builder 子代理生成 `builders/<书码>.py` + 自测（注册数/覆盖率/常识断言/冒烟回测）
6. `factor_engine.py --save` 回测出图（逐因子落盘，断点续跑）
7. `filter_factors.py` 精选导出
8. （可选，人工确认）`refine_factor.py` 精修 → 采纳回写 → 回到 6 重跑受影响因子
9. `portfolio_cs.py` 组合构建与评估
10. 底稿留痕：净值/因子值/网格结果全部落盘 results/ 下，改图改表读缓存，只有改参数才重算
