#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
黄金境内外价差监控（期货 + 现货 双对）
- 期货对: SHFE 沪金主力 au0 (元/克) vs COMEX GC (美元/盎司)
- 现货对: SGE Au99.99 (元/克) vs 伦敦金现 XAU (美元/盎司)
- 汇率: 在岸 USDCNY (实时新浪 fx_susdcny; 历史欧央行参考汇率)
- 价差 = 境内(元/克) - 境外(美元/盎司)/31.1035 * USDCNY
- 双口径: 沪金/SGE 交易时段 → 同刻快照(全实时); 闭市 → 收盘口径(全取日线对)
- 输出: outputs/gold_spread_YYYY-MM-DD.html, gold_spread_latest.html(固定名),
       gold_spread_latest.json, gold_spread_snapshots.jsonl
"""
import json, re, urllib.request, urllib.parse, datetime, os

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(WORKSPACE, "outputs")
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
SGE_HEADERS = {"Referer": "https://www.sge.com.cn/sjzx/mrhq", "X-Requested-With": "XMLHttpRequest",
               "User-Agent": "Mozilla/5.0"}

def _with_retry(fn, *args, retries=3, **kw):
    last = None
    for i in range(retries):
        try:
            return fn(*args, **kw)
        except Exception as e:  # 瞬时网络抖动：退避后重试
            last = e
            if i < retries - 1:
                import time
                time.sleep(2 * (i + 1))
    raise last

def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or SINA_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

def http_post(url, data, headers=None, timeout=30):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers or SGE_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))

_http_get_raw, _http_post_raw = http_get, http_post

def http_get(url, headers=None, timeout=30):
    return _with_retry(_http_get_raw, url, headers=headers, timeout=timeout)

def http_post(url, data, headers=None, timeout=30):
    return _with_retry(_http_post_raw, url, data, headers=headers, timeout=timeout)

def parse_sina_jsonp_array(text):
    m = re.search(r"var _=\((.*)\)\s*;?\s*$", text.strip(), re.S)
    if not m:
        raise ValueError("jsonp parse failed: " + text[:120])
    return json.loads(m.group(1))

# ---------- data fetch ----------
def fetch_sina_daily(symbol, inner=False):
    if inner:
        url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_=/"
               "InnerFuturesNewService.getDailyKLine?symbol=" + symbol)
        data = parse_sina_jsonp_array(http_get(url))
        return {d["d"]: float(d["c"]) for d in data if d.get("c")}
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_=/"
           "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=" + symbol)
    data = parse_sina_jsonp_array(http_get(url))
    return {d["date"]: float(d["close"]) for d in data if d.get("close")}

def fetch_fx_history(start="2016-01-01"):
    today = datetime.date.today().isoformat()
    try:
        url = f"https://api.frankfurter.dev/v1/{start}..{today}?base=USD&symbols=CNY"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            js = json.loads(r.read().decode())
        return {d: v["CNY"] for d, v in js["rates"].items()}
    except Exception:
        # fallback: 新浪 USDCNY 日线（date,open,high,low,close），按 start 过滤
        url = ("https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/var%20_=/"
               "NewForexService.getDayKLine?symbol=fx_susdcny")
        raw = http_get(url, timeout=60)
        m = re.search(r'var _=\("(.*)"\)', raw.strip(), re.S)
        if not m:
            raise ValueError("sina fx kline parse failed: " + raw[:120])
        out = {}
        for row in m.group(1).split("|"):
            parts = row.split(",")
            if len(parts) >= 5 and parts[0] >= start and parts[4]:
                out[parts[0]] = float(parts[4])
        if not out:
            raise ValueError("sina fx kline empty after filter")
        return out

def fetch_sge_hist(symbol="Au99.99"):
    js = http_post("https://www.sge.com.cn/graph/Dailyhq", {"instid": symbol})
    return {row[0]: float(row[2]) for row in js["time"] if len(row) >= 3}

def fetch_realtime():
    """returns (au0|None, gc|None, xau|None, fx|None, sge|None)"""
    raw = http_get("https://hq.sinajs.cn/list=au0,hf_GC,hf_XAU,fx_susdcny")
    out = {}
    for line in raw.splitlines():
        m = re.match(r'var hq_str_(\w+)="(.*)"', line.strip())
        if not m:
            continue
        sym, body = m.group(1), [x.strip() for x in m.group(2).split(",")]
        if sym == "au0":
            out["au0"] = float(body[8]) if len(body) > 8 and body[8] else (float(body[6]) if len(body) > 6 and body[6] else None)
        elif sym == "hf_GC":
            out["gc"] = float(body[0]) if body and body[0] else None
        elif sym == "hf_XAU":
            out["xau"] = float(body[0]) if body and body[0] else None
        elif sym == "fx_susdcny":
            out["fx"] = float(body[8]) if len(body) > 8 and body[8] else None
    # SGE Au99.99 实时（官网分钟线，最后一个非空价）
    try:
        js = json.loads(http_get("https://www.sge.com.cn/graph/quotations?instid=Au99.99", headers=SGE_HEADERS))
        prices = [float(x) for x in js.get("data", []) if x not in (None, "", "null")]
        out["sge"] = prices[-1] if prices else None
    except Exception:
        out["sge"] = None
    return (out.get("au0"), out.get("gc"), out.get("xau"), out.get("fx"), out.get("sge"))

# ---------- core calc ----------
def compute_spread_series(d1, d2, fx, days=1300):
    """d1: 境内(元/克), d2: 境外(美元/盎司) → rows aligned by date"""
    rows = []
    for d in sorted(set(d1) & set(d2) & set(fx))[-days:]:
        a, g, f = d1[d], d2[d], fx[d]
        conv = g / 31.1035 * f
        rows.append({"date": d, "d1": a, "d2": g, "fx": f,
                     "conv": round(conv, 2), "spread": round(a - conv, 2),
                     "premium": round((a - conv) / conv * 100, 2)})
    return rows

def percentile(arr, x):
    arr = sorted(arr)
    return round(sum(1 for v in arr if v <= x) / len(arr) * 100, 1)

def pair_stat(rows, cur_d1, cur_d2, cur_fx, cur_spread, cur_premium):
    all_sp = [r["spread"] for r in rows]
    conv_now = cur_d2 / 31.1035 * cur_fx
    return {
        "d1": round(cur_d1, 2), "d2": round(cur_d2, 2), "fx": round(cur_fx, 4),
        "conv": round(conv_now, 2), "spread": round(cur_spread, 2),
        "premium": round(cur_premium, 2),
        "p1": percentile(all_sp[-250:], cur_spread),
        "p2": percentile(all_sp[-500:], cur_spread),
        "p3": percentile(all_sp, cur_spread),
        "n_days": len(all_sp),
        "mean_2y": round(sum(all_sp[-500:]) / 500, 2) if len(all_sp) >= 500 else round(sum(all_sp) / len(all_sp), 2),
        "dates": [r["date"] for r in rows], "spreads": all_sp,
        "premiums": [r["premium"] for r in rows],
        "gap_vs_daily": round(cur_spread - rows[-1]["spread"], 2),
    }

def load_snapshot_stats(cur_spread_f, cur_spread_s):
    path = os.path.join(OUT_DIR, "gold_spread_snapshots.jsonl")
    sf, ss = [], []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("mode") == "同刻快照":
                        if "spread_f" in r: sf.append(r["spread_f"])
                        if "spread_s" in r: ss.append(r["spread_s"])
                except Exception:
                    pass
    return {
        "pf": percentile(sf, cur_spread_f) if len(sf) >= 10 else None,
        "nf": len(sf),
        "ps": percentile(ss, cur_spread_s) if len(ss) >= 10 else None,
        "ns": len(ss),
    }

# ---------- report ----------
def pair_html(title, sub, stat, snap_p, snap_n, color):
    p2 = stat['p2']
    badge_cls = 'b-high' if p2 >= 80 else ('b-low' if p2 <= 20 else 'b-mid')
    badge_txt = '历史高位（≥80分位）' if p2 >= 80 else ('历史低位（≤20分位）' if p2 <= 20 else '中性区间（20-80分位）')
    snap_v = (f"{snap_p}% ({snap_n} 次)" if snap_p is not None else f"累积中(已存 {snap_n} 次)")
    return f"""
