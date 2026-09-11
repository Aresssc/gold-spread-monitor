# gld-cloud — GLD 期权链监控（GitHub Actions 云端版）

每天北京时间 21:30（周一~五，美东盘前）在 GitHub 免费云服务器上跑
`gld_option_monitor.py`，邮件推送日报 + 把快照 commit 进 `reports/`。

## 文件

| 文件 | 作用 |
|---|---|
| `gld_option_monitor.py` | 监控主脚本（抓 Nasdaq 期权链 → OI 墙 → 卖方推荐 → 图/表） |
| `notify_email.py` | SMTP 邮件推送（正文=报告，附件=OI 分布图） |
| `.github/workflows/daily.yml` | 定时 workflow（cron UTC 13:30 周一~五） |
| `reports/` | 每日快照自动归档（历史数据直接在 GitHub 上翻） |

## 一次性配置（Secrets）

在 GitHub 仓库 **Settings → Secrets and variables → Actions** 里加：

| Secret | 值 | 说明 |
|---|---|---|
| `SMTP_USER` | 发件邮箱，如 `xxx@qq.com` | |
| `SMTP_PASS` | SMTP 授权码 | QQ邮箱：设置→账号→POP3/SMTP 服务→开启→生成授权码（不是登录密码） |
| `MAIL_TO` | 收件邮箱 | 可以填自己，或手机上好收信的邮箱 |
| `SMTP_HOST` | 可选，默认 `smtp.qq.com` | 用 163 填 `smtp.163.com`，Gmail 填 `smtp.gmail.com` |
| `SMTP_PORT` | 可选，默认 `465` | |

## 手动触发

仓库 **Actions** 页 → 选 *GLD option monitor daily* → **Run workflow**。
改时间：编辑 `daily.yml` 里的 cron（UTC 时间，北京 = UTC+8）。

## 注意

- GitHub 定时任务有 5~15 分钟随机延迟，属正常。
- 仓库 60 天无任何 commit 会被 GitHub 停用定时任务；本 workflow 每天自动 commit，天然不会触发。
- 美股法定假日当天会照跑，数据为上一交易日收盘，邮件里可自行判断。
