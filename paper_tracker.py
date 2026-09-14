#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
纸面持仓跟踪器（不改动监控脚本，读取监控输出 + 历史数据做触发判断）
- 触发: 同刻快照口径，现货价差 ≥ 平滑序列 p80 → 做空价差；≤ p20 → 做多价差；期货对同理
- 持仓: 止盈=回归半程, 止损=±12元/克, 最长60个交易日(≈84日历天), 每日计息0.016元/克
- 状态: outputs/paper_position.json  |  成交流水: outputs/paper_trades.jsonl
"""
import json, os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gold_spread_monitor as gm

OUT_DIR = gm.OUT_DIR
STATE_F = os.path.join(OUT_DIR, "paper_position.json")
TRADES_F = os.path.join(OUT_DIR, "paper_trades.jsonl")
SL = 12.0
COST_PER_DAY = 0.0156   # 元/克/天: 名义950元 × 10%保证金 × 6%年化 / 365
MAX_HOLD_DAYS = 84      # 日历天 ≈ 60交易日

def smoothed(spreads, k=3):
    return [sum(spreads[max(0, i - k + 1):i + 1]) / len(spreads[max(0, i - k + 1):i + 1])
            for i in range(len(spreads))]

def levels(rows):
    sp = smoothed([r["spread"] for r in rows])[-500:]
    sp_sorted = sorted(sp)
    p80 = sp_sorted[int(len(sp_sorted) * 0.80) - 1]
    p20 = sp_sorted[int(len(sp_sorted) * 0.20) - 1]
    mean = sum(sp) / len(sp)
    return p80, p20, mean

def load_state():
    if os.path.exists(STATE_F):
        with open(STATE_F, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(s):
    with open(STATE_F, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

def log_trade(t):
    with open(TRADES_F, "a", encoding="utf-8") as f:
        f.write(json.dumps(t, ensure_ascii=False) + "\n")

CACHE_F = os.path.join(OUT_DIR, "history_cache.json")

def fetch_cached(key, fn):
    cache = {}
    if os.path.exists(CACHE_F):
        try:
            cache = json.load(open(CACHE_F, encoding="utf-8"))
        except Exception:
            cache = {}
    try:
        data = fn()
        cache[key] = data
        with open(CACHE_F, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        return data
    except Exception:
        if key in cache:
            return cache[key]  # 网络失败时用缓存
        raise

def main():
    latest = json.load(open(os.path.join(OUT_DIR, "gold_spread_latest.json"), encoding="utf-8"))
    au_d = fetch_cached("au0", lambda: gm.fetch_sina_daily("au0", inner=True))
    gc_d = fetch_cached("GC", lambda: gm.fetch_sina_daily("GC"))
    xau_d = fetch_cached("XAU", lambda: gm.fetch_sina_daily("XAU"))
    sge_d = fetch_cached("sge", lambda: gm.fetch_sge_hist("Au99.99"))
    fx_h = fetch_cached("fx", lambda: gm.fetch_fx_history())
    rows_f = gm.compute_spread_series(au_d, gc_d, fx_h, days=1300)
    rows_s = gm.compute_spread_series(sge_d, xau_d, fx_h, days=1300)
    p80_f, p20_f, mean_f = levels(rows_f)
    p80_s, p20_s, mean_s = levels(rows_s)

    now = datetime.datetime.now().isoformat(timespec="seconds")
    state = load_state()
    events = []

    pairs = {
        "期货": {"cur": latest["期货"]["spread"], "p80": p80_f, "p20": p20_f, "mean": mean_f,
                 "d1": latest["期货"]["d1"], "d2": latest["期货"]["d2"], "conv": latest["期货"]["conv"]},
        "现货": {"cur": latest["现货"]["spread"], "p80": p80_s, "p20": p20_s, "mean": mean_s,
                 "d1": latest["现货"]["d1"], "d2": latest["现货"]["d2"], "conv": latest["现货"]["conv"]},
    }

    for name, p in pairs.items():
        pos = state.get(name)
        # ---- 持仓管理 ----
        if pos:
            held_days = (datetime.datetime.now() - datetime.datetime.fromisoformat(pos["entry_ts"])).days
            pos["accrued_cost"] = round(held_days * COST_PER_DAY, 3)
            pos["cur_spread"] = p["cur"]
            pos["pnl_gross"] = round((pos["entry_spread"] - p["cur"]) * pos["direction"] * -1 if False else
                                     (p["cur"] - pos["entry_spread"]) * pos["direction"], 2)
            pos["pnl_net"] = round(pos["pnl_gross"] - pos["accrued_cost"], 2)
            pos["held_days"] = held_days
            exit_reason = None
            if pos["direction"] == -1:
                if p["cur"] <= pos["tp"]: exit_reason = "止盈(回归半程)"
                elif p["cur"] >= pos["sl_price"]: exit_reason = "止损"
            else:
                if p["cur"] >= pos["tp"]: exit_reason = "止盈(回归半程)"
                elif p["cur"] <= pos["sl_price"]: exit_reason = "止损"
            if held_days >= MAX_HOLD_DAYS:
                exit_reason = "超时平仓(60交易日)"
            if exit_reason:
                trade = dict(pos, exit_ts=now, exit_spread=p["cur"], exit_reason=exit_reason)
                log_trade(trade)
                events.append(f"{name}对平仓：{exit_reason}，净盈亏 {trade['pnl_net']} 元/克")
                state[name] = None
            else:
                events.append(f"{name}对持仓中：方向{'空价差' if pos['direction']==-1 else '多价差'}，"
                              f"浮动 {pos['pnl_gross']:+.2f} 元/克（净 {pos['pnl_net']:+.2f}），已持有 {held_days} 天")
            continue
        # ---- 触发判断（仅同刻快照口径）----
        if latest["mode"] != "同刻快照":
            continue
        direction = None
        if p["cur"] >= p["p80"]:
            direction = -1  # 做空价差: 赌走窄
        elif p["cur"] <= p["p20"]:
            direction = +1  # 做多价差: 赌走扩
        if direction is None:
            continue
        tp = p["cur"] + (p["mean"] - p["cur"]) * 0.5
        pos = {"entry_ts": now, "direction": direction, "entry_spread": p["cur"],
               "trigger": "≥p80" if direction == -1 else "≤p20",
               "mean_at_entry": round(p["mean"], 2), "tp": round(tp, 2),
               "sl_price": round(p["cur"] + SL * (-direction if direction == -1 else direction) * -1, 2) if False else round(p["cur"] + (SL if direction == -1 else -SL), 2),
               "legs": {"境内": p["d1"], "境外": p["d2"], "conv": p["conv"]},
               "accrued_cost": 0.0, "pnl_gross": 0.0, "pnl_net": 0.0, "held_days": 0}
        state[name] = pos
        events.append(f"⚠️ {name}对开仓（纸面）：{'做空价差(赌走窄)' if direction == -1 else '做多价差(赌走扩)'}，"
                      f"入场价差 {p['cur']}，止盈 {pos['tp']}，止损 {pos['sl_price']}，"
                      f"境内腿 {p['d1']} / 境外折境内 {p['conv']}")

    # 未持仓时给出观察距离
    watch = []
    for name, p in pairs.items():
        if not state.get(name):
            watch.append(f"{name}对：现价差 {p['cur']}，做空触发 {p['p80']}（差 {round(p['p80']-p['cur'],2)}），"
                         f"做多触发 {p['p20']}（差 {round(p['cur']-p['p20'],2)}）")

    save_state(state)
    print(json.dumps({"ts": now, "mode": latest["mode"], "events": events,
                      "watch": watch, "positions": {k: v for k, v in state.items() if v}},
                     ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
