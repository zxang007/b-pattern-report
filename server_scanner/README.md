# 独立行情扫描与画图服务

服务运行在 ssh qqbot 服务器，与 QQ 机器人无关，不发送消息。公司电脑只编辑和离线检查，真实行情测试及画图全部在 qqbot 执行，验证后上传本仓库备份。GitHub 原有自动任务保持启用。

操作说明见 [service/README.md](service/README.md)。在本仓库使用 `python3 server_scanner/deploy_service.py --release <新版本名>` 上传，经远端真实行情验证后加 `--activate` 激活。原工作区工具位于 `qqbot_tools/deploy_service.py`。

网页地址为 https://129.226.223.91:80/ ，报表、图形及候选数据以 AES-256-GCM 密文提供，使用用户设置的报告密码解锁。密码和派生密钥不在源码或仓库中。派生密钥使用服务器 systemd 加密凭据。

迁移验证：TUT 2026 年 7–8 月的 8 条候选与旧结果逐字段一致，服务器生成全部 8 张 SVG。缓存复跑跳过已完成识别，不重复拉取历史 K 线。
