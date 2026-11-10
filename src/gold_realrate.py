"""First-round descriptive study. Credentials are read only from the environment.

Run from repository root; default mode uses the saved local snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/2026-11-10"
PROCESSED = ROOT / "data/processed"
OUT = ROOT / "outputs"
META = ROOT / "data/metadata"
AS_OF = "2026-11-10T13:45:00+08:00"
END = "20261109"  # Exclude the current GMT trading day, which is not complete.
START = "20030101"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10"
SOURCES = {
    "gold": "https://tushare.pro/document/2?doc_id=179",
    "instrument": "https://tushare.pro/document/2?doc_id=178",
    "real_yield": "https://tushare.pro/document/2?doc_id=220",
    "reference": "https://fred.stlouisfed.org/series/DFII10",
}


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ts_request(session, api, params, fields):
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("Set TUSHARE_TOKEN in this process environment before --fetch")
    response = session.post("https://api.tushare.pro", json={
        "api_name": api, "token": token, "params": params, "fields": fields,
    }, timeout=40)
    response.raise_for_status()
    obj = response.json()
    if obj.get("code") != 0:
        msg = str(obj.get("msg", "unknown error")).replace(token, "[REDACTED]")
        raise RuntimeError(f"{api}: code={obj.get('code')}; {msg}")
    # Never retain a response that contains the credential.
    if token in response.text:
        raise RuntimeError("Credential unexpectedly echoed in response; refusing to save")
    return obj


def fetch():
    RAW.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    requests_log = []
    for api, fields, extra in [
        ("fx_daily", "ts_code,trade_date,bid_close,ask_close", {"ts_code": "XAUUSD.FXCM"}),
        ("us_trycr", "date,y10", {}),
    ]:
        for year in range(2003, 2027):
            params = {**extra, "start_date": f"{year}0101", "end_date": min(f"{year}1231", END)}
            path = RAW / f"{api}_{year}.json"
            if path.exists():
                obj = json.loads(path.read_text(encoding="utf-8"))
            else:
                try:
                    obj = ts_request(session, api, params, fields)
                except Exception as exc:
                    save_json(OUT / "fetch_failure.json", {"api": api, "year": year,
                        "error": str(exc), "fallback": "Use FRED DFII10 for real yield; verify a licensed daily gold source before substituting."})
                    raise
                save_json(path, obj)
                time.sleep(0.4)
            count = len(obj["data"]["items"])
            if count >= (1000 if api == "fx_daily" else 2000):
                raise ValueError("Potential pagination truncation")
            requests_log.append({"api": api, "params": params, "fields": fields, "rows": count,
                "file": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "saved_at_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()})
        print(api, "annual batches saved", flush=True)
    reference = RAW / "fred_DFII10.csv"
    reference_status = "available"
    if not reference.exists():
        try:
            r = session.get(FRED_URL, timeout=40)
            r.raise_for_status()
            check = pd.read_csv(io.StringIO(r.text))
            assert "DFII10" in check.columns
            reference.write_bytes(r.content)
        except Exception as exc:
            reference_status = f"unavailable: {type(exc).__name__}"
    save_json(META / "snapshot_manifest.json", {
        "research_as_of": AS_OF, "observation_date_limit": "2026-11-09",
        "sources": SOURCES, "requests": requests_log, "reference_status": reference_status,
        "reference_sha256": hashlib.sha256(reference.read_bytes()).hexdigest() if reference.exists() else None,
        "raw_redistribution": "Not verified; raw and processed data remain local and are git-ignored.",
        "vintage": "Current retrieved history, not point-in-time vintages; exact release timestamps not supplied by Tushare.",
        "runtime": {"python": platform.python_version(), "pandas": pd.__version__,
                    "numpy": np.__version__, "matplotlib": matplotlib.__version__, "requests": requests.__version__},
    })


def read_daily(api):
    parts = []
    for year in range(2003, 2027):
        obj = json.loads((RAW / f"{api}_{year}.json").read_text(encoding="utf-8"))
        parts.append(pd.DataFrame(obj["data"]["items"], columns=obj["data"]["fields"]))
    frame = pd.concat(parts, ignore_index=True)
    date_col = "trade_date" if api == "fx_daily" else "date"
    frame["date"] = pd.to_datetime(frame[date_col], format="%Y%m%d")
    frame = frame.set_index("date").sort_index()
    if frame.index.duplicated().any():
        raise ValueError(f"Duplicate dates in {api}; do not silently drop")
    if not frame.index.to_series().between(pd.Timestamp("2003-01-01"), pd.Timestamp("2026-11-09")).all():
        raise ValueError("Out-of-range dates")
    if api == "fx_daily":
        for col in ["bid_close", "ask_close"]:
            frame[col] = pd.to_numeric(frame[col], errors="raise")
        bad = frame[["bid_close", "ask_close"]].notna().all(axis=1) & (
            (frame.bid_close <= 0) | (frame.ask_close <= 0) | (frame.ask_close < frame.bid_close))
        if bad.any():
            raise ValueError(f"Invalid gold quotes: {frame.index[bad].tolist()}")
        frame["gold_usd_oz"] = (frame.bid_close + frame.ask_close) / 2
    else:
        frame["real_yield_pct"] = pd.to_numeric(frame["y10"], errors="raise")
    return frame


def make_weekly(gold, rates, cutoff="2026-11-10"):
    """Pick the last common weekday in each Sat-Fri bin, never fill across weeks."""
    daily = pd.concat([gold.rename("gold_usd_oz"), rates.rename("real_yield_pct")], axis=1)
    daily = daily.loc[daily.index.dayofweek < 5]
    daily["observation_date"] = daily.index
    daily = daily.dropna(subset=["gold_usd_oz", "real_yield_pct"])
    weekly = daily.resample("W-FRI", label="right", closed="right").last()
    weekly.index.name = "week_end"
    weekly["partial_week"] = weekly.index >= pd.Timestamp(cutoff)
    complete = weekly.loc[~weekly.partial_week].copy()
    # Keep the full weekly grid so gaps invalidate returns and 52-week windows.
    complete["gold_return"] = complete.gold_usd_oz.pct_change(fill_method=None)
    complete["real_yield_change_pp"] = complete.real_yield_pct.diff()
    complete["rolling_pairs_52w"] = (complete.gold_return.notna() & complete.real_yield_change_pp.notna()).rolling(52, min_periods=1).sum()
    complete["corr_52w"] = complete.gold_return.rolling(52, min_periods=52).corr(complete.real_yield_change_pp)
    return daily, weekly, complete


def configure_plots():
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"]:
        if name in available:
            plt.rcParams["font.family"] = name
            break
    plt.rcParams.update({"axes.unicode_minus": False, "font.size": 11,
        "axes.spines.top": False, "figure.facecolor": "#f7f9fc", "axes.facecolor": "white"})


def comparison_plot(complete, partial, start, end, filename, title):
    x = complete.loc[start:end]
    fig, ax = plt.subplots(figsize=(13.5, 6.7))
    fig.subplots_adjust(left=.08, right=.91, bottom=.19, top=.80)
    right = ax.twinx()
    ax.plot(x.index, x.gold_usd_oz, color="#b17c13", linewidth=1.65, label="黄金收盘报价中点（左轴）")
    right.plot(x.index, x.real_yield_pct, color="#215e9b", linewidth=1.2, alpha=.88, label="10年期实际收益率（右轴）")
    tail = partial.loc[start:end]
    if len(tail):
        actual_dates = pd.DatetimeIndex(tail.observation_date)
        ax.scatter(actual_dates, tail.gold_usd_oz, s=45, facecolors="none", edgecolors="#b17c13", zorder=5)
        right.scatter(actual_dates, tail.real_yield_pct, s=45, facecolors="none", edgecolors="#215e9b", zorder=5)
    ax.set_ylabel("黄金（美元/金衡盎司）", color="#986b11")
    right.set_ylabel("10年期实际收益率（%，正常方向）", color="#215e9b")
    ax.grid(axis="y", alpha=.18)
    right.axhline(0, color="#215e9b", linestyle=":", linewidth=.8, alpha=.4)
    ax.xaxis.set_major_locator(mdates.YearLocator(2 if len(x) > 600 else 1))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    lines = ax.lines[:1] + right.lines[:1]
    ax.legend(lines, [line.get_label() for line in lines], loc="lower left",
              bbox_to_anchor=(0, 1.005), ncol=2, fontsize=10, frameon=False)
    fig.suptitle(title, x=.08, ha="left", y=.965, fontsize=19, fontweight="bold", color="#20334c")
    fig.text(.08, .885, "初步版：黄金日期标签待核验；按共同工作日标签取周末值，两轴均未反转", color="#53657b", fontsize=11)
    fig.text(.08, .075, "来源：Tushare / FXCM XAUUSD.FXCM、us_trycr(y10)  |  黄金为经纪商报价，非LBMA定盘价", fontsize=10, color="#53657b")
    tail_note = "；空心点：11月9日未完周标签" if len(tail) else ""
    fig.text(.08, .038, f"研究基准：2026-11-10 13:45（上海）  |  每周最后共同有效工作日标签{tail_note}；不插值、不跨周填充", fontsize=9.5, color="#53657b")
    fig.savefig(OUT / "figures" / filename, dpi=180)
    plt.close(fig)


def plots(complete, partial):
    configure_plots()
    (OUT / "figures").mkdir(parents=True, exist_ok=True)
    comparison_plot(complete, partial, "2003", "2026", "gold_vs_realrate_longterm.png", "黄金与美国10年期实际收益率｜长期事实对照")
    for start, end in [("2008", "2013"), ("2018", "2021"), ("2022", "2026")]:
        comparison_plot(complete, partial, start, end, f"gold_vs_realrate_{start}_{end}.png", f"黄金与美国10年期实际收益率｜{start}—{end}")
    fig, ax = plt.subplots(figsize=(13.5, 5.9))
    fig.subplots_adjust(left=.08, right=.95, bottom=.20, top=.80)
    c = complete.corr_52w
    ax.plot(c.index, c, color="#215e9b", linewidth=1.5)
    ax.fill_between(c.index, c, 0, where=c >= 0, color="#bf8b2a", alpha=.22)
    ax.fill_between(c.index, c, 0, where=c < 0, color="#215e9b", alpha=.13)
    ax.axhline(0, color="#69798a", linewidth=.8)
    ax.set_ylim(-1, 1)
    ax.set_ylabel("52周Pearson相关系数")
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.grid(axis="y", alpha=.2)
    fig.suptitle("黄金周收益率与实际收益率周变化｜52周滚动相关", x=.08, ha="left", y=.95, fontsize=18, fontweight="bold", color="#20334c")
    fig.text(.08, .85, "初步版：黄金日期标签待核验；52对有效周变化，缺口与未完周不填补", fontsize=11, color="#53657b")
    fig.text(.08, .075, "来源：Tushare / FXCM、us_trycr(y10)  |  重叠窗口，仅为描述统计；未做显著性或因果检验", fontsize=10, color="#53657b")
    fig.text(.08, .035, f"完整周：{complete.index.min():%Y-%m-%d}—{complete.index.max():%Y-%m-%d}  |  研究基准：2026-11-10 13:45（上海）", fontsize=10, color="#53657b")
    fig.savefig(OUT / "figures/gold_realrate_rolling_corr.png", dpi=180)
    plt.close(fig)


def analyze():
    for path in [PROCESSED, OUT, META]:
        path.mkdir(parents=True, exist_ok=True)
    gold, rate = read_daily("fx_daily"), read_daily("us_trycr")
    daily, weekly, complete = make_weekly(gold.gold_usd_oz, rate.real_yield_pct)
    daily.to_csv(PROCESSED / "gold_realrate_common_daily.csv", date_format="%Y-%m-%d")
    weekly.to_csv(PROCESSED / "gold_realrate_weekly_all.csv", date_format="%Y-%m-%d")
    complete.to_csv(PROCESSED / "gold_realrate_weekly.csv", date_format="%Y-%m-%d")
    quality = {}
    for label, df, col in [("gold", gold, "gold_usd_oz"), ("real_yield", rate, "real_yield_pct")]:
        valid = df[col].dropna()
        expected = pd.bdate_range(valid.index.min(), valid.index.max())
        quality[label] = {"rows": len(df), "valid": len(valid), "missing_values": int(df[col].isna().sum()),
            "first": str(valid.index.min().date()), "last": str(valid.index.max().date()),
            "weekend_rows": int((df.index.dayofweek >= 5).sum()),
            "absent_weekdays_including_holidays": len(expected.difference(valid.index))}
    quality["gold_equal_bid_ask_rows"] = int(gold.bid_close.eq(gold.ask_close).sum())
    quality["gold_largest_abs_daily_return"] = float(gold.gold_usd_oz.pct_change(fill_method=None).abs().max())
    quality["common_weekday_rows"] = len(daily)
    quality["complete_week_bins"] = len(complete)
    quality["empty_complete_week_bins"] = int(complete.gold_usd_oz.isna().sum())
    quality["weekly_endpoint_not_friday"] = int((complete.observation_date.dropna().dt.dayofweek != 4).sum())
    quality["partial_week_bins"] = int(weekly.partial_week.sum())
    quality["empty_week_labels"] = [str(d.date()) for d in complete.index[complete.gold_usd_oz.isna()]]
    quality["gold_calendar_counts_by_year"] = {
        str(year): {str(day): int(count) for day, count in group.groupby(group.index.dayofweek).size().items()}
        for year, group in gold.groupby(gold.index.year)
    }
    quality["gold_date_alignment_status"] = "Provisional: source weekday convention varies across years; no date shifting applied."
    quality["valid_rolling_windows"] = int(complete.corr_52w.notna().sum())
    quality["checks"] = {"duplicate_dates": "none", "nonpositive_or_crossed_gold_quotes": "none",
        "fill_or_interpolation": "none", "causal_or_significance_tests": "not performed"}
    # Cross-check all overlapping daily rates against a separate public distribution.
    ref = RAW / "fred_DFII10.csv"
    if ref.exists():
        f = pd.read_csv(ref, index_col=0, parse_dates=True)
        f["DFII10"] = pd.to_numeric(f.DFII10, errors="coerce")
        pairs = rate[["real_yield_pct"]].join(f).dropna()
        delta = pairs.real_yield_pct - pairs.DFII10
        mismatch = pairs.loc[delta.abs() > .000001].copy()
        mismatch.to_csv(PROCESSED / "rate_reference_mismatches.csv", date_format="%Y-%m-%d")
        quality["fred_crosscheck"] = {"pairs": len(pairs), "mismatches": len(mismatch),
            "max_abs_difference_pp": float(delta.abs().max()), "last_reference_date": str(pairs.index.max().date())}
    else:
        quality["fred_crosscheck"] = {"status": "unavailable"}
    valid_c = complete.corr_52w.dropna()
    quality["rolling_summary"] = {"min": float(valid_c.min()), "min_date": str(valid_c.idxmin().date()),
        "max": float(valid_c.max()), "max_date": str(valid_c.idxmax().date()),
        "latest": float(valid_c.iloc[-1]), "positive_windows": int((valid_c > 0).sum()),
        "negative_windows": int((valid_c < 0).sum())}
    periods = []
    for start, end in [("2003", "2007"), ("2008", "2011"), ("2012", "2015"),
                       ("2016", "2018"), ("2019", "2020"), ("2021", "2021"),
                       ("2022", "2022"), ("2023", "2024"), ("2025", "2026")]:
        # Fixed calendar observation windows, not retrospectively optimized regimes.
        x = complete.loc[complete.observation_date.between(pd.Timestamp(f"{start}-01-01"), pd.Timestamp(f"{end}-12-31"))].dropna(subset=["gold_usd_oz", "real_yield_pct"])
        a, b = x.iloc[0], x.iloc[-1]
        periods.append({"window": f"{start}—{end}", "first": str(a.observation_date.date()),
            "last": str(b.observation_date.date()), "gold_return_pct": (b.gold_usd_oz / a.gold_usd_oz - 1) * 100,
            "rate_change_pp": b.real_yield_pct - a.real_yield_pct,
            "gold_start": a.gold_usd_oz, "gold_end": b.gold_usd_oz,
            "rate_start": a.real_yield_pct, "rate_end": b.real_yield_pct})
    quality["fixed_periods"] = periods
    save_json(OUT / "quality_checks.json", quality)
    partial = weekly.loc[weekly.partial_week]
    plots(complete, partial)
    write_findings(gold, rate, complete, partial, quality, periods)
    print(json.dumps(quality, ensure_ascii=False, indent=2))


def write_findings(gold, rate, complete, partial, q, periods):
    c = q["rolling_summary"]
    ref = q["fred_crosscheck"]
    table = "\n".join(f"| {p['window']} | {p['first']}—{p['last']} | {p['gold_start']:,.2f} → {p['gold_end']:,.2f} | {p['gold_return_pct']:+.2f}% | {p['rate_start']:.2f} → {p['rate_end']:.2f} | {p['rate_change_pp']:+.2f} |" for p in periods)
    last = partial.iloc[-1] if len(partial) else None
    text = f"""# 第一轮事实整理：黄金与美国10年期实际收益率