<div class="card">
  <h2>{title} <span style="font-weight:400;font-size:13px;color:#666">{sub}</span></h2>
  <div class="stats">
    <div class="stat"><div class="k">境内（元/克）</div><div class="v">{stat['d1']}</div></div>
    <div class="stat"><div class="k">境外（美元/盎司）</div><div class="v">{stat['d2']}</div></div>
    <div class="stat"><div class="k">在岸 USDCNY</div><div class="v">{stat['fx']}</div></div>
    <div class="stat"><div class="k">境外折境内（元/克）</div><div class="v">{stat['conv']}</div></div>
    <div class="stat"><div class="k">价差（元/克）</div><div class="v">{stat['spread']}</div></div>
    <div class="stat"><div class="k">境内溢价率</div><div class="v">{stat['premium']}%</div></div>
    <div class="stat"><div class="k">同刻快照分位（累积）</div><div class="v">{snap_v}</div></div>
    <div class="stat"><div class="k">与日线口径之差</div><div class="v">{('+' if stat['gap_vs_daily'] > 0 else '')}{stat['gap_vs_daily']}</div></div>
  </div>
  <table class="pc-table">
    <tr><th>价差历史分位</th><th>近1年</th><th>近2年</th><th>近5年</th><th>解读（近2年）</th></tr>
    <tr><td>当前价差所处分位</td><td><b>{stat['p1']}%</b></td><td><b>{stat['p2']}%</b></td><td><b>{stat['p3']}%</b></td>
    <td><span class="badge {badge_cls}">{badge_txt}</span></td></tr>
  </table>
