#!/usr/bin/env python3
"""每日检查 Codex 稳定版，验证后切换并关闭同一安装来源的旧会话。"""

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time


ROOT = Path("/opt/codex-auto-update")
PACKAGES = ROOT / "packages"
BACKUPS = Path("/var/backups/codex-auto-update")
STATE = Path("/var/lib/codex-auto-update")
COMMAND = Path("/usr/local/bin/codex")
REGISTRY = "https://registry.npmjs.org"
KEEP = 5
VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def log(message):
    print(f"{dt.datetime.now().astimezone().isoformat(timespec='seconds')} {message}", flush=True)


def run(arguments, timeout=30):
    # 不回显子命令的错误正文，避免 npm 环境信息进入持久日志。
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{Path(str(arguments[0])).name} 执行失败，退出码 {result.returncode}")
    return result.stdout.strip()


def version_tuple(value):
    if not VERSION_PATTERN.fullmatch(value):
        raise ValueError(f"不是稳定版版本号：{value!r}")
    return tuple(map(int, value.split(".")))


def installed_version(command=COMMAND):
    output = run([str(command), "--version"])
    prefix = "codex-cli "
    if not output.startswith(prefix):
        raise RuntimeError("Codex 版本输出无法识别")
    version = output[len(prefix):].strip()
    version_tuple(version)
    return version


def verify(command, expected):
    if installed_version(command) != expected:
        raise RuntimeError("安装版本与目标稳定版不一致")
    run([str(command), "--help"])


def replace_link(target):
    # 临时链接和入口位于同一文件系统，替换入口时不会出现半安装状态。
    temporary = COMMAND.with_name(f".codex-auto-update-{os.getpid()}")
    try:
        temporary.symlink_to(target)
        os.replace(temporary, COMMAND)
    finally:
        temporary.unlink(missing_ok=True)


def activate(target, expected):
    if not COMMAND.is_symlink():
        raise RuntimeError("Codex 入口不是符号链接，拒绝覆盖未知安装")
    previous = os.readlink(COMMAND)
    replace_link(str(target))
    try:
        verify(COMMAND, expected)
    except Exception:
        replace_link(previous)
        log("新入口验证失败，已恢复原入口")
        raise


def write_state(result):
    STATE.mkdir(parents=True, exist_ok=True)
    temporary = STATE / ".last-result.json.tmp"
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, STATE / "last-result.json")