研究基准：{AS_OF}（北京时间）；仅描述数据及统计现象，不做宏观归因或预测。

**初步版，黄金日期标签待核验。** Tushare黄金日历标签在不同时期分布明显不同，不能确认其与美国利率为同一交易日。本报告的“日期”“周收益率”均按接口原始标签计算，数值可复现，但同步相关性尚不宜视为定稿。未平移日期，也未用其他源拼接。

## 数据来源与定义

- 黄金：[Tushare fx_daily]({SOURCES['gold']})，代码 `XAUUSD.FXCM`；美元/金衡盎司，日收盘买卖报价中点 `(bid_close + ask_close) / 2`。经纪商报价用于国际黄金价格代理，非LBMA定盘价或交易所结算价，也不是可成交的无成本价格。日期按接口GMT标签，不进行未经证实的日期平移。
- 实际收益率：[Tushare us_trycr]({SOURCES['real_yield']}) 的 `y10`，美国10年期国债实际收益率曲线，单位%。文档门槛为120积分；外汇日线为2000积分。本次两接口认证成功，无额外开通。
- 校验：[FRED DFII10]({SOURCES['reference']})，美联储H.15的10年期通胀保值国债恒定期限收益率，日度、未季调。本次用作独立分发渠道交叉核验，不与主序列拼接；两者并非完全独立的经济测量。
- 黄金有效日观测：{q['gold']['first']}—{q['gold']['last']}，{q['gold']['valid']}条；利率：{q['real_yield']['first']}—{q['real_yield']['last']}，{q['real_yield']['valid']}条。按年请求，均低于接口单次条数上限。
- 每批原始响应保存在本地 `data/raw/2026-11-10/`，参数、获取时刻、SHA-256及运行环境见 [snapshot_manifest.json](../data/metadata/snapshot_manifest.json)。原始及处理后数据未核实再分发许可，均不提交Git；代码、方法、图表与汇总校验提交仓库。

