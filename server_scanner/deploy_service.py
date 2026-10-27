#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import subprocess
import tarfile


CORE_FILES = (
    "four_h_segment_arc_scan.py", "scan_four_h_segment_arc_market.py",
    "market_archive_pattern_scan.py", "screen_aria_4h_pattern.py",
    "draw_four_h_segment_arc_matches_svg.py", "draw_market_current_logic_from_csv.py",
    "verified_examples.csv",
    "new_examples.csv",
)
SERVICE_FILES = (
    "scanner_service.py", "report_server.py", "report_front.py", "report_crypto.js", "report_zoom.js", "test_report_zoom.js", "setup_report_key.py", "renew_report_certificate.py", "test_scanner_service.py", "regression_reclaims.py", "config.json", "README.md",
    "market-pattern-scanner.service", "market-pattern-scanner.timer", "market-pattern-viewer.service",
    "market-pattern-http.service", "market-report-certificate.service", "market-report-certificate.timer",
    "market-pattern-front.service",
)
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "qqbot"]


def ssh(command: str, payload: bytes | None = None) -> None:
    subprocess.run([*SSH, command], input=payload, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="只通过 SSH 部署行情服务，在 qqbot 上执行测试；本机不请求行情。")
    parser.add_argument("--release", required=True)
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{3,79}", args.release):
        parser.error("版本名只能包含小写字母、数字、连字符，且以字母开头。")
    root = Path(__file__).resolve().parent.parent
    core = root / "github_pages_pattern_task" if (root / "github_pages_pattern_task").is_dir() else root
    service = Path(__file__).resolve().parent / "service"
    files = {name: (core / name).read_bytes() for name in CORE_FILES}
    files.update({name: (service / name).read_bytes() for name in SERVICE_FILES})
    manifest = {"release": args.release, "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    files["release.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        for name, data in files.items():
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(data), 0o600
            archive.addfile(item, io.BytesIO(data))
    destination = "/home/ubuntu/market-pattern-scanner/releases/" + args.release
    probe = "test ! -e " + shlex.quote(destination)
    exists = subprocess.run([*SSH, probe]).returncode != 0
    if not exists:
        ssh("mkdir -p " + shlex.quote(destination) + " && tar xzf - -C " + shlex.quote(destination), payload.getvalue())
    # 已存在的发布目录只校验，不覆盖，防止运行中的版本被逐文件改写。
    verify = """
import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1]); expected=json.loads(sys.stdin.read())
for name,value in expected['files'].items():
    if hashlib.sha256((root/name).read_bytes()).hexdigest()!=value:
        raise SystemExit('发布目录与本机代码不同，请使用新版本名: '+name)
print('RELEASE_VERIFIED',root.name)
"""
    ssh(shlex.join(["python3", "-c", verify, destination]), json.dumps(manifest).encode())
    ssh("cd " + shlex.quote(destination) + " && python3 -m unittest -v test_scanner_service")
    ssh("cd " + shlex.quote(destination) + " && node test_report_zoom.js")
    if not args.activate:
        print("已上传并通过远端检查；请先用此版本在 qqbot 完成真实行情回测，再加 --activate 激活。")
        return
    activate = """
from pathlib import Path
import shutil,subprocess,sys
root=Path('/home/ubuntu/market-pattern-scanner'); release=Path(sys.argv[1])
state=subprocess.run(['systemctl','is-active','market-pattern-scanner.service'],capture_output=True,text=True).stdout.strip()
if state in ('active','activating','reloading'):
    raise SystemExit('扫描正在执行，请完成后再激活，避免混用版本。')
(root/'state').mkdir(exist_ok=True)
config=root/'config.json'
if not config.exists():
    shutil.copyfile(release/'config.json',config);config.chmod(0o600)
for name in ['market-pattern-scanner.service','market-pattern-scanner.timer','market-pattern-viewer.service','market-pattern-http.service','market-report-certificate.service','market-report-certificate.timer','market-pattern-front.service']:
    subprocess.run(['sudo','-n','install','-m','644',str(release/name),'/etc/systemd/system/'+name],check=True)
subprocess.run(['sudo','-n','systemd-analyze','verify','/etc/systemd/system/market-pattern-scanner.service','/etc/systemd/system/market-pattern-scanner.timer','/etc/systemd/system/market-pattern-viewer.service'],check=True)
current=root/'current'
if current.is_symlink():
    previous=root/'previous.pending';previous.unlink(missing_ok=True);previous.symlink_to(current.resolve());previous.replace(root/'previous')
pending=root/'current.pending';pending.unlink(missing_ok=True);pending.symlink_to(release);pending.replace(current)
web=root/'web-current.pending';web.unlink(missing_ok=True);web.symlink_to(release);web.replace(root/'web-current')
subprocess.run(['sudo','-n','systemctl','daemon-reload'],check=True)
subprocess.run(['sudo','-n','systemctl','enable','--now','market-pattern-scanner.timer','market-pattern-viewer.service','market-pattern-http.service','market-report-certificate.timer','market-pattern-front.service'],check=True)
subprocess.run(['sudo','-n','systemctl','restart','market-pattern-viewer.service','market-pattern-http.service','market-pattern-front.service'],check=True)
print('ACTIVATED',release.name)
"""
    ssh(shlex.join(["python3", "-c", activate, destination]))


if __name__ == "__main__":
    main()
