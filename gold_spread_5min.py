#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""黄金境内外价差 · 5 分钟微观研究

三腿对齐：
  境内  沪金主力连续 au0（新浪财经，元/克，真实价格）
  境外  COMEX 黄金 GC（东方财富 101.GC00Y，美元/盎司）
  汇率  离岸 USDCNH（东方财富 133.USDCNH）—— 在岸 5 分钟无免费历史，以离岸代替并标注

只保留境内期货可交易时段：
  日盘 09:05-10:15 / 10:30-11:30 / 13:35-15:00
  夜盘 21:05-23:55 / 00:00-02:30（归属前一交易日）

输出：均值、标准差、当前 σ 位置、σ 分布占用、日盘/夜盘分层、盘中时间曲线
产物：outputs/gold_spread_5min.html、outputs/gold_spread_5min.json
归档：data5m/archive_5m.jsonl（逐日沉淀，用于将来延长窗口）
"""
import os, json, re, time, datetime, math, urllib.request, statistics

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data5m")
OUT_DIR = os.path.join(BASE, "outputs")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# 价差窗口：外盘 5 分钟（东财）保留约 1400 根，23 小时盘 ≈ 5 个交易日，是当前可对齐的最长窗口
LOOKBACK_DAYS = 14


# ---------------------------------------------------------------- 数据抓取
def _get(url, referer, timeout=40, tries=3, wait=8):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception:
            if i < tries - 1:
                time.sleep(wait)
    return None


def _cache_path(name):
    return os.path.join(DATA_DIR, name)


def _load_cache(name):
    p = _cache_path(name)
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return None
    return None


def _save_cache(name, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump(obj, open(_cache_path(name), "w", encoding="utf-8"), ensure_ascii=False)


def fetch_au0_5m():
    """沪金主力连续 5 分钟（新浪，真实价格；接口固定返回 1023 根 ≈ 10 个交易日）"""
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_=/"
           "InnerFuturesNewService.getFewMinLine?symbol=au0&type=5")
    t = _get(url, "https://finance.sina.com.cn", tries=3)
    if t and '"d"' in t:
        arr = json.loads(t[t.index("(") + 1:t.rindex(")")])
        bars = [{"t": x["d"], "c": float(x["c"])} for x in arr if x.get("c")]
        if bars:
            _save_cache("au_sina.json", bars)
            return bars
    cached = _load_cache("au_sina.json")
    if cached:
        if isinstance(cached, dict):
            cached = [{"t": x["d"], "c": float(x["c"])} for x in cached.get("bars", [])]
        return cached
    return []


def _fetch_em_5m(secid, cache_name):
    """东方财富 5 分钟（f51 时间, f52 开, f53 收, f54 高, f55 低, f56 量）"""
    beg = (datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    end = datetime.date.today().strftime("%Y%m%d")
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
           f"&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56"
           f"&klt=5&fqt=0&beg={beg}&end={end}&lmt=10000")
    t = _get(url, "https://quote.eastmoney.com/", tries=3, wait=10)
    if t:
        try:
            d = (json.loads(t).get("data") or {})
            kl = d.get("klines") or []
            bars = []
            for row in kl:
                f = row.split(",")
                if len(f) >= 3 and f[2]:
                    bars.append({"t": f[0], "c": float(f[2])})
            if bars:
                _save_cache(cache_name, {"name": d.get("name"), "bars": bars})
                return bars
        except Exception:
            pass
    cached = _load_cache(cache_name)
    if cached:
        return cached.get("bars", [])
    return []


def fetch_gc_5m():
    return _fetch_em_5m("101.GC00Y", "gc_em.json")


def fetch_cnh_5m():
    return _fetch_em_5m("133.USDCNH", "cnh_em.json")


# ---------------------------------------------------------------- 交易时段守卫
def in_session(now=None):
    """沪金可交易时段判断（北京时间）。日盘周一~周五 09:00-10:15 / 10:30-11:30 / 13:30-15:00；
    夜盘 21:00-次日02:30（周五夜盘尾段落在周六凌晨，周日夜盘开启新一周）。
    不含法定节假日日历——节假日盘中实时价会自然缺失，报告回落为收盘口径。"""
    from zoneinfo import ZoneInfo
    now = now or datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
    hm = now.hour * 60 + now.minute
    wd = now.weekday()  # Mon=0 .. Sun=6
    day = wd <= 4 and (540 <= hm <= 615 or 630 <= hm <= 690 or 810 <= hm <= 900)
    night = (wd <= 4 and hm >= 1260) or (wd <= 5 and hm <= 150) or (wd == 6 and hm >= 1260)
    return day or night


# ---------------------------------------------------------------- 实时三腿报价
def fetch_realtime_3leg():
    """新浪实时报价快照（生成时刻）：沪金 au0 / COMEX GC / 离岸 USDCNH，供与报告结果交叉验证"""
    res = {"au": None, "gc": None, "fx": None, "fx_time": None, "gc_time": None}
    t = _get("https://hq.sinajs.cn/list=au0,hf_GC,fx_susdcnh",
             "https://finance.sina.com.cn", timeout=15, tries=2, wait=3)
    if not t:
        return res

    def pick(fields, idxs, lo=0.0, hi=1e12):
        for i in idxs:
            if i < len(fields):
                try:
                    v = float(fields[i])
                    if lo <= v <= hi:
                        return v
                except ValueError:
                    continue
        return None

    for line in t.splitlines():
        m = re.match(r'var hq_str_(\w+)="(.*)"', line.strip())
        if not m:
            continue
        sym, f = m.group(1), m.group(2).split(",")
        if sym == "au0":
            res["au"] = pick(f, [7, 8, 6], lo=100)     # 新浪内盘期货：7=最新价（8结算/6卖价兜底）
        elif sym == "hf_GC":
            res["gc"] = pick(f, [0, 7, 8], lo=100)     # hf_ 格式：0=最新价
            res["gc_time"] = f[6] if len(f) > 6 else None
        elif sym == "fx_susdcnh":
            res["fx"] = pick(f, [8, 7, 1], lo=1, hi=20)
            res["fx_time"] = f[0] if f else None
    return res


# ---------------------------------------------------------------- 时段划分
DAY_SEGS = [((9, 5), (10, 15)), ((10, 30), (11, 30)), ((13, 35), (15, 0))]
NIGHT_SEGS = [((21, 5), (23, 55)), ((0, 0), (2, 30))]


def _in(seg, hm):
    return seg[0] <= hm <= seg[1]


def classify(dt):
    """返回 ('日盘'|'夜盘'|None, 归属交易日)"""
    hm = (dt.hour, dt.minute)
    if any(_in(s, hm) for s in DAY_SEGS):
        return "日盘", dt.date()
    if any(_in(s, hm) for s in NIGHT_SEGS):
        # 凌晨时段归属前一交易日
        d = dt.date() if dt.hour >= 21 else dt.date() - datetime.timedelta(days=1)
        return "夜盘", d
    return None, None


# ---------------------------------------------------------------- 对齐
def align(au_bars, gc_bars, fx_bars, tol_gc=10, tol_fx=20):
    au = sorted([(datetime.datetime.strptime(b["t"], "%Y-%m-%d %H:%M:%S"), b["c"]) for b in au_bars])
    gc = sorted([(datetime.datetime.strptime(b["t"], "%Y-%m-%d %H:%M"), b["c"]) for b in gc_bars])
    fx = sorted([(datetime.datetime.strptime(b["t"], "%Y-%m-%d %H:%M"), b["c"]) for b in fx_bars])

    def last_le(series, t, tol):
        lo, hi = 0, len(series) - 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if series[mid][0] <= t:
                best = series[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        if best and (t - best[0]).total_seconds() <= tol * 60:
            return best[1]
        return None

    rows = []
    for dt, au_c in au:
        sess, tday = classify(dt)
        if sess is None:
            continue
        gc_c = last_le(gc, dt, tol_gc)
        fx_c = last_le(fx, dt, tol_fx)
        if gc_c is None or fx_c is None:
            continue
        conv = gc_c / 31.1035 * fx_c          # 境外价折算为 元/克
        rows.append({
            "t": dt.strftime("%Y-%m-%d %H:%M"), "dt": dt, "date": tday.isoformat(),
            "sess": sess, "au": au_c, "gc": gc_c, "fx": fx_c,
            "conv": conv, "spread": au_c - conv, "prem": (au_c - conv) / conv * 100,
        })
    return rows


# ---------------------------------------------------------------- 统计
def describe(vals):
    if not vals:
        return {}
    n = len(vals)
    m = sum(vals) / n
    sd = statistics.stdev(vals) if n > 1 else 0.0
    sv = sorted(vals)
    med = sv[n // 2] if n % 2 else (sv[n // 2 - 1] + sv[n // 2]) / 2
    return {"n": n, "mean": m, "sd": sd, "min": sv[0], "max": sv[-1], "median": med}


def build_stats(rows):
    sp = [r["spread"] for r in rows]
    d = describe(sp)
    cur = rows[-1]
    z = (cur["spread"] - d["mean"]) / d["sd"] if d["sd"] else 0.0
    rank = sum(1 for v in sp if v <= cur["spread"]) / len(sp) * 100

    occ = {}
    for k in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
        occ[str(k)] = sum(1 for v in sp if abs(v - d["mean"]) <= k * d["sd"]) / len(sp) * 100

    # 分层
    sess = {}
    for name in ("日盘", "夜盘"):
        sub = [r["spread"] for r in rows if r["sess"] == name]
        s = describe(sub)
        last_sub = [r for r in rows if r["sess"] == name]
        s["cur_z"] = round((last_sub[-1]["spread"] - s["mean"]) / s["sd"], 2) if s.get("sd") else None
        sess[name] = s

    daily = []
    for day in sorted({r["date"] for r in rows}):
        sub = [r for r in rows if r["date"] == day]
        dd = describe([r["spread"] for r in sub])
        dd["date"] = day
        dd["open"] = sub[0]["spread"]
        dd["close"] = sub[-1]["spread"]
        daily.append(dd)

    prof = []
    buckets = {}
    for r in rows:
        key = r["dt"].strftime("%H:%M")
        buckets.setdefault(key, []).append(r["spread"])
    for key in sorted(buckets):
        vals = buckets[key]
        prof.append({"t": key, "mean": sum(vals) / len(vals), "n": len(vals)})

    ext = sorted(rows, key=lambda r: r["spread"])
    extremes = {
        "low": [{"t": r["t"], "v": r["spread"]} for r in ext[:3]],
        "high": [{"t": r["t"], "v": r["spread"]} for r in ext[-3:]][::-1],
    }
    return {"desc": d, "cur": cur, "z": z, "rank": rank, "occ": occ,
            "sess": sess, "daily": daily, "profile": prof, "extremes": extremes}


# ---------------------------------------------------------------- 卡片：实时报价 / 开仓提示
def realtime_card(rt, cur):
    """实时三腿报价卡片：沪金 au0 / COMEX GC / 离岸 USDCNH（生成时刻快照，供交叉验证）"""
    if not rt:
        return ""
    au_rt, gc_rt, fx_rt = rt.get("au"), rt.get("gc"), rt.get("fx")
    au_html = (f"<b style='color:#c0392b'>{au_rt:,.2f}</b>"
               if au_rt else f"已收盘 · 最近5分钟收盘 <b style='color:#c0392b'>{cur['au']:,.2f}</b>")
    gc_html = (f"<b style='color:#c0392b'>{gc_rt:,.2f}</b>"
               if gc_rt else f"<b style='color:#c0392b'>{cur['gc']:,.2f}</b>（最近5分钟收盘）")
    fx_html = (f"<b style='color:#c0392b'>{fx_rt:.4f}</b>"
               if fx_rt else f"<b style='color:#c0392b'>{cur['fx']:.4f}</b>（最近5分钟收盘）")
    snap = ""
    if au_rt and gc_rt and fx_rt:
        conv_rt = gc_rt / 31.1035 * fx_rt
        spread_rt = au_rt - conv_rt
        snap = (f"<div style='margin-top:10px;padding:10px 12px;background:#fff8f0;border-radius:8px;font-size:13px'>"
                f"<b>实时同刻校验</b>：境外折算 = {gc_rt:,.2f} ÷ 31.1035 × {fx_rt:.4f} = <b>{conv_rt:,.2f}</b> 元/克，"
                f"实时价差 = {au_rt:,.2f} − {conv_rt:,.2f} = <b style='color:{'#c0392b' if spread_rt >= 0 else '#1e7d4e'}'>{spread_rt:+.2f}</b> 元/克"
                f"（正=境内溢价，负=境内折价）</div>")
    return f"""
