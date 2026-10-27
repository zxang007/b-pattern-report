# qqbot 独立行情扫描服务

本服务只做行情扫描、缓存和画图，与同机 QQ 机器人无关，不发送消息。GitHub 原有自动任务继续保留。

## 工作位置

- 服务目录：`/home/ubuntu/market-pattern-scanner/`
- 当前代码：`current/`，链接到经过验证的 `releases/` 版本。
- 配置：`config.json`，只包含数据目录、重试次数和超时，不需要 API 密钥。
- 持久行情及结构记录：`state/scanner.sqlite3`。
- 最近一次自动扫描：`state/latest/`。
- 历史扫描与回测：`state/runs/`，每轮保存版本指纹、日期边界、完成数量、失败信息、全部 CSV 和 SVG 图。

## 自动运行

`market-pattern-scanner.timer` 在北京时间每天 00:00、04:00、08:00、12:00、16:00、20:00 整点触发，每 4 小时一轮。
服务器时间决定已收盘 K 线边界，只扫描已收盘四小时 K 线，不再额外延迟 2 分钟或回退 90 秒。
扫描范围沿用 GitHub 日报：从当前北京时间月份前两个月的十五日 UTC 起，直到最近收盘。
图表和 CSV 展示北京时间。历史回测入口按北京时间日期，结束日期包含当天。

交易对自动从公开 `exchangeInfo` 更新，保留当前交易中的 USDT 永续，沿用原脚本排除倍数面值合约的规则。
历史 K 线用 SQLite 持久保存，只补缺失区间。数据缺口、空数据和异常价格会使该币失败，不算成零命中。
同一版本、同一窗口已成功的币直接续用结果，失败的币在 15 分钟后补跑；任务互斥，避免重叠。

识别仍调用原来的 `find_matches`、市场过滤、排序和去重函数。移除每币两条和总共五百条输出截断。
同一币、同一下杀低点及洗盘起点归组，全部边界候选逐条保留，并分别生成 SVG 图。
报表画图只读取本次缓存与 CSV，不重复请求行情。

结构变化记录包含：部署时基线、首次新增、边界更新、再次出现、不在当前窗口。
首次观察时间是服务器实际观察时间；历史形态在首次部署时标记为基线。
不在当前窗口可能由时间滚动或候选冲突筛选导致，不等同于价格触发失效。
原规则含离场后的确认条件，因此图形边界时间与实际可识别时间需要分别查看。

## 检查与查看

以下命令均在本机通过 SSH 控制服务器，不在本机拉取行情：

```sh
ssh qqbot 'systemctl status market-pattern-scanner.timer market-pattern-scanner.service --no-pager'
ssh qqbot 'cat /home/ubuntu/market-pattern-scanner/state/status.json'
ssh qqbot 'journalctl -u market-pattern-scanner.service -n 30 --no-pager'
```

报告地址为 `https://129.226.223.91:80/`，原 `http://129.226.223.91/` 自动跳转。
首页优先展示最新一轮完整扫描；新一轮尚未完成或存在失败时，继续展示上一轮完整结果，进度和部分结果可在运行状态及历史页查看。
结构按洗盘结束时间从新到旧排列，同时间按洗盘开始时间、交易对名称倒序，与 GitHub 原报告一致。
每组标题展示洗盘结束时间；点击币名复制完整交易对，例如 `BTCUSDT`。手机未允许自动复制时提供长按复制入口。
云安全组当前未放行 443，入口在已开放的 80 端口区分普通 HTTP 与标准 TLS，分别转给本机 18765 与 443。
使用可信 IP 证书，不跳过证书验证。证书每天检查两次并自动续期。
`/history` 可查看历史回测和扫描，`/status.json` 显示最新任务状态。
公开传输的报表、SVG、CSV、摘要和历史记录均为 AES-256-GCM 密文，浏览器用 PBKDF2-SHA256（210000 轮）派生密钥解锁。
派生密钥保存在 systemd 加密凭据中，由系统仅在报告进程运行时解锁。源码、GitHub 和报表不包含密码或解锁密钥。
不提供目录列表、数据库、输入缓存或配置文件。
首次成功解锁后，当前浏览器使用不可导出的 Web Crypto AES-GCM 密钥加密报告解锁材料，并存入 IndexedDB。
后续打开自动解锁，不存储明文密码或明文派生密钥。清除网站数据、使用其他浏览器/设备或点击“忘记本设备”后，需要重新输入密码。
密码派生和解密在后台 Worker 执行；HTTPS 下优先原生 Web Crypto，避免手机卡住。网络和计算都有超时提示。
服务与 QQ 机器人独立，网页不能启动扫描或执行服务器命令。

初次设置报告密钥用 `ssh -t qqbot 'python3 <新版本绝对路径>/setup_report_key.py'`，在终端无回显输入密码。
该工具将派生密钥交给 `systemd-creds` 加密保存到 `/etc/credstore.encrypted/market-report.key`。
密码不通过命令参数、环境文件、源码或部署包传递。报告服务通过 `LoadCredentialEncrypted` 在运行时读取密钥。
证书私钥及 ACME 账户数据仅在 `/run` 内存文件系统中解锁，持久副本为系统加密凭据。
系统凭据由主机受控的 root 密钥解锁，具有该主机 root 权限的管理员仍可解锁；这不代表磁盘整体加密。
首次部署证书使用专用 `acme-venv` 中的 Certbot 5.4.0；`renew_report_certificate.py` 可重复运行，不重复申请尚未到期的证书。

## 指定历史回测

```sh
ssh qqbot 'python3 /home/ubuntu/market-pattern-scanner/current/scanner_service.py run --config /home/ubuntu/market-pattern-scanner/config.json --symbols TUTUSDT --start-date 2026-07-01 --end-date 2026-08-31'
```

该命令只新增历史回测结果，不更新自动扫描的 `latest`、结构观察状态和事件历史。
服务与回测共享进程锁，已有任务运行时跳过重复启动；先检查服务状态，再安排长回测。

## 更新流程

1. 在公司电脑编辑 `github_pages_pattern_task/` 中的扫描/画图源码或本目录服务源码；本机不运行行情脚本。
2. 运行 `python3 qqbot_tools/deploy_service.py --release <新版本名>`，把源文件、指纹和测试上传新目录，在 qqbot 执行服务回归检查。
3. 使用新版本的绝对路径在 qqbot 执行历史样本回测。核对完整 CSV、图形数量、数据覆盖和变化原因。
4. 验证通过后用 `python3 qqbot_tools/deploy_service.py --release <新版本名> --activate` 原子切换版本。激活时若已有扫描在执行会拒绝切换，避免中途混用版本。
5. 将已验证源码和部署说明上传 GitHub 的 `server_scanner/` 目录备份。不要上传 SQLite、行情数据、运行日志和凭据。

扫描使用 Python 标准库；报告加密使用服务器已有的 cryptography。浏览器加密库从 `web_crypto/` 的固定版本依赖打包，保留依赖锁文件。每币识别放在受超时约束的子进程中；
单次任务最多使用约一个 CPU 核，内存上限 950 MiB，避免占用同机其他服务的全部资源。
旧版本目录和迁移前结果保留；回退时将 `current` 链接切回已验证版本，再启动服务。
