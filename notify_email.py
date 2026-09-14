# -*- coding: utf-8 -*-
"""Email the GLD monitor report as a styled HTML mail (mobile-friendly).

Env vars:
  SMTP_HOST (default smtp.qq.com)  SMTP_PORT (default 465)
  SMTP_USER / SMTP_PASS / MAIL_TO
  OUT_DIR (default "output")

Skips silently if SMTP_USER/SMTP_PASS not set.
Maintains state.json (previous spot) for day-over-day change.
"""
import json
import os
import smtplib
import sys
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

OUT_DIR = os.environ.get("OUT_DIR", "output")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

RED = "#c0392b"
GREEN = "#1e8449"
BG = "#f5f6f7"
CARD = "#ffffff"
INK = "#1c2733"
MUTED = "#7a8691"


def esc(x):
    return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_val(x, fallback="—"):
    """Render a scalar safely; None/NaN -> em dash."""
    if x is None:
        return fallback
    try:
        f = float(x)
    except (TypeError, ValueError):
        return fallback
    if f != f:  # NaN
        return fallback
    return f"{f:g}"


def fmt_num(x, fallback="—"):
    """Integer with thousands separator; None/NaN -> em dash."""
    if x is None:
        return fallback
    try:
        f = float(x)
    except (TypeError, ValueError):
        return fallback
    if f != f:
        return fallback
    return f"{int(f):,}"


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(spot):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"spot": spot}, f)


def build_html(d, prev_spot):
    spot = d["spot"]
    gen = d["generated_at"][:16]
    cb, pb = d["bands"]["call_band"], d["bands"]["put_band"]

    if prev_spot:
        delta = spot - prev_spot
        pct = delta / prev_spot * 100
        cls = "chg-up" if delta >= 0 else "chg-dn"
        sign = "+" if delta >= 0 else ""
        chg_html = (f'<span class="{cls}">{sign}{delta:.2f}（{sign}{pct:.2f}%）</span>'
                    f' <span style="font-size:12px;color:{MUTED}">vs 前值 {prev_spot:.2f}</span>')
    else:
        chg_html = f'<span style="font-size:12px;color:{MUTED}">首日无对比</span>'

    def wall_chips(walls, color):
        return "".join(
            f'<span style="display:inline-block;margin:3px 4px 3px 0;padding:5px 10px;'
            f'border-radius:8px;font-size:13px;font-weight:600;background:{color}14;'
            f'color:{color};border:1px solid {color}44">'
            f'{fmt_num(s)} <span style="font-weight:400;font-size:11px">{fmt_num(o)} 张</span></span>'
            for s, o in walls.items())

    def rec_table(recs, first_strike):
        head = ("<tr><th>行权价</th><th>%OTM</th><th>中间价</th><th>Bid~Ask</th>"
                "<th>OI</th><th>成交量</th><th>估Δ</th><th>到期</th></tr>")
        rows = []
        for r in recs:
            cls = ' bgcolor="#fdf3e7"' if int(r["strike"] or 0) == first_strike else ""
            rows.append(
                f"<tr{cls}><td><b>{fmt_num(r['strike'])}</b></td>"
                f"<td>+{fmt_val(r['pct_otm'])}%</td><td>{fmt_val(r['mid'])}</td>"
                f"<td>{fmt_val(r['bid'])}~{fmt_val(r['ask'])}</td>"
                f"<td>{fmt_num(r.get('oi'))}</td><td>{fmt_num(r.get('volume'))}</td>"
                f"<td>{fmt_val(r['est_delta'])}</td>"
                f"<td>{str(r['expiry'])[5:]}·{fmt_val(r['dte'])}天</td></tr>")
        return f"<table>{head}{''.join(rows)}</table>"

    call_recs = d["recs"]["call"]
    put_recs = d["recs"]["put"]
    call_first = int(call_recs[0]["strike"] or 0) if call_recs else -1
    put_first = int(put_recs[0]["strike"] or 0) if put_recs else -1

    return f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background:{BG};font-family:-apple-system,'PingFang SC','Helvetica Neue',Arial,sans-serif;color:{INK};">
<div style="max-width:680px;margin:0 auto;padding:16px;">

<div style="background:{CARD};border-radius:12px;padding:20px 22px;margin-bottom:14px;">
  <div style="font-size:20px;font-weight:700;">📊 GLD 期权链日报</div>
  <div style="font-size:12px;color:{MUTED};margin-top:4px;">{esc(gen)}（北京时间）· 数据源 Nasdaq（延迟15分钟）· 覆盖 {len(d['expirations'])} 个到期日</div>
  <div style="margin-top:12px;"><span style="font-size:34px;font-weight:800;">${spot:.2f}</span>&nbsp;&nbsp;{chg_html}</div>
</div>

<div style="background:{CARD};border-radius:12px;padding:20px 22px;margin-bottom:14px;">
  <div style="font-size:15px;font-weight:700;border-left:4px solid {RED};padding-left:9px;margin-bottom:10px;">① Call OI 墙（阻力）
    <span style="font-weight:400;color:{MUTED};font-size:12px">　区间 {int(cb[0])}–{int(cb[1])}</span></div>
  {wall_chips(d['bands']['call_walls'], RED)}
  <div style="font-size:15px;font-weight:700;border-left:4px solid {GREEN};padding-left:9px;margin:16px 0 10px;">② Put OI 墙（支撑）
    <span style="font-weight:400;color:{MUTED};font-size:12px">　区间 {int(pb[0])}–{int(pb[1])}</span></div>
  {wall_chips(d['bands']['put_walls'], GREEN)}
  <div style="margin-top:10px;font-size:13px;color:{MUTED};line-height:1.7;">现价距 Call 墙上限 <b style="color:{INK}">+{(cb[1]/spot-1)*100:.1f}%</b>，距 Put 墙下限 <b style="color:{INK}">{(pb[0]/spot-1)*100:.1f}%</b>。卖 Call 避开墙内行权价，卖 Put 避开深度支撑密集区。</div>