<div class="card">
  <h2>🔴 实时三腿报价 <span style="font-weight:400;font-size:12px;color:#888">生成时刻快照 · 可与任意行情软件交叉验证</span></h2>
  <div class="stats" style="grid-template-columns:repeat(4,1fr)">
    <div class="stat"><div class="k">沪金主力 au0（元/克）</div><div class="v">{au_html}</div></div>
    <div class="stat"><div class="k">COMEX GC（美元/盎司）</div><div class="v">{gc_html}</div></div>
    <div class="stat"><div class="k">离岸 USDCNH</div><div class="v">{fx_html}</div></div>
    <div class="stat"><div class="k">报价时间</div><div class="v" style="font-size:13px">{rt.get('gc_time') or '—'}（美盘）/ {rt.get('fx_time') or '—'}（汇市）</div></div>
  </div>
  {snap}
</div>"""


def signal_card(sig):
    """±1.5σ 开仓提示卡片（提示性表述，非投资建议）"""
    if not sig:
        return ""
    u, lo = sig["upper_15"], sig["lower_15"]
    if sig["state"] == "short_zone":
        body = (f"当前价差 <b>{sig['cur']:+.2f}</b> 元/克，已<b>上穿均值+1.5σ（{u:+.2f}）</b>，位于 +{sig['z']}σ。"
                f"提示性信号：<b>价差偏贵区间，历史样本内偏向回落收敛</b>。若执行，方向为「卖境内 + 买境外」；"
                f"建议先对照上方实时三腿确认非数据错位，再分批建仓并预设止损（价差再走扩即离场）。")
        badge = "<span class='badge b-high'>做空价差区间</span>"
        color = "#fde8e8"
    elif sig["state"] == "long_zone":
        body = (f"当前价差 <b>{sig['cur']:+.2f}</b> 元/克，已<b>下破均值−1.5σ（{lo:+.2f}）</b>，位于 {sig['z']}σ。"
                f"提示性信号：<b>价差偏便宜区间，历史样本内偏向回升收敛</b>。若执行，方向为「买境内 + 卖境外」；"
                f"同样建议实时校验、分批建仓、预设止损。")
        badge = "<span class='badge b-low'>做多价差区间</span>"
        color = "#e8f5ee"
    else:
        body = (f"当前价差 <b>{sig['cur']:+.2f}</b> 元/克，处于 ±1.5σ 中性带内（{sig['z']:+.2f}σ），<b>无开仓提示</b>。"
                f"距做空触发线（均值+1.5σ = <b>{u:+.2f}</b>）还差 <b>{sig['dist_upper']:.2f}</b> 元/克；"
                f"距做多触发线（均值−1.5σ = <b>{lo:+.2f}</b>）还差 <b>{sig['dist_lower']:.2f}</b> 元/克。"
                f"触及任一触发线后，本栏将给出方向性提示。")
        badge = "<span class='badge b-mid'>中性 · 观望</span>"
        color = "#fff8f0"
    return f"""