</div>
<div class="card"><div class="chart" id="{color[1]}"></div></div>
<div class="card"><div class="chart" id="{color[2]}"></div></div>"""

CHART_JS = """
function drawChart(id, title, dates, data, color, cur){
  var c = echarts.init(document.getElementById(id));
  c.setOption({
    title:{text:title,left:'center',textStyle:{fontSize:14}},
    tooltip:{trigger:'axis'},
    grid:{left:60,right:30,top:40,bottom:60},
    xAxis:{type:'category',data:dates,axisLabel:{formatter:v=>v.slice(0,7)}},
    yAxis:{type:'value',scale:true},
    dataZoom:[{type:'inside'},{type:'slider',height:18,bottom:12}],
    series:[{type:'line',data:data,showSymbol:false,lineStyle:{width:1.5,color:color},
      areaStyle:{color:{type:'linear',x:0,y:0,x2:0,y2:1,colorStops:[{offset:0,color:color.replace(')',',.22)').replace('rgb','rgba')},{offset:1,color:'rgba(0,0,0,0)'}]}},
      markLine:(cur!=null)?{symbol:'none',data:[{yAxis:cur,label:{formatter:'当前 '+cur},lineStyle:{color:'#2980b9',type:'dashed'}}]}:undefined
    }]
  });
  window.addEventListener('resize',function(){c.resize();});
}
"""

def build_html(stat_f, stat_s, snap, mode, ts, note):
    today = datetime.date.today().isoformat()
    blocks = (
        pair_html('📈 期货对：沪金主力 au0 vs COMEX GC', '期货 vs 期货', stat_f, snap['pf'], snap['nf'], ('rgb(192,57,43)', 'cf1', 'cf2'))
        + pair_html('🪙 现货对：SGE Au99.99 vs 伦敦金现 XAU', '现货 vs 现货', stat_s, snap['ps'], snap['ns'], ('rgb(184,134,11)', 'cs1', 'cs2'))
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>黄金境内外价差监控 - {today}</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
  body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#f5f6f8;color:#1a1a2e;margin:0;padding:16px;}}
  .card{{background:#fff;border-radius:12px;padding:16px 20px;margin-bottom:14px;box-shadow:0 1px 4px rgba(0,0,0,.06);}}
  h1{{font-size:19px;margin:0 0 4px;}} h2{{font-size:15px;margin:0 0 8px;}} .sub{{color:#888;font-size:12px;margin-bottom:10px;}}
  .stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:6px 0;}}
  .stat{{background:#f8f9fb;border-radius:10px;padding:10px 12px;}}
  .stat .k{{font-size:11px;color:#888;}} .stat .v{{font-size:16px;font-weight:600;margin-top:3px;}}
  .pc-table{{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px;}}
  .pc-table td,.pc-table th{{padding:7px 10px;border-bottom:1px solid #eee;text-align:left;}}
  .badge{{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600;}}
  .b-high{{background:#fde8e8;color:#c0392b;}} .b-mid{{background:#fff4dd;color:#b7791f;}} .b-low{{background:#e8f5ee;color:#1e7d4e;}}
  .chart{{width:100%;height:340px;}} .note{{font-size:12px;color:#999;line-height:1.8;}}
  @media (max-width:700px){{.stats{{grid-template-columns:repeat(2,1fr);}}}}
</style></head><body>
<div class="card">
  <h1>🥇 黄金境内外价差监控 <span style="font-weight:400;font-size:13px;color:#666">{today} {ts}</span></h1>
  <div class="sub">口径：<b>{mode}</b>（交易时段=同刻快照全实时；闭市=收盘对收盘）· 价差 = 境内 − 境外÷31.1035×在岸USDCNY</div>
</div>
{blocks}
<div class="card"><div class="note">{note}</div></div>
<script>{CHART_JS}
drawChart('cf1','期货价差（元/克）· 近5年',{json.dumps(stat_f['dates'])},{json.dumps(stat_f['spreads'])},'rgb(192,57,43)',{stat_f['spread']});
drawChart('cf2','期货境内溢价率（%）· 近5年',{json.dumps(stat_f['dates'])},{json.dumps(stat_f['premiums'])},'rgb(192,57,43)',null);
drawChart('cs1','现货价差（元/克）· 近5年',{json.dumps(stat_s['dates'])},{json.dumps(stat_s['spreads'])},'rgb(184,134,11)',{stat_s['spread']});
drawChart('cs2','现货境内溢价率（%）· 近5年',{json.dumps(stat_s['dates'])},{json.dumps(stat_s['premiums'])},'rgb(184,134,11)',null);
</script></body></html>"""

