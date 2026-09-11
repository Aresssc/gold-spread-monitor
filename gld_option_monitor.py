#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GLD 期权链每日监控脚本
======================
功能:
  - 拉取 GLD 当前最新价 + 完整期权链(call/put, 近 6 个到期日)
  - 找出 Call/Put 侧 OI 集中区(OI 墙)
  - 基于"远离 OI 墙"原则, 推荐 Sell Call / Sell Put 行权价
  - 输出:
      - 标准 Markdown 报告(可推送)
      - JSON 数据(可下游消费)
      - CSV 期权链
      - 1 张 OI 分布图

数据源: Nasdaq public API (https://api.nasdaq.com/api/quote/GLD/option-chain)
  - 限制: 一次最多 200 行, 需要分页(limit=200 + offset)
  - 备选(将来): yfinance(需自备), CBOE cdn-api(被 WAF 拒)

CLI:
  python3 gld_option_monitor.py --report md
  python3 gld_option_monitor.py --json --csv --plot
  python3 gld_option_monitor.py --days-ahead 45 --top 3
"""
import argparse
import csv
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, date, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd

OUT_DIR = os.environ.get("OUT_DIR", "output")
os.makedirs(OUT_DIR, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "Chrome/124.0.0.0 Safari/537.36",
      "Accept": "application/json, text/plain, */*",
      "Accept-Language": "en-US,en;q=0.9",
      "Referer": "https://www.nasdaq.com/"}

# =========================================================
# 中文字体
# =========================================================
def setup_cjk_font():
    for name in ["Hiragino Sans GB", "PingFang SC", "Heiti SC", "Arial Unicode MS",
                 "Microsoft YaHei", "Noto Sans CJK SC", "WenQuanYi Zen Hei"]:
        if name in {f.name for f in font_manager.fontManager.ttflist}:
            matplotlib.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            matplotlib.rcParams["axes.unicode_minus"] = False
            return name
    matplotlib.rcParams["axes.unicode_minus"] = False
    return None


FONT_NAME = setup_cjk_font()


# =========================================================
# 数据获取
# =========================================================
def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _num(v):
    """Nasdaq returns '--' or '' for missing/zero; coerce to None or float."""
    if v in (None, "", "--", "N/A"):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def fetch_option_chain(ticker: str, side: str, expiration: str,
                       limit=150, max_pages=3) -> list:
    """expiration = 'YYYY-MM-DD'.
    Paginates automatically if Nasdaq returns more rows than `limit`.
    Capped at `max_pages` to keep memory bounded.
    """
    base_q = {
        "assetclass": "etf",
        "side": side,
        "fromdate": expiration,
        "todate": expiration,
        "excode": "oprac",
        "money": "all",
        "type": "all",
    }
    all_rows = []
    offset = 0
    for page in range(max_pages):
        q = urllib.parse.urlencode({**base_q, "limit": limit, "offset": offset})
        url = f"https://api.nasdaq.com/api/quote/{ticker}/option-chain?{q}"
        try:
            body = http_get(url)
        except Exception as e:
            print(f"[warn] {ticker} {side} {expiration} p{page}: {e}", file=sys.stderr)
            break
        try:
            data = json.loads(body).get("data") or {}
        except json.JSONDecodeError:
            break
        if not data:
            break
        rows = data.get("table", {}).get("rows", [])
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < limit:
            break
        offset += limit
    return all_rows


def fetch_last_trade_from_chain(ticker: str) -> float:
    """Get latest price from a tiny chain call."""
    rows = fetch_option_chain(ticker, "call",
                              date.today().strftime("%Y-%m-%d"), limit=1)
    # Use the expirygroup page → find any single option row
    # Better: just return from the special 'lastTrade' field embedded in the
    # initial response (we use a separate micro-call to read data.lastTrade).
    q = urllib.parse.urlencode({
        "assetclass": "etf", "limit": 1, "offset": 0, "side": "call",
        "fromdate": date.today().strftime("%Y-%m-%d"),
        "todate":     date.today().strftime("%Y-%m-%d"),
        "excode": "oprac", "money": "all", "type": "all",
    })
    url = f"https://api.nasdaq.com/api/quote/{ticker}/option-chain?{q}"
    body = http_get(url)
    data = json.loads(body).get("data") or {}
    s = data.get("lastTrade", "")
    m = re.search(r"\$([\d,.]+)", s)
    if m:
        return float(m.group(1).replace(",", ""))
    return float("nan")


def list_expirations(ticker: str, days_ahead: int = 60) -> list:
    """Discover available expiry dates.

    Strategy (fast, max ~10s):
      - Probe one date (today) to get the 0DTE
      - Probe dates 7/14/21/30/45/60 days ahead (6 probes) and pick the
        Fridays among them (GLD weekly = every Fri + 0DTE)
      - Also explicitly probe the standard 3rd Fridays of next 4 months
        (these are the monthlies)
    """
    today = date.today()
    candidates = set()
    candidates.add(today)
    for off in [1, 7, 14, 21, 28, 35, 45, 60]:
        candidates.add(today + timedelta(days=off))
    # Standard monthly 3rd Fridays for next 4 months
    for m_off in range(1, 5):
        y = today.year + (today.month + m_off - 1) // 12
        m = (today.month + m_off - 1) % 12 + 1
        d = date(y, m, 1)
        offset = (4 - d.weekday()) % 7
        third_fri = d + timedelta(days=offset + 14)
        candidates.add(third_fri)
    found = []
    for d in sorted(candidates):
        try:
            rows = fetch_option_chain(ticker, "call", d.strftime("%Y-%m-%d"),
                                      limit=1, max_pages=1)
        except Exception:
            continue
        if not rows:
            continue
        grp = rows[0].get("expirygroup", "")
        if not grp:
            continue
        try:
            exp_dt = datetime.strptime(grp, "%B %d, %Y").date()
        except ValueError:
            continue
        if any(e == exp_dt for e in found):
            continue
        found.append(exp_dt)
    return sorted(found)


# =========================================================
# 数据规整
# =========================================================
def build_chain_df(ticker: str, expirations: list) -> pd.DataFrame:
    """Fetch full chain for given expirations, both sides. Returns tidy DataFrame.

    To stay memory-friendly, only fetch up to 5 most informative expirations:
      - the nearest 0DTE (today)
      - the nearest weekly (Fri in next 2 weeks)
      - the nearest monthly (3rd Fri / DTE 20-50)
    """
    if not expirations:
        return pd.DataFrame()
    today = date.today()
    # pick: 0DTE / next weekly / monthly (target for 30-45 DTE recommendation)
    chosen = []
    if expirations and expirations[0] == today:
        chosen.append(expirations[0])
    # Next weekly (DTE 4-14)
    weekly_candidates = [e for e in expirations if 4 < (e - today).days <= 14]
    if weekly_candidates:
        chosen.append(weekly_candidates[0])
    # Monthly 25-50 DTE (the recommendation target)
    monthly_candidates = [e for e in expirations if 25 <= (e - today).days <= 50]
    if monthly_candidates:
        chosen.append(monthly_candidates[0])
    # Next monthly 50-90 DTE (for richer OI picture)
    next_monthly = [e for e in expirations if 50 < (e - today).days <= 90]
    if next_monthly:
        chosen.append(next_monthly[0])

    # dedup and cap at 5
    chosen = sorted(set(chosen))[:5]
    print(f"  pulling chain for {len(chosen)} expiries: "
          f"{[e.strftime('%Y-%m-%d') for e in chosen]}")

    records = []
    for exp in chosen:
        for side in ("call", "put"):
            try:
                rows = fetch_option_chain(ticker, side, exp.strftime("%Y-%m-%d"),
                                          limit=150, max_pages=2)
            except Exception as e:
                print(f"[warn] {ticker} {side} {exp}: {e}", file=sys.stderr)
                continue
            for r in rows:
                strike = _num(r.get("strike"))
                if strike is None:
                    continue
                if side == "call":
                    bid = _num(r.get("c_Bid")); ask = _num(r.get("c_Ask"))
                    last = _num(r.get("c_Last")); chg = _num(r.get("c_Change"))
                    vol = _num(r.get("c_Volume")); oi = _num(r.get("c_Openinterest"))
                else:
                    bid = _num(r.get("p_Bid")); ask = _num(r.get("p_Ask"))
                    last = _num(r.get("p_Last")); chg = _num(r.get("p_Change"))
                    vol = _num(r.get("p_Volume")); oi = _num(r.get("p_Openinterest"))
                records.append({
                    "exp": exp, "side": side, "strike": strike,
                    "bid": bid, "ask": ask, "last": last, "change": chg,
                    "volume": vol, "oi": oi,
                })
            # free memory aggressively
            del rows
    df = pd.DataFrame(records)
    del records
    if df.empty:
        return df
    df["mid"] = df[["bid", "ask"]].mean(axis=1, skipna=True)
    df = df.sort_values(["exp", "side", "strike"]).reset_index(drop=True)
    return df


# =========================================================
# 分析: OI 墙 + 推荐行权价
# =========================================================
def _bs_delta_call(spot: float, strike: float, dte: int, iv: float = 0.22) -> float:
    """Black-Scholes call delta, simplified.

    sigma = 0.22 (黄金 ETF 历史 30D 隐含波动率大约 18-26%, 中位 22%)。
    实际 IV 可后续接入 CBOE 实时数据。
    """
    T = max(dte, 1) / 365.0
    sigma = iv
    d1 = (math.log(spot / strike) + (sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))
    return 0.5 * (1 + math.erf(d1 / math.sqrt(2)))


def _bs_delta_put(spot: float, strike: float, dte: int, iv: float = 0.22) -> float:
    return _bs_delta_call(spot, strike, dte, iv) - 1.0


def analyze_oi_walls(df: pd.DataFrame, spot: float, top: int = 3):
    """For each (expiry, side), find the strike(s) with the largest OI.

    Wall definition: a strike whose OI is significantly higher than the OI
    of strikes within ±$5 (a local spike). This identifies true "walls"
    (concentrated hedging) rather than just the highest-OI strikes (which
    may simply be near-the-money).

    Returns dict[exp][side] -> list of {strike, oi, ...}
    Also returns the OI wall band for spot guidance.
    """
    walls = defaultdict(lambda: {"call": [], "put": []})
    if df.empty:
        return walls, {"call_band": None, "put_band": None,
                       "call_walls": {}, "put_walls": {}}

    def find_local_walls(strike_oi: pd.Series, window: float = 10.0,
                         spike_factor: float = 3.0, top: int = 5):
        """Find strikes where OI >= spike_factor × median of OI in ±$window.

        CRITICAL: neighbour median EXCLUDES the strike itself (otherwise a
        single huge strike's median equals itself and never spikes).
        """
        if strike_oi.empty:
            return (None, None), {}
        s = strike_oi.sort_index()
        spikes = []
        for strike, oi in s.items():
            nbr = s[(s.index >= strike - window) & (s.index <= strike + window)]
            # exclude self from neighbour median
            nbr = nbr[nbr.index != strike]
            if len(nbr) < 3:
                continue
            med = nbr.median()
            if med > 0 and oi >= spike_factor * med:
                spikes.append((float(strike), float(oi)))
        if not spikes:
            # fallback: top OI strikes (still need a sensible band)
            peaks = s.sort_values(ascending=False).head(top)
            band = (float(peaks.index.min()), float(peaks.index.max())) if not peaks.empty else (None, None)
            return band, {float(k): float(v) for k, v in peaks.items()}
        # Sort by OI desc, take top N
        spikes.sort(key=lambda x: -x[1])
        spikes = spikes[:top]
        band = (min(s[0] for s in spikes), max(s[0] for s in spikes))
        return band, {s[0]: s[1] for s in spikes}

    call_by_strike = df[df.side == "call"].groupby("strike")["oi"].sum().sort_index()
    put_by_strike  = df[df.side == "put"].groupby("strike")["oi"].sum().sort_index()

    call_band, call_walls = find_local_walls(call_by_strike, top=max(5, top))
    put_band,  put_walls  = find_local_walls(put_by_strike,  top=max(5, top))

    # Per-expiry walls (for the recommendation target)
    for exp in sorted(df["exp"].unique())[:3]:
        sub = df[df.exp == exp]
        for side in ("call", "put"):
            g = (sub[sub.side == side]
                 .groupby("strike")["oi"].sum()
                 .sort_values(ascending=False).head(top))
            for strike, oi in g.items():
                walls[exp][side].append({
                    "strike": float(strike), "oi": float(oi),
                })

    return walls, {"call_band": call_band, "put_band": put_band,
                   "call_walls": call_walls,
                   "put_walls":  put_walls}


def _score_picks(sub, otm_lo, otm_hi, side: str, spot: float, today,
                 walls_dict: dict, top: int):
    """Inner worker for recommend_strikes. Returns list of pick dicts.

    Hard-locked OTM range: [otm_lo, otm_hi]. Wall filter (±$3 around any
    spike). Minimum premium $0.15. Minimum OI 30. OI sweet range [200, 8000].
    """
    s = sub[sub.side == side].dropna(subset=["bid"]).copy()
    s["dist"]    = (s["strike"] - spot) if side == "call" else (spot - s["strike"])
    s["pct_otm"] = s["dist"] / spot
    s = s[(s["pct_otm"] >= otm_lo) & (s["pct_otm"] <= otm_hi)]
    for spike_str in walls_dict.keys():
        spike = float(spike_str)
        s = s[~((s["strike"] >= spike - 3) & (s["strike"] <= spike + 3))]
    s = s[s["bid"] >= 0.15]
    s = s[s["oi"].fillna(0) >= 30]
    s["oi_filled"] = s["oi"].fillna(0)
    s["oi_penalty"] = (
        s["oi_filled"].clip(lower=200) - 200
    ) * 0.0001 + (
        s["oi_filled"].clip(upper=8000) - 8000
    ).abs() * 0.0002
    s["score"] = s["mid"] - s["oi_penalty"] * 100
    return s.sort_values("score", ascending=False).head(top)


def recommend_strikes(df: pd.DataFrame, spot: float, walls: dict, bands: dict,
                      top: int = 3):
    """Recommend sell-call and sell-put strikes.

    Sweet spot (hard):
      - Sell Call: 5-8% OTM
      - Sell Put : 8-12% OTM
      - DTE window: 25-50 (monthly, 3rd-Friday preference)
      - Exclude strikes within ±$3 of any OI wall spike
      - Minimum premium $0.15; minimum OI 30
      - Sort: prefer moderate OI (200-8000), then higher premium

    Fallback (when sweet spot returns 0 picks — usually due to strike-gap
    in monthly chain at ATM±5-10% region):
      - Sell Call: 4-10% OTM
      - Sell Put : 6-15% OTM
    Picks in fallback range are tagged `"fallback": true` so reports can
    highlight the broader window.
    """
    recs = {"call": [], "put": []}
    if df.empty:
        return recs

    call_walls_dict = bands.get("call_walls", {})
    put_walls_dict  = bands.get("put_walls",  {})

    # Choose target expiration: DTE in [25, 50]
    today = date.today()
    candidates = []
    for exp in sorted(df["exp"].unique()):
        dte = (exp - today).days
        if 25 <= dte <= 50:
            candidates.append((exp, dte))
    if not candidates:
        for exp in sorted(df["exp"].unique())[:1]:
            dte = (exp - today).days
            candidates.append((exp, dte))
    if not candidates:
        return recs
    target_exp, _ = candidates[0]
    sub = df[df.exp == target_exp]

    # ----- Sweet-spot range -----
    sweet_lo_hi = {"call": (0.05, 0.08), "put": (0.08, 0.12)}

    def _emit(picks_df, walls_dict, side, fallback_used):
        out = []
        for _, r in picks_df.iterrows():
            dte = (r["exp"] - today).days
            fn = _bs_delta_call if side == "call" else _bs_delta_put
            oi_v = r["oi"]
            vol_v = r["volume"]
            out.append({
                "expiry": r["exp"].strftime("%Y-%m-%d"),
                "dte": dte,
                "strike": float(r["strike"]),
                "pct_otm": round(float(r["pct_otm"]) * 100, 2),
                "bid": r["bid"], "ask": r["ask"], "mid": round(r["mid"], 3),
                "oi": int(oi_v) if pd.notna(oi_v) else None,
                "volume": int(vol_v) if pd.notna(vol_v) else None,
                "est_delta": round(fn(spot, r["strike"], dte), 3),
                "fallback": fallback_used,
            })
        return out

    # Try sweet-spot first, then fallback
    fallback_lo_hi = {"call": (0.04, 0.10), "put": (0.06, 0.15)}

    for side, walls_d in [("call", call_walls_dict), ("put", put_walls_dict)]:
        sweet = _score_picks(sub, *sweet_lo_hi[side], side, spot, today,
                             walls_d, top)
        if not sweet.empty:
            recs[side] = _emit(sweet, walls_d, side, fallback_used=False)
        else:
            # broaden the range and retry
            fb = _score_picks(sub, *fallback_lo_hi[side], side, spot, today,
                              walls_d, top)
            if not fb.empty:
                recs[side] = _emit(fb, walls_d, side, fallback_used=True)

    return recs


# =========================================================
# 输出: 图表 / CSV / Markdown / JSON
# =========================================================
def plot_oi(df: pd.DataFrame, spot: float, bands: dict, out_png: str,
            ticker: str = "GLD"):
    """OI distribution by strike (calls vs puts)."""
    if df.empty:
        return
    call_by = df[df.side == "call"].groupby("strike")["oi"].sum().sort_index()
    put_by  = df[df.side == "put"].groupby("strike")["oi"].sum().sort_index()

    fig, ax = plt.subplots(figsize=(13, 7))
    width = (call_by.index.max() - call_by.index.min()) * 0.012 if len(call_by) > 1 else 0.5

    ax.bar(call_by.index - width/2, call_by.values / 1000,
           width=width, color="#C62828", alpha=0.85, label="Call OI (千张)")
    ax.bar(put_by.index + width/2,  put_by.values / 1000,
           width=width, color="#2E7D32", alpha=0.85, label="Put OI (千张)")

    ax.axvline(spot, color="#000", lw=1.5, ls="--", alpha=0.7, label=f"现价 {spot:.2f}")
    if bands.get("call_band"):
        cb = bands["call_band"]
        ax.axvspan(cb[0], cb[1], color="#C62828", alpha=0.12, label=f"Call OI 墙 {cb[0]:.0f}–{cb[1]:.0f}")
    if bands.get("put_band"):
        pb = bands["put_band"]
        ax.axvspan(pb[0], pb[1], color="#2E7D32", alpha=0.12, label=f"Put OI 墙 {pb[0]:.0f}–{pb[1]:.0f}")

    ax.set_xlabel("行权价", fontsize=11)
    ax.set_ylabel("未平仓合约 OI (千张)", fontsize=11)
    ax.set_title(f"{ticker} · 期权 OI 分布（全部到期日聚合）· {date.today()}",
                 fontsize=13, pad=12)
    ax.legend(loc="upper right", fontsize=10, framealpha=0.9, ncol=2)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    plt.close(fig)


def write_csv(df: pd.DataFrame, path: str):
    df.to_csv(path, index=False, encoding="utf-8-sig")


def write_json(payload: dict, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)


def write_markdown(payload: dict, path: str):
    p = payload
    md = []
    md.append(f"# 📊 {p['ticker']} 期权链监控报告")
    md.append(f"\n> **生成时间**: {p['generated_at']}  ")
    md.append(f"> **最新价**: ${p['spot']:.2f}  ")
    md.append(f"> **覆盖到期日**: {len(p['expirations'])} 个（{p['expirations'][0]} ~ {p['expirations'][-1]}）")
    md.append(f"> **数据源**: Nasdaq public option-chain API")
    md.append("")
    md.append("## 1. OI 墙（未平仓合约密集区）")
    md.append("")
    md.append("**Call OI 墙**: " +
              (" - ".join(f"${s:.0f}（{int(o):,} 张）"
                          for s, o in list(p['bands']['call_walls'].items())[:5])))
    md.append("")
    md.append("**Put OI 墙**: " +
              (" - ".join(f"${s:.0f}（{int(o):,} 张）"
                          for s, o in list(p['bands']['put_walls'].items())[:5])))
    md.append("")
    md.append(f"**Call 墙区间**: ${p['bands']['call_band'][0]:.0f} ~ ${p['bands']['call_band'][1]:.0f}  ")
    md.append(f"**Put 墙区间**: ${p['bands']['put_band'][0]:.0f} ~ ${p['bands']['put_band'][1]:.0f}  ")
    md.append(f"**结论**: 现价 ${p['spot']:.2f} 距离 Call 墙上限 {((p['bands']['call_band'][1]/p['spot']-1)*100):+.1f}%；"
              f"距离 Put 墙下限 {((p['bands']['put_band'][0]/p['spot']-1)*100):+.1f}%。")
    md.append("")

    md.append("## 2. Sell Call 推荐行权价（30-45 DTE，远离 Call OI 墙）")
    md.append("")
    fallback_used = any(r.get("fallback") for r in p['recs']['call'])
    if p['recs']['call']:
        if fallback_used:
            md.append("> ⚠️ 标准 5-8% OTM 区间当日数据稀薄，下表为放宽至 4-10% 的 fallback 推荐")
            md.append("")
        md.append("| 到期 | DTE | 行权价 | %OTM | 中间价 | Bid | Ask | OI | Vol | 估 Δ |")
        md.append("|---|---|---|---|---|---|---|---|---|---|")
        for r in p['recs']['call']:
            md.append(f"| {r['expiry']} | {r['dte']} | ${r['strike']:.0f} | "
                      f"{r['pct_otm']:+.1f}% | ${r['mid']:.2f} | ${r['bid']:.2f} | "
                      f"${r['ask']:.2f} | {r['oi'] or '-'} | {r['volume'] or '-'} | "
                      f"{r['est_delta']:.3f} |")
    else:
        md.append("_无符合条件行权价_")
    md.append("")

    md.append("## 3. Sell Put 推荐行权价（30-45 DTE，远离 Put OI 墙）")
    md.append("")
    fallback_used_put = any(r.get("fallback") for r in p['recs']['put'])
    if p['recs']['put']:
        if fallback_used_put:
            md.append("> ⚠️ 标准 8-12% OTM 区间当日数据稀薄，下表为放宽至 6-15% 的 fallback 推荐")
            md.append("")
        md.append("| 到期 | DTE | 行权价 | %OTM | 中间价 | Bid | Ask | OI | Vol | 估 Δ |")
        md.append("|---|---|---|---|---|---|---|---|---|---|")
        for r in p['recs']['put']:
            md.append(f"| {r['expiry']} | {r['dte']} | ${r['strike']:.0f} | "
                      f"{r['pct_otm']:+.1f}% | ${r['mid']:.2f} | ${r['bid']:.2f} | "
                      f"${r['ask']:.2f} | {r['oi'] or '-'} | {r['volume'] or '-'} | "
                      f"{r['est_delta']:.3f} |")
    else:
        md.append("_无符合条件行权价_")
    md.append("")

    md.append("## 4. 风险提示")
    md.append("")
    md.append("- 数据为 Nasdaq 延迟报价，可能与券商终端有 15 分钟时延。")
    md.append("- 卖期权理论亏损无限，请严格仓位管理（单腿不超过组合 5%）。")
    md.append("- 重大事件（FOMC / 地缘冲突 / 央行决议）前 24 小时建议关闭新仓位。")
    md.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))


# =========================================================
# 推送（由调用方通过 MCP 工具完成，本脚本只生成文件）
# =========================================================
# 设计说明:
#   推送 (mail / IM) 需要 MCP connector, 不能在脚本里 import。
#   推荐工作流:
#     1) 本脚本生成 .md / .json / .csv / .png
#     2) 由 agent（Buddy）或 automation 读 .md 后调用 mcp__agent-mail__SendMessage
#   这样脚本可纯命令行/CI 调用, 推送层和采集层解耦。
#
# 下面是给 automation 用的 push helper, 直接调用 MCP 工具。
# =========================================================
def push_markdown(path: str, recipient: str = "ares@workbuddy.local"):
    """Read markdown file, send via agent-mail MCP.

    Must be called from the WorkBuddy agent context (not from CLI),
    because mcp__agent-mail__SendMessage is a runtime MCP tool.
    """
    try:
        with open(path, encoding="utf-8") as f:
            body = f.read()
        if len(body) > 8000:
            body = body[:8000] + "\n\n...(完整报告见文件)..."
        # Lazy import works only inside agent runtime
        from mcp__agent_mail import SendMessage
        SendMessage({
            "to": [{"email": recipient}],
            "subject": f"📊 GLD 期权链监控 - {date.today()}",
            "body": body,
            "body_format": "MARKDOWN",
        })
        print("[push] sent via agent-mail")
    except Exception as e:
        print(f"[push] skip (agent-mail unavailable): {e}")


# =========================================================
# Main
# =========================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="GLD")
    ap.add_argument("--days-ahead", type=int, default=60,
                    help="向后搜索到期日的天数（默认 60）")
    ap.add_argument("--top", type=int, default=3,
                    help="推荐行权价数量（每边）")
    ap.add_argument("--plot", action="store_true")
    ap.add_argument("--csv", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--report", choices=["md", "txt"], default="md")
    ap.add_argument("--push", action="store_true",
                    help="推送到手机（通过 agent-mail）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    ticker = args.ticker.upper()
    print(f"[step 1/4] finding expirations for {ticker}...")
    exps = list_expirations(ticker, args.days_ahead)
    print(f"  found {len(exps)} expirations: "
          f"{[e.strftime('%Y-%m-%d') for e in exps]}")
    if not exps:
        print("[err] no expirations found, abort.", file=sys.stderr)
        sys.exit(1)

    print(f"[step 2/4] fetching spot price...")
    try:
        spot = fetch_last_trade_from_chain(ticker)
    except Exception:
        spot = float("nan")
    print(f"  spot: {spot}")

    print(f"[step 3/4] fetching full option chain...")
    df = build_chain_df(ticker, exps)
    print(f"  rows: {len(df)}, columns: {list(df.columns)}")
    if df.empty:
        print("[err] empty chain, abort.", file=sys.stderr)
        sys.exit(1)

    print(f"[step 4/4] analyzing OI walls + recommendations...")
    walls, bands = analyze_oi_walls(df, spot, top=args.top)
    recs = recommend_strikes(df, spot, walls, bands, top=args.top)

    payload = {
        "ticker": ticker,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "spot": spot,
        "expirations": [e.strftime("%Y-%m-%d") for e in exps],
        "bands": {
            "call_band": bands["call_band"],
            "put_band":  bands["put_band"],
            "call_walls": bands["call_walls"],
            "put_walls":  bands["put_walls"],
        },
        "walls_per_expiry": {k.strftime("%Y-%m-%d"): v for k, v in walls.items()},
        "recs": recs,
    }

    out_md  = f"{OUT_DIR}/gld_monitor_report.md"
    out_csv = f"{OUT_DIR}/gld_monitor_chain.csv"
    out_json = f"{OUT_DIR}/gld_monitor_data.json"
    out_png = f"{OUT_DIR}/gld_monitor_oi.png"

    if args.plot or not args.quiet:
        plot_oi(df, spot, bands, out_png, ticker)
        print(f"[png]  {out_png}")
    if args.csv or not args.quiet:
        write_csv(df, out_csv)
        print(f"[csv]  {out_csv}")
    if args.json or not args.quiet:
        write_json(payload, out_json)
        print(f"[json] {out_json}")
    if args.report or not args.quiet:
        write_markdown(payload, out_md)
        print(f"[md]   {out_md}")

    # Print a brief summary to stdout
    print("\n" + "=" * 60)
    print(f"📊 {ticker} @ ${spot:.2f}  |  {len(exps)} 到期日  |  "
          f"{len(df)} 行期权")
    cb = bands["call_band"]; pb = bands["put_band"]
    print(f"   Call OI 墙: ${cb[0]:.0f}–${cb[1]:.0f}  ({len(bands['call_walls'])} peaks)")
    print(f"   Put  OI 墙: ${pb[0]:.0f}–${pb[1]:.0f}  ({len(bands['put_walls'])} peaks)")
    if recs["call"]:
        r = recs["call"][0]
        print(f"   ➡️ 最佳 Sell Call: ${r['strike']:.0f} "
              f"({r['pct_otm']:+.1f}% OTM, 30DTE=${r['mid']:.2f})")
    if recs["put"]:
        r = recs["put"][0]
        print(f"   ➡️ 最佳 Sell Put : ${r['strike']:.0f} "
              f"({r['pct_otm']:+.1f}% OTM, 30DTE=${r['mid']:.2f})")

    if args.push:
        push_markdown(out_md)


if __name__ == "__main__":
    main()