<div class="card" style="background:{color}">
  <h2>🎯 ±1.5σ 开仓提示 <span style="font-weight:400;font-size:12px;color:#888">提示性表述 · 非投资建议 · 口径为 5 分钟同刻价差</span> {badge}</h2>
  <div style="font-size:13px;line-height:1.9">{body}</div>
  <div class="note" style="margin-top:8px">触发线随窗口滚动更新（窗口延长时均值与 σ 会变）。±1.5σ 在正态假设下约对应 13% 双尾概率，样本内为稀有事件区；5 分钟口径噪声大于日线，<b>建议以两次相邻快照同向确认后再动作</b>。</div>
</div>"""


# ---------------------------------------------------------------- 图表
CHART_JS = """
function mk(el){
  var c = echarts.init(document.getElementById(el));
  window.addEventListener('resize', function(){ c.resize(); });
  return c;
}
function spreadChart(el, o){
  var mkLine = function(v, name, color, dash){
    return {name: name, type: 'line', data: o.t.map(function(){return v;}),
            symbol: 'none', lineStyle: {color: color, width: 1, type: dash || 'dashed'},
            tooltip: {show: false}, silent: true};
  };
  var c = mk(el);
  c.setOption({
    animation: false,
    grid: {left: 52, right: 16, top: 34, bottom: 34},
    tooltip: {trigger: 'axis', valueFormatter: function(v){ return (+v).toFixed(2) + ' 元/克'; }},
    legend: {top: 0, itemWidth: 14, textStyle: {fontSize: 11}},
    xAxis: {type: 'category', data: o.t, axisLabel: {fontSize: 10, interval: Math.floor(o.t.length/8)}},
    yAxis: {type: 'value', scale: true, axisLabel: {fontSize: 10}, splitLine: {lineStyle: {color: '#f0f0f4'}}},
    series: [
      mkLine(o.mean + 2*o.sd, '均值+2σ', '#c0392b', 'dotted'),
      mkLine(o.mean + o.sd,   '均值+1σ', '#e08a7f'),
      mkLine(o.mean,          '均值',    '#888'),
      mkLine(o.mean - o.sd,   '均值−1σ', '#7fb99a'),
      mkLine(o.mean - 2*o.sd, '均值−2σ', '#1e7d4e', 'dotted'),
      {name: '价差（5分钟）', type: 'line', data: o.s, symbol: 'none',
       lineStyle: {color: 'rgb(192,57,43)', width: 1.4},
       itemStyle: {color: 'rgb(192,57,43)'}}
    ]
  });
}
function histChart(el, o){
  var c = mk(el);
  c.setOption({
    animation: false,
    grid: {left: 52, right: 16, top: 30, bottom: 34},
    tooltip: {trigger: 'axis'},
    xAxis: {type: 'category', data: o.bins, name: '价差（元/克）', nameLocation: 'middle', nameGap: 24,
            axisLabel: {fontSize: 10, interval: Math.max(0, Math.floor(o.bins.length/10))}},
    yAxis: {type: 'value', axisLabel: {fontSize: 10}, splitLine: {lineStyle: {color: '#f0f0f4'}}},
    series: [{
      type: 'bar', data: o.counts, itemStyle: {color: '#b8c4d8'}, barWidth: '92%',
      markLine: {silent: true, symbol: 'none', lineStyle: {color: 'rgb(192,57,43)', width: 2},
                 label: {formatter: '当前', fontSize: 11, color: 'rgb(192,57,43)'},
                 data: [{xAxis: o.curBin}]}
    }]
  });
}
function profileChart(el, o){
  var c = mk(el);
  c.setOption({
    animation: false,
    grid: {left: 52, right: 16, top: 34, bottom: 40},
    tooltip: {trigger: 'axis', valueFormatter: function(v){ return (+v).toFixed(2) + ' 元/克'; }},
    legend: {top: 0, itemWidth: 14, textStyle: {fontSize: 11}},
    xAxis: {type: 'category', data: o.t, axisLabel: {fontSize: 10, interval: Math.floor(o.t.length/10)}},
    yAxis: {type: 'value', scale: true, axisLabel: {fontSize: 10}, splitLine: {lineStyle: {color: '#f0f0f4'}}},
    series: [
      {name: '盘中均值', type: 'line', data: o.mean, symbol: 'none',
       lineStyle: {color: '#2b6cb0', width: 1.6}, areaStyle: {color: 'rgba(43,108,176,.08)'}},
      {name: '全样本均值', type: 'line', data: o.t.map(function(){return o.allMean;}),
       symbol: 'none', lineStyle: {color: '#888', width: 1, type: 'dashed'}, silent: true}
    ]
  });
}
"""


def build_html(stats, meta, note):
    d = stats["desc"]
    cur = stats["cur"]
    z = stats["z"]
    absz = abs(z)
    band_cls = "b-high" if absz >= 2 else ("b-mid" if absz >= 1 else "b-low")
    band_txt = ("极端偏贵" if z >= 2 else "偏贵" if z >= 1 else
                "极端偏便宜" if z <= -2 else "偏便宜" if z <= -1 else "中性")
    sp = [r["spread"] for r in meta["rows"]]
    lo, hi = d["min"], d["max"]
    bins, counts = [], []
    nb = 26
    step = (hi - lo) / nb if hi > lo else 1
    for i in range(nb):
        a = lo + i * step
        bins.append(f"{a:.2f}")
        counts.append(sum(1 for v in sp if a <= v < a + step))
    counts[-1] += sum(1 for v in sp if v >= lo + nb * step)
    cur_bin = min(nb - 1, max(0, int((cur["spread"] - lo) / step))) if step else 0
    cur_bin_label = bins[cur_bin]

    sess_rows = ""
    for name in ("日盘", "夜盘"):
        s = stats["sess"].get(name) or {}
        if not s.get("n"):
            continue
        sess_rows += (f"<tr><td>{name}</td><td>{s['n']}</td><td>{s['mean']:.2f}</td><td>{s['sd']:.2f}</td>"
                      f"<td>{s['min']:.2f} ~ {s['max']:.2f}</td><td>{s['cur_z']:+.2f}σ</td></tr>")
    daily_rows = ""
    for r in stats["daily"]:
        daily_rows += (f"<tr><td>{r['date']}</td><td>{r['n']}</td><td>{r['mean']:.2f}</td>"
                       f"<td>{r['sd']:.2f}</td><td>{r['min']:.2f} ~ {r['max']:.2f}</td>"
                       f"<td>{r['open']:.2f} → {r['close']:.2f}</td></tr>")
    occ_rows = ""
    for k in ("0.5", "1.0", "1.5", "2.0", "2.5", "3.0"):
        occ_rows += f"<tr><td>±{k}σ</td><td>{stats['occ'][k]:.1f}%</td></tr>"
    ext_html = ""
    for tag, arr, color in (("最高", stats["extremes"]["high"], "#c0392b"),
                            ("最低", stats["extremes"]["low"], "#1e7d4e")):
        items = "、".join(f"{e['t'][5:]} <b style='color:{color}'>{e['v']:.2f}</b>" for e in arr)
        ext_html += f"<div style='margin:3px 0'>{tag}：{items}</div>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>黄金价差 5 分钟微观研究 - {meta['window_end']}</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
  body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#f5f6f8;color:#1a1a2e;margin:0;padding:16px;}}
  .card{{background:#fff;border-radius:12px;padding:16px 20px;margin-bottom:14px;box-shadow:0 1px 4px rgba(0,0,0,.06);}}
  h1{{font-size:19px;margin:0 0 4px;}} h2{{font-size:15px;margin:0 0 8px;}}
  .sub{{color:#888;font-size:12px;margin-bottom:10px;line-height:1.7;}}
  .stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:6px 0;}}
  .stat{{background:#f8f9fb;border-radius:10px;padding:10px 12px;}}
  .stat .k{{font-size:11px;color:#888;}} .stat .v{{font-size:16px;font-weight:600;margin-top:3px;}}
  .pc-table{{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px;}}
  .pc-table td,.pc-table th{{padding:7px 10px;border-bottom:1px solid #eee;text-align:left;}}
  .badge{{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600;}}
  .b-high{{background:#fde8e8;color:#c0392b;}} .b-mid{{background:#fff4dd;color:#b7791f;}} .b-low{{background:#e8f5ee;color:#1e7d4e;}}
  .chart{{width:100%;height:340px;}} .note{{font-size:12px;color:#999;line-height:1.9;}}
  .kv{{display:grid;grid-template-columns:repeat(2,1fr);gap:6px 18px;font-size:13px;}}
  @media (max-width:700px){{.stats{{grid-template-columns:repeat(2,1fr);}} .kv{{grid-template-columns:1fr;}}}}
</style></head><body>

<div class="card">
  <h1>🔬 黄金境内外价差 · 5 分钟微观研究 <span style="font-weight:400;font-size:13px;color:#666">{meta['ts']}</span></h1>
  <div class="sub">
    窗口：<b>{meta['window_start']} ~ {meta['window_end']}</b>（{meta['days']} 个交易日，{meta['bars']} 根境内样本）·
    口径：<b>5 分钟同刻对齐</b><br>
    境内 沪金主力 au0（元/克）｜境外 COMEX GC ÷ 31.1035 × 汇率｜汇率 <b>离岸 USDCNH</b>（在岸 5 分钟无免费历史，以离岸替代）<br>
    样本仅含境内期货可交易时段：日盘 09:00-10:15 / 10:30-11:30 / 13:30-15:00，夜盘 21:00-02:30
  </div>
  <div class="stats">
    <div class="stat"><div class="k">当前价差</div><div class="v">{cur['spread']:+.2f} 元/克</div></div>
    <div class="stat"><div class="k">样本均值</div><div class="v">{d['mean']:+.2f} 元/克</div></div>
    <div class="stat"><div class="k">标准差 σ</div><div class="v">{d['sd']:.2f} 元/克</div></div>
    <div class="stat"><div class="k">当前 σ 位置</div><div class="v"><span class="badge {band_cls}">{z:+.2f}σ · {band_txt}</span></div></div>
    <div class="stat"><div class="k">σ 单位价格</div><div class="v">{d['sd'] / meta['px'] * 1000:.2f} ‰</div></div>
    <div class="stat"><div class="k">当前分位</div><div class="v">{stats['rank']:.1f}%</div></div>
    <div class="stat"><div class="k">区间</div><div class="v" style="font-size:14px">{d['min']:.2f} ~ {d['max']:.2f}</div></div>
    <div class="stat"><div class="k">样本数</div><div class="v">{d['n']} 根</div></div>
  </div>
</div>

{realtime_card(meta.get('rt'), stats['cur'])}

<div class="card">
  <h2>📈 5 分钟价差与均值 ± 1σ / ± 2σ 带</h2>
  <div class="sub">红线为价差逐 5 分钟收盘；虚线为均值与 σ 带。突破 ±2σ 即进入样本内的极端区。</div>
  <div id="c1" class="chart"></div>
</div>

<div class="card">
  <h2>📊 价差分布与当前位置</h2>
  <div class="sub">直方图为 {d['n']} 根样本的价差分布，红线为当前值。</div>
  <div id="c2" class="chart" style="height:300px"></div>
</div>

<div class="card">
  <h2>🕐 盘中时间曲线（按 5 分钟刻度平均）</h2>
  <div class="sub">横轴为境内交易时段的时间刻度，纵轴为该时刻全窗口平均价差——用于观察日内系统性偏高/偏低的时点。</div>
  <div id="c3" class="chart" style="height:300px"></div>
</div>

<div class="card">
  <h2>🧭 执行参考</h2>
  <div class="kv">
    <div>1σ 上沿：<b>{d['mean'] + d['sd']:+.2f}</b> 元/克</div>
    <div>1σ 下沿：<b>{d['mean'] - d['sd']:+.2f}</b> 元/克</div>
    <div>2σ 上沿：<b>{d['mean'] + 2 * d['sd']:+.2f}</b> 元/克</div>
    <div>2σ 下沿：<b>{d['mean'] - 2 * d['sd']:+.2f}</b> 元/克</div>
    <div>均值回归半程（1σ）：<b>{(d['mean'] + cur['spread']) / 2:+.2f}</b> 元/克</div>
    <div>当前距均值：<b>{cur['spread'] - d['mean']:+.2f}</b> 元/克</div>
  </div>
  <div class="note" style="margin-top:10px">{ext_html}</div>
  <div class="note" style="margin-top:8px"><b>解读</b>：当前价差位于样本均值 {z:+.2f}σ（{band_txt}）。σ 分布占用见右表——正态分布下 ±1σ 应覆盖约 68%，实际覆盖 {stats['occ']['1.0']:.0f}%，说明价差{'偏肥尾（极端时刻更频繁）' if stats['occ']['1.0'] < 68 else '接近正态'}。</div>
</div>

{signal_card(meta.get('signal'))}

<div class="card">
  <h2>📋 分层统计</h2>
  <table class="pc-table">
    <tr><th>时段</th><th>样本</th><th>均值</th><th>σ</th><th>区间</th><th>最新σ位置</th></tr>
    {sess_rows}
  </table>
  <table class="pc-table" style="margin-top:14px">
    <tr><th>交易日</th><th>样本</th><th>均值</th><th>σ</th><th>区间</th><th>开→收</th></tr>
    {daily_rows}
  </table>
  <div class="kv" style="margin-top:14px">
    <div>σ 分布占用</div><div></div>
  </div>
  <table class="pc-table" style="width:60%;margin-top:4px">
    <tr><th>带宽</th><th>样本占比</th></tr>
    {occ_rows}
  </table>
</div>

<div class="card"><div class="note">{note}</div></div>

<script>{CHART_JS}
spreadChart('c1', {json.dumps({"t": [r["t"][5:] for r in meta["rows"]], "s": [round(r["spread"], 3) for r in meta["rows"]], "mean": d["mean"], "sd": d["sd"]})});
histChart('c2', {json.dumps({"bins": bins, "counts": counts, "curBin": cur_bin_label})});
profileChart('c3', {json.dumps({"t": [p["t"] for p in stats["profile"]], "mean": [round(p["mean"], 3) for p in stats["profile"]], "allMean": d["mean"]})});
</script></body></html>"""