## 周度转换与计算

1. 保留两序列共同有值的周一至周五日期。同日标签并不保证同一时刻报价；美国利率与GMT黄金收盘仍有日内时点差异。
2. 使用 `W-FRI`：周六至周五、右闭区间，以周五标记；每周选最后一个共同有效日期，同时记录所选原始日期标签。假期可退至本周较早日期，不跨周填充。周日黄金报价不纳入。
3. 自2003-01-01开始，首周不足一周，仅作为起始价；从次周开始计算收益率。完整周共{len(complete)}个，最后完整周为{complete.index.max():%Y-%m-%d}，空周{q['empty_complete_week_bins']}个，非周五观测端点{q['weekly_endpoint_not_friday']}个。保留完整周频网格，不插值、不前向填充。
4. 黄金简单周收益率 `P_t / P_(t-1) - 1`；利率周变化 `y_t - y_(t-1)`，单位百分点（1个百分点=100基点）。只对相邻周做变化，不把跨缺口累计变化误算为单周变化。
5. Pearson滚动相关使用连续52周变化，必须52对均有效；计算的是收益率与利率变化，不是两条水平线的相关。首52对变化形成前留空；未做显著性检验，重叠窗口不能当作独立样本。
6. 2026-11-13标记周尚未结束，最新共同观测为{last.observation_date:%Y-%m-%d}：黄金{last.gold_usd_oz:,.2f}、利率{last.real_yield_pct:.2f}%。仅以空心点显示在长期及2022年以后图中，不计算该周收益率或滚动相关；当前GMT日2026-11-10也不抓取。