def backup_current(version, stamp):
    package = COMMAND.resolve(strict=True).parents[1]
    metadata = json.loads((package / "package.json").read_text())
    if metadata.get("name") != "@openai/codex" or metadata.get("version") != version:
        raise RuntimeError("当前 npm 包身份不匹配，拒绝备份和升级")
    BACKUPS.mkdir(parents=True, exist_ok=True)
    archive = BACKUPS / f"codex-{version}-{stamp}.tar.gz"
    temporary = archive.with_suffix(".gz.partial")
    try:
        run(["/usr/bin/tar", "-czf", str(temporary), "-C", str(package.parent), package.name], 300)
        run(["/usr/bin/tar", "-tzf", str(temporary)], 120)
        os.replace(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    log(f"旧版备份完成：{archive}")
    return str(archive)


def stage(version, stamp):
    PACKAGES.mkdir(parents=True, exist_ok=True)
    target = PACKAGES / f"{version}-{stamp}"
    target.mkdir(mode=0o755)
    try:
        run([
            "/usr/bin/npm", "install", "--global", "--prefix", str(target),
            f"@openai/codex@{version}", f"--registry={REGISTRY}",
            "--cache=/var/cache/codex-auto-update/npm", "--no-audit", "--no-fund",
            "--ignore-scripts",
        ], 900)
        verify(target / "bin/codex", version)
        (target / ".codex-managed.json").write_text(json.dumps({"version": version}) + "\n")
    except Exception:
        shutil.rmtree(target)
        raise
    log(f"新版本目录验证通过：{version}")
    return target / "bin/codex"


def managed_process(executable, cgroup):
    # 手机端及 QQ 服务使用独立运行时，本更新器只管理全局 CLI 安装。
    if any(name in cgroup for name in ("qqbot", "codex-mobile")):
        return False
    path = Path(executable.removesuffix(" (deleted)"))
    return path.is_relative_to("/usr/local/lib/node_modules/@openai/codex") or path.is_relative_to(PACKAGES)


def process_identity(pid):
    proc = Path("/proc") / str(pid)
    stat = (proc / "stat").read_text()
    start_time = stat[stat.rfind(")") + 2:].split()[19]
    executable = (proc / "exe").stat()
    return start_time, executable.st_dev, executable.st_ino


def old_sessions(active_version):
    found = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            if (proc / "comm").read_text().strip() != "codex":
                continue
            identity = process_identity(int(proc.name))
            if not managed_process(os.readlink(proc / "exe"), (proc / "cgroup").read_text()):
                continue
            version = installed_version(proc / "exe")
            if process_identity(int(proc.name)) != identity:
                continue
            if version_tuple(version) < version_tuple(active_version):
                found.append((int(proc.name), identity, version))
        except (FileNotFoundError, ProcessLookupError):
            continue
    return found


def same_process(pid, identity):
    try:
        return process_identity(pid) == identity
    except (FileNotFoundError, ProcessLookupError):
        return False


def close_old_sessions(active_version):
    sessions = old_sessions(active_version)
    for pid, identity, version in sessions:
        if same_process(pid, identity):
            try:
                os.kill(pid, signal.SIGTERM)
                log(f"关闭旧版 CLI 会话：PID={pid}，版本={version}")
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 30
    while any(same_process(pid, identity) for pid, identity, _ in sessions) and time.monotonic() < deadline:
        time.sleep(0.2)
    for pid, identity, _ in sessions:
        if same_process(pid, identity):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 5
    while any(same_process(pid, identity) for pid, identity, _ in sessions) and time.monotonic() < deadline:
        time.sleep(0.2)
    if any(same_process(pid, identity) for pid, identity, _ in sessions):
        raise RuntimeError("旧版 CLI 会话尚未完全退出")
    log(f"旧版 CLI 会话检查完成：{len(sessions)} 个")
    return [pid for pid, _, _ in sessions]


def prune():
    # 只清理本工具创建并带标记的历史目录，始终保留当前版和五个旧版。
    active = COMMAND.resolve()
    history = []
    for directory in PACKAGES.iterdir() if PACKAGES.exists() else ():
        if directory.is_symlink() or not directory.is_dir() or not (directory / ".codex-managed.json").is_file():
            continue
        if not active.is_relative_to(directory):
            history.append(directory)
    for directory in sorted(history, key=lambda item: item.stat().st_mtime, reverse=True)[KEEP:]:
        shutil.rmtree(directory)
    for archive in sorted(BACKUPS.glob("codex-*.tar.gz"), key=lambda item: item.stat().st_mtime, reverse=True)[KEEP:]:
        archive.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true", help="只检查稳定版和旧会话，不切换、不关闭")
    parser.add_argument("--force-reinstall", action="store_true", help="同版本也部署独立目录，用于首次接管及验证")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("需要由 root 系统服务运行")
    with open("/run/lock/codex-auto-update.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("已有更新任务运行，本次跳过")
            return
        current = installed_version()
        latest = json.loads(run([
            "/usr/bin/npm", "view", "@openai/codex", "dist-tags.latest", "--json",
            f"--registry={REGISTRY}", "--cache=/var/cache/codex-auto-update/npm",
        ], 120))
        version_tuple(latest)
        log(f"稳定版检查：当前={current}，最新={latest}")
        if args.check_only:
            log(f"待关闭旧版 CLI 会话：{len(old_sessions(current))} 个")
            return
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        result = {"checked_at": dt.datetime.now().astimezone().isoformat(), "previous_version": current, "latest_version": latest}
        if version_tuple(latest) > version_tuple(current) or (args.force_reinstall and latest == current):
            result["backup"] = backup_current(current, stamp)
            target = stage(latest, stamp)
            activate(target, latest)
            current = latest
            result["status"] = "updated"
            log(f"Codex 入口已原子切换：{current}")
        else:
            verify(COMMAND, current)
            result["status"] = "up_to_date"
            log("当前版本无需升级")
        # 仅在当前入口验证成功后关闭旧版进程，版本回退时也不关闭更高版本进程。
        result["closed_pids"] = close_old_sessions(current)
        result["active_version"] = current
        write_state(result)
        prune()
        log("检查和验证完成")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"更新失败：{error}")
        sys.exit(1)
