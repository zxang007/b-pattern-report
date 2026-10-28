"""只保留最新完整联合扫描、它的四小时来源和已验证的五倍年度扫描。"""
from contextlib import ExitStack
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import shutil


RUN_NAME = re.compile(r"(?:live|backtest)-[0-9a-f]{20}")


def read_summary(folder):
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("扫描目录缺失或为符号链接")
    data = json.loads((folder / "summary.json").read_text())
    if data.get("scope") != folder.name:
        raise ValueError("扫描标识与目录不一致")
    return data


def prune_scans(base):
    base = Path(base).resolve()
    root = base / "state"
    runs = root / "runs"
    if runs.is_symlink() or not runs.is_dir():
        raise ValueError("扫描根目录异常，停止清理")
    with ExitStack() as stack:
        # 正在扫描、抓取行情或生成联合报告时，不删除任何批次。
        for name in ("scan.lock", "fifteen_m_fetch.lock", "platform_market.lock"):
            lock = stack.enter_context((root / name).open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"skipped": "扫描任务正在运行", "removed": []}
        entries = sorted(p for p in runs.iterdir() if RUN_NAME.fullmatch(p.name))
        confirmed = []
        for folder in entries:
            if folder.is_symlink() or not folder.is_dir():
                continue
            try:
                data = read_summary(folder)
            except (OSError, ValueError):
                continue
            if (folder.name.startswith("live-") and data.get("status") == "complete"
                    and data.get("stage") == "4h_and_15m" and data.get("validation_ok")
                    and (folder / "index.html").is_file()):
                confirmed.append((data["end_bj_exclusive"], data.get("updated_at_bj", ""), folder, data))
        if not confirmed:
            raise ValueError("没有完整且通过校验的最新联合扫描，停止清理")
        _, _, latest, latest_data = max(confirmed, key=lambda item: item[:2])
        source_name = latest_data["source_scope"]
        proof = json.loads((base / "validation/year-fivefold/completed.json").read_text())
        annual_name = proof["scope"]
        if not (RUN_NAME.fullmatch(source_name) and RUN_NAME.fullmatch(annual_name)):
            raise ValueError("需要保留的扫描标识无效")
        source = runs / source_name
        annual = runs / annual_name
        source_data = read_summary(source)
        annual_data = read_summary(annual)
        # 缺失来源、年度结果或有效页面时，宁可停止也不猜测保留目标。
        if not (source_name.startswith("live-") and source_data.get("status") == "complete"
                and (source / "by_symbol").is_dir()
                and source_data.get("end_bj_exclusive") == latest_data["end_bj_exclusive"]):
            raise ValueError("最新扫描的四小时来源不完整")
        if not (annual_name.startswith("backtest-") and proof.get("status") == "complete"
                and proof.get("validation_ok") and annual_data.get("status") == "complete"
                and annual_data.get("validation_ok") and annual_data.get("stage") == "annual_outcome"
                and (annual / "index.html").is_file()):
            raise ValueError("五倍年度扫描未通过完整性检查")
        keep = {latest.name, source_name, annual_name}
        candidates = [p for p in entries if p.name not in keep and p.is_dir() and not p.is_symlink()]
        # 新四小时扫描尚未完成确认时，等待下一次成功流程再清理。
        for folder in candidates:
            try:
                data = read_summary(folder)
            except (OSError, ValueError):
                continue
            if (folder.name.startswith("live-") and data.get("status") == "complete"
                    and (folder / "by_symbol").is_dir()
                    and data.get("end_bj_exclusive", "") > latest_data["end_bj_exclusive"]):
                return {"skipped": "等待新一轮联合扫描完成", "removed": []}
        link = root / (".latest-retention-" + str(os.getpid()))
        link.symlink_to(Path("runs") / latest.name)
        link.replace(root / "latest")
        before = os.statvfs(base)
        removed = []
        for folder in candidates:
            if folder.parent != runs or folder.is_symlink():
                raise ValueError("待删除目录发生变化，停止清理")
            shutil.rmtree(folder)
            removed.append(folder.name)
        after = os.statvfs(base)
        result = {
            "at": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
            "retained": sorted(keep), "removed": removed,
            "available_bytes": after.f_bavail * after.f_frsize,
            "released_bytes": max(0, after.f_bavail * after.f_frsize - before.f_bavail * before.f_frsize),
        }
        audit = root / "retention-last.json"
        pending = root / "retention-last.json.pending"
        pending.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        pending.replace(audit)
        return result


if __name__ == "__main__":
    print(json.dumps(prune_scans("/home/ubuntu/market-pattern-scanner"), ensure_ascii=False), flush=True)