## 基础观察

以下以真实保留的观测标签筛选固定日历窗口，比较其中首末完整周值；不是客观算法识别的牛熊阶段，也不是严格日历年收益，未计入窗口起点之前的收益。

| 固定窗口 | 首末观测标签 | 黄金（美元/盎司） | 黄金首末变化 | 利率（%） | 利率变化（百分点） |
| --- | --- | --- | --- | --- | --- |
{table}

- 52周滚动相关共{q['valid_rolling_windows']}个有效窗口，其中负值{c['negative_windows']}个、正值{c['positive_windows']}个；关系方向及强度并不恒定。
- 相关系数最低为{c['min']:.3f}（周标记{c['min_date']}），最高为{c['max']:.3f}（{c['max_date']}）；最新完整周为{c['latest']:.3f}。
- 图中可同时看到黄金上涨、利率下降的窗口，以及两者同向变化的窗口。上述数据只陈述首末变化与滚动相关，不判定哪一个变量驱动另一变量，也不解释这些现象的经济原因。

## 数据检查及限制

- 重复日期：未发现；非正黄金价格或买价高于卖价：未发现。字段数值转换严格执行，异常时停止，不静默替换。
- 黄金缺失值{q['gold']['missing_values']}条、利率缺失值{q['real_yield']['missing_values']}条；黄金周末记录{q['gold']['weekend_rows']}条，利率周末记录{q['real_yield']['weekend_rows']}条。黄金缺席工作日{q['gold']['absent_weekdays_including_holidays']}个、利率{q['real_yield']['absent_weekdays_including_holidays']}个，此计数包含假期，不等于数据错误。
- 黄金买卖价相等记录{q['gold_equal_bid_ask_rows']}条，原样保留，不反推真实点差。最大相邻有效日绝对简单收益率为{q['gold_largest_abs_daily_return']:.2%}；该变化仅用于异常筛查，尚非独立报价验证。
- 两个空周标签为{', '.join(q['empty_week_labels'])}，不作任何补齐。黄金日期标签在2003—2008年为周日至周四（完全没有周五），2021年为周一至周五，2026年还出现周六。逐年星期分布见quality_checks.json，星期编号0=周一、6=周日。该现象不能仅用正常节假日解释，具体是供应商交易日约定、时区转换还是数据问题尚未确认；不能假定统一平移一天即可修复。
- FRED利率交叉校验：{json.dumps(ref, ensure_ascii=False)}。差异明细保存在本地 `data/processed/rate_reference_mismatches.csv`；如有差异，不自动替换Tushare值。
- 黄金尚未获得覆盖全样本的第二独立日度源校验。报价来源、历史字段生成及闭市时刻无法由接口文档完全确认，需保留该限制。需核实日期标签或取得可比的独立源后，才能把同步相关性作为稳健事实。
- Tushare返回的是当前获取的历史版本，不是当时可得版本，也不附逐条发布时间。观测日期上限与获取时刻已记录，但不能证明每条历史数据的首次公开时刻；本结果仅作回顾性描述，不用于无未来信息的回测。
- 备选源核查：[FRED公告](https://fred.stlouisfed.org/news/2022/01/ice-benchmark-administration-ltd-iba-data-to-be-removed-from-fred)说明LBMA日度金银序列于2022-01-31移除；[世界黄金协会](https://www.gold.org/goldhub/data/gold-prices)说明历史LBMA价格自2025-03-18起受限。本次未使用旧 `GOLDAMGBD228NLBM` 或拼接第三方镜像。
- 主数据下载无失败，但黄金日期质量未通过最终确认。探索性备选检查：Stooq HTTPS连接失败（SSLEOFError，系统curl也握手失败）；WGC历史下载返回403；Dukascopy公开日线文件请求返回429。未关闭TLS校验、未绕过限制，均未采用这些源的数据。可行后续方案是核实Tushare的标签定义，或在正常访问可用后以Dukascopy独立重做，或取得获许可的LBMA日度数据；均尚未执行。
- 两条纵轴均正常向上，尺度不同。双轴线形的交叉与幅度不能直接比较；周度采样也会省略周内波动。未建立模型、未检验结构变化、未开展宏观解释。

## 复现与交付

参见 [复现说明](../docs/02_data_method.md)。输出包括长期图、52周相关图及三个放大图；逐项机器检查见 [quality_checks.json](quality_checks.json)。阶段结果待负责人审验，不自动进入归因或预测。
"""
    (OUT / "findings.md").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true", help="Fetch missing annual files; requires environment token")
    args = parser.parse_args()
    if args.fetch:
        fetch()
    analyze()