</div>

<div style="background:{CARD};border-radius:12px;padding:20px 22px;margin-bottom:14px;">
  <div style="font-size:15px;font-weight:700;border-left:4px solid {RED};padding-left:9px;margin-bottom:10px;">③ Sell Call 推荐（30-45 DTE · 5-8% OTM）</div>
  {rec_table(call_recs, call_first)}
  <div style="margin-top:8px;font-size:12px;color:{MUTED};line-height:1.7;">🥇 首选 <b style="color:{INK}">{call_first}</b>（高亮行）：premium / 流动性综合最优；成交前先看盘口深度。</div>
</div>

<div style="background:{CARD};border-radius:12px;padding:20px 22px;margin-bottom:14px;">
  <div style="font-size:15px;font-weight:700;border-left:4px solid {GREEN};padding-left:9px;margin-bottom:10px;">④ Sell Put 推荐（30-45 DTE · 8-12% OTM）</div>
  {rec_table(put_recs, put_first)}
  <div style="margin-top:8px;font-size:12px;color:{MUTED};line-height:1.7;">🥇 首选 <b style="color:{INK}">{put_first}</b>（高亮行）：OTM 最深、Δ 最小，最保守。</div>
</div>

<div style="background:{CARD};border-radius:12px;padding:20px 22px;margin-bottom:14px;">
  <div style="font-size:15px;font-weight:700;border-left:4px solid #8e44ad;padding-left:9px;margin-bottom:10px;">⑤ OI 分布全景图</div>
  <img src="cid:oichart" alt="GLD OI distribution" style="width:100%;height:auto;border-radius:8px;" />
</div>

<div style="background:{CARD};border-radius:12px;padding:16px 22px;margin-bottom:14px;">
  <div style="font-size:12px;color:{MUTED};line-height:1.8;">⚠️ 卖方理论亏损无限，单腿仓位不超过组合 5%；FOMC / 央行决议 / 地缘事件前 24h 建议暂停开新仓。估 Δ 为 BSM 近似（σ=0.22），非实盘希腊字母。<br/>历史报告归档：<a href="https://github.com/Aresssc/gld-option-monitor/tree/main/reports" style="color:#2980b9;">GitHub reports/</a></div>
</div>

<div style="font-size:11px;color:{MUTED};text-align:center;margin:6px 0 20px;">由 gld_option_monitor.py 在 GitHub Actions 自动生成</div>
</div></body></html>"""


def plain_fallback(d):
    call_first = fmt_num(d["recs"]["call"][0]["strike"]) if d["recs"]["call"] else "-"
    put_first = fmt_num(d["recs"]["put"][0]["strike"]) if d["recs"]["put"] else "-"
    return (f"GLD {d['spot']:.2f} @ {d['generated_at'][:16]}\n"
            f"Call 墙 {d['bands']['call_band']} / Put 墙 {d['bands']['put_band']}\n"
            f"Sell Call 首选 {call_first} / Sell Put 首选 {put_first}\n"
            "（本邮件为 HTML 版降级文本，请用支持 HTML 的客户端查看）")


def main():
    user = os.environ.get("SMTP_USER", "")
    pwd = os.environ.get("SMTP_PASS", "")
    if not user or not pwd:
        print("[mail] SMTP_USER/SMTP_PASS not set, skip sending.")
        return
    to = os.environ.get("MAIL_TO", user)
    host = os.environ.get("SMTP_HOST", "smtp.qq.com")
    port = int(os.environ.get("SMTP_PORT", "465"))

    with open(f"{OUT_DIR}/gld_monitor_data.json", encoding="utf-8") as f:
        d = json.load(f)

    prev = load_state().get("spot")
    try:
        html = build_html(d, prev)
    except Exception as e:
        # never lose a day's report because of a rendering bug
        print(f"[mail][warn] HTML build failed ({type(e).__name__}: {e}), falling back to plain text")
        html = None
    save_state(d["spot"])

    subj_date = d.get("generated_at", "")[:10]
    subject = f"📊 GLD 期权日报 {subj_date} — spot {d['spot']:.2f}"

    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = formataddr(("GLD Monitor", user))
    msg["To"] = to

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(plain_fallback(d), "plain", "utf-8"))
    if html:
        alt.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(alt)

    png_path = f"{OUT_DIR}/gld_monitor_oi.png"
    if os.path.exists(png_path):
        # inline copy for the embedded chart (CID)
        with open(png_path, "rb") as f:
            rel = MIMEApplication(f.read(), _subtype="png")
        rel.add_header("Content-ID", "<oichart>")
        rel.add_header("Content-Disposition", "inline", filename="oi.png")
        msg.attach(rel)

    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    try:
        server.login(user, pwd)
        server.sendmail(user, [to], msg.as_string())
        print(f"[mail] sent HTML report to {to}: {subject}")
    finally:
        server.quit()


if __name__ == "__main__":
    sys.exit(main())