def shfe_in_session():
    """沪金可交易时段（北京时间）：日盘周一~周五 09:00-10:15 / 10:30-11:30 / 13:30-15:00，
    夜盘 21:00-次日02:30（周五夜盘尾段落在周六凌晨，周日夜盘开启新一周）。不含法定节假日。"""
    from zoneinfo import ZoneInfo
    now = datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
    hm = now.hour * 60 + now.minute
    wd = now.weekday()
    day = wd <= 4 and (540 <= hm <= 615 or 630 <= hm <= 690 or 810 <= hm <= 900)
    night = (wd <= 4 and hm >= 1260) or (wd <= 5 and hm <= 150) or (wd == 6 and hm >= 1260)
    return day or night


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    # 非沪金交易时段 → 数据暂停更新（FORCE_RUN=1 可强制）
    if os.environ.get("FORCE_RUN") != "1" and not shfe_in_session():
        print("OUT_OF_SESSION 沪金非交易时段，数据暂停更新")
        return 0
    # 历史
    au_d = fetch_sina_daily("au0", inner=True)
    gc_d = fetch_sina_daily("GC")
    xau_d = fetch_sina_daily("XAU")
    sge_d = fetch_sge_hist("Au99.99")
    fx_h = fetch_fx_history()

    rows_f = compute_spread_series(au_d, gc_d, fx_h)
    rows_s = compute_spread_series(sge_d, xau_d, fx_h)

    # 实时
    au_rt, gc_rt, xau_rt, fx_rt, sge_rt = fetch_realtime()
    last_f, last_s = rows_f[-1], rows_s[-1]
    if au_rt:
        mode = "同刻快照"
        conv_f = gc_rt / 31.1035 * fx_rt
        stat_f = pair_stat(rows_f, au_rt, gc_rt, fx_rt, au_rt - conv_f, (au_rt - conv_f) / conv_f * 100)
        d1s = sge_rt if sge_rt is not None else last_s["d1"]
        conv_s = xau_rt / 31.1035 * fx_rt
        stat_s = pair_stat(rows_s, d1s, xau_rt, fx_rt, d1s - conv_s, (d1s - conv_s) / conv_s * 100)
    else:
        mode = "收盘口径"
        conv_f = last_f["d2"] / 31.1035 * last_f["fx"]
        stat_f = pair_stat(rows_f, last_f["d1"], last_f["d2"], last_f["fx"], last_f["spread"], last_f["premium"])
        conv_s = last_s["d2"] / 31.1035 * last_s["fx"]
        stat_s = pair_stat(rows_s, last_s["d1"], last_s["d2"], last_s["fx"], last_s["spread"], last_s["premium"])

    snap = load_snapshot_stats(stat_f["spread"], stat_s["spread"])

    # 快照沉淀
    if mode == "同刻快照":
        rec = {"ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "mode": mode,
               "au": stat_f["d1"], "gc": stat_f["d2"], "fx": stat_f["fx"],
               "spread_f": stat_f["spread"], "premium_f": stat_f["premium"],
               "sge": stat_s["d1"], "xau": stat_s["d2"],
               "spread_s": stat_s["spread"], "premium_s": stat_s["premium"]}
        with open(os.path.join(OUT_DIR, "gold_spread_snapshots.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    ts = datetime.datetime.now().strftime("%H:%M:%S")
    note = ("<b>口径说明：</b>① 日线口径历史分位（近1/2/5年）：境内日线收盘（沪金15:00 / SGE 15:30）对境外日线收盘"
            "（GC/XAU 均为北京时间次日凌晨），含隔夜跳空噪声，仅作长期分布参考；② <b>同刻快照</b>：盘中实时抓取，"
            "严格同一时刻、无跳空——工作日 9:00–14:00 每小时运行沉淀该序列，为主要信号口径。"
            "期货对=沪金主力 vs COMEX GC；现货对=SGE Au99.99 vs 伦敦金现 XAU（现货历史：上金所官网日线，2016-12 起）。"
            "汇率历史与在岸即期有 ±0.1% 差异。价差为正=境内溢价，为负=境内折价。")

    html = build_html(stat_f, stat_s, snap, mode, ts, note)

    for s in (stat_f, stat_s):
        s.pop("dates"), s.pop("spreads"), s.pop("premiums")
    out = {"date": datetime.date.today().isoformat(), "ts": ts, "mode": mode,
           "期货": stat_f, "现货": stat_s, "snap": snap, "note": note}
    p1 = os.path.join(OUT_DIR, f"gold_spread_{out['date']}.html")
    p2 = os.path.join(OUT_DIR, "gold_spread_latest.html")
    for p in (p1, p2):
        with open(p, "w", encoding="utf-8") as f:
            f.write(html)
    with open(os.path.join(OUT_DIR, "gold_spread_latest.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2)[:2400])
    print("HTML:", p1)

if __name__ == "__main__":
    main()