# ---------------------------------------------------------------- 归档
def append_archive(au_bars, gc_bars, fx_bars):
    path = os.path.join(DATA_DIR, "archive_5m.jsonl")
    seen = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                    seen.add((o["k"], o["t"]))
                except Exception:
                    pass
    new = 0
    with open(path, "a", encoding="utf-8") as f:
        for key, bars in (("au", au_bars), ("gc", gc_bars), ("fx", fx_bars)):
            for b in bars:
                k = (key, b["t"])
                if k in seen:
                    continue
                f.write(json.dumps({"k": key, "t": b["t"], "c": b["c"]}, ensure_ascii=False) + "\n")
                seen.add(k)
                new += 1
    return new


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    # 非沪金交易时段 → 数据暂停更新（FORCE_RUN=1 可强制）
    if os.environ.get("FORCE_RUN") != "1" and not in_session():
        print("OUT_OF_SESSION 沪金非交易时段，数据暂停更新")
        return 0
    au = fetch_au0_5m()
    gc = fetch_gc_5m()
    fx = fetch_cnh_5m()
    print(f"raw bars: au={len(au)} gc={len(gc)} fx={len(fx)}")
    rows = align(au, gc, fx)
    if not rows:
        print("ERROR: 无对齐样本")
        return 1
    added = append_archive(au, gc, fx)
    stats = build_stats(rows)
    d = stats["desc"]
    window_start, window_end = rows[0]["date"], rows[-1]["date"]
    days = len({r["date"] for r in rows})
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    note = (
        "数据来源：沪金主力连续 au0（新浪财经 5 分钟，1023 根上限）、COMEX 黄金 GC 与离岸 USDCNH"
        "（东方财富 5 分钟，约 1400 根上限）。<b>窗口仅 "
        f"{days} 个交易日</b>——免费数据源对分钟级历史的保留期决定了这一点：外盘 5 分钟只有约 5 个交易日，"
        "境内 5 分钟约 10 个交易日、30 分钟约 54 个交易日，三腿必须同刻对齐，故取最短腿。"
        "汇率腿以离岸 CNH 代替在岸（在岸无免费分钟历史），离岸与在岸日常偏差通常 &lt;0.3%，对价差的影响"
        f"约 {d['sd'] * 0.03:.2f} 元/克量级，远小于价差自身的 σ。"
        "价差为正 = 境内溢价（境内更贵），为负 = 境内折价。"
        f"本次新归档 5 分钟样本 {added} 条（data5m/archive_5m.jsonl），逐日沉淀后报告窗口将自动延长。"
    )
    meta = {"rows": rows, "window_start": window_start, "window_end": window_end,
            "days": days, "bars": len(rows), "ts": ts, "px": stats["cur"]["au"]}

    # 实时三腿 + ±1.5σ 开仓提示
    rt = fetch_realtime_3leg()
    meta["rt"] = rt
    up15 = d["mean"] + 1.5 * d["sd"]
    lo15 = d["mean"] - 1.5 * d["sd"]
    cur_sp = stats["cur"]["spread"]
    if stats["z"] >= 1.5:
        sig_state = "short_zone"
    elif stats["z"] <= -1.5:
        sig_state = "long_zone"
    else:
        sig_state = "neutral"
    sig = {"state": sig_state, "z": round(stats["z"], 2),
           "upper_15": round(up15, 2), "lower_15": round(lo15, 2),
           "cur": round(cur_sp, 2),
           "dist_upper": round(up15 - cur_sp, 2), "dist_lower": round(cur_sp - lo15, 2)}
    meta["signal"] = sig

    html = build_html(stats, meta, note)
    day = datetime.date.today().isoformat()
    html_path = os.path.join(OUT_DIR, "gold_spread_5min.html")
    open(html_path, "w", encoding="utf-8").write(html)
    open(os.path.join(OUT_DIR, f"gold_spread_5min_{day}.html"), "w", encoding="utf-8").write(html)

    payload = {
        "date": day, "ts": ts, "window": {"start": window_start, "end": window_end,
                                          "days": days, "bars": len(rows)},
        "unit": "元/克", "curve": "离岸USDCNH",
        "cur": {"t": stats["cur"]["t"], "spread": round(stats["cur"]["spread"], 3),
                "prem": round(stats["cur"]["prem"], 3), "au": stats["cur"]["au"],
                "gc": stats["cur"]["gc"], "fx": stats["cur"]["fx"], "conv": stats["cur"]["conv"]},
        "mean": round(d["mean"], 3), "sd": round(d["sd"], 3),
        "median": round(d["median"], 3), "min": round(d["min"], 3), "max": round(d["max"], 3),
        "z": round(stats["z"], 2), "rank": round(stats["rank"], 1),
        "occ": {k: round(v, 1) for k, v in stats["occ"].items()},
        "sess": {k: {"n": v["n"], "mean": round(v["mean"], 3), "sd": round(v["sd"], 3),
                     "min": round(v["min"], 3), "max": round(v["max"], 3), "cur_z": v.get("cur_z")}
                 for k, v in stats["sess"].items()},
        "daily": [{"date": r["date"], "n": r["n"], "mean": round(r["mean"], 3), "sd": round(r["sd"], 3),
                   "min": round(r["min"], 3), "max": round(r["max"], 3),
                   "open": round(r["open"], 3), "close": round(r["close"], 3)} for r in stats["daily"]],
        "archived_new": added,
        "signal": sig,
        "realtime": {"au": rt.get("au"), "gc": rt.get("gc"), "fx": rt.get("fx"),
                     "fx_time": rt.get("fx_time"), "gc_time": rt.get("gc_time")},
    }
    json.dump(payload, open(os.path.join(OUT_DIR, "gold_spread_5min.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"aligned={len(rows)} days={days} mean={d['mean']:.2f} sd={d['sd']:.2f} "
          f"cur={stats['cur']['spread']:+.2f} z={stats['z']:+.2f} rank={stats['rank']:.1f}%")
    print("SIGNAL:", json.dumps(sig, ensure_ascii=False))
    print("REALTIME:", json.dumps({k: v for k, v in rt.items()}, ensure_ascii=False))
    print("saved:", html_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
