# qqbot 服务器 Codex CLI 自动升级

每天北京时间 05:10 检查 npm 官方源的 `@openai/codex` 最新稳定版。有新版本时，先备份旧 npm 包，再在独立目录安装指定版本，检查版本和帮助命令，通过后原子切换 `/usr/local/bin/codex`。切换后验证失败会恢复原入口；检查、备份或安装失败时不关闭旧会话。

成功验证后，关闭同一全局 CLI 安装来源中低于当前版本的 Codex 会话，先发送 SIGTERM，等待最多 30 秒，再结束尚未退出的同一进程。发送信号前核对 PID 启动时间及可执行文件身份，避免 PID 复用误关。相同版本和更高版本会话继续运行。QQ 与手机端服务使用独立运行时，不属于这个更新器的管理范围。

- 更新器：`/opt/codex-auto-update/updater-current/`，指向经过远端测试的 `updater-releases/` 目录。
- Codex 包：`/opt/codex-auto-update/packages/`。
- 旧包备份：`/var/backups/codex-auto-update/`，保留最近五份成功备份。
- 原安装目录：`/usr/local/lib/node_modules/@openai/codex/`，首次接管后继续保留。
- 管理目录保留当前安装及五个历史安装；只清理本工具带标记的历史目录。
- 最近成功结果：`/var/lib/codex-auto-update/last-result.json`；失败详情查看系统日志。
- 定时任务使用互斥锁；停机错过检查后由 systemd 补跑。
- 不需要 Codex 登录凭据，不执行对话、行情请求或消息发送。

维护命令：

```sh
ssh qqbot 'systemctl list-timers codex-auto-update.timer --no-pager'
ssh qqbot 'sudo systemctl start codex-auto-update.service'
ssh qqbot 'sudo journalctl -u codex-auto-update.service -n 40 --no-pager'
ssh qqbot 'sudo python3 /opt/codex-auto-update/updater-current/updater.py --check-only'
ssh qqbot 'cat /var/lib/codex-auto-update/last-result.json; codex --version'
```

首次接管使用 `--force-reinstall`，把当前稳定版安装到独立目录，实际验证备份、安装和入口切换链路。每日任务不传此参数，版本未变时仅验证入口并检查残留旧会话。

更新代码顺序为：本机静态检查，上传新更新器版本目录，在 qqbot 运行 `python3 -m unittest -v test_updater.py` 和 `systemd-analyze verify`，验证通过后原子切换 `updater-current`，最后备份源码到 GitHub 的 `server_tools/codex_auto_update/`。不改仓库已有自动任务。

回退 CLI 时，可将 `/usr/local/bin/codex` 原子改回保留的旧版 `bin/codex`；回退更新器时，将 `updater-current` 原子改回旧更新器目录。备份归档只包含程序 npm 包，不包含用户配置和会话材料。
