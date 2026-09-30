#!/usr/bin/env python3
"""Install, inspect, or remove the macOS LaunchAgent for AI Frontier daily push."""

import argparse
import getpass
import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.guoyongfa.ai-frontier.daily-brief"
PLIST_NAME = f"{LABEL}.plist"


def project_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def parse_time(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError as error:
        raise argparse.ArgumentTypeError("时间格式应为 HH:MM，例如 09:00") from error

    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise argparse.ArgumentTypeError("时间必须在 00:00 到 23:59 之间")

    return hour, minute


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    return launch_agents_dir() / PLIST_NAME


def run_launchctl(args: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", *args],
        text=True,
        capture_output=True,
        check=check,
    )


def bootout_if_loaded() -> None:
    uid = os.getuid()
    run_launchctl(["bootout", f"gui/{uid}", str(plist_path())], check=False)


def install(time_value: tuple[int, int]) -> None:
    hour, minute = time_value
    root = project_dir()
    logs_dir = root / "logs"
    launch_agents_dir().mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    runner = root / "scripts" / "run_daily_brief.sh"

    if not runner.exists():
        raise FileNotFoundError(f"缺少运行脚本：{runner}")

    runner.chmod(0o755)

    plist = {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", str(runner)],
        "WorkingDirectory": str(root),
        "StartCalendarInterval": {
            "Hour": hour,
            "Minute": minute,
        },
        "RunAtLoad": False,
        "StandardOutPath": str(logs_dir / "launchd.out.log"),
        "StandardErrorPath": str(logs_dir / "launchd.err.log"),
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }

    with plist_path().open("wb") as file:
        plistlib.dump(plist, file)

    bootout_if_loaded()
    result = run_launchctl(["bootstrap", f"gui/{os.getuid()}", str(plist_path())])

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    enable = run_launchctl(["enable", f"gui/{os.getuid()}/{LABEL}"])

    if enable.returncode != 0:
        print(enable.stderr.strip() or enable.stdout.strip())

    print(f"已安装每日自动推送任务：{LABEL}")
    print(f"运行时间：每天 {hour:02d}:{minute:02d}")
    print(f"配置文件：{plist_path()}")
    print(f"运行日志：{logs_dir / 'daily_push.log'}")


def uninstall() -> None:
    bootout_if_loaded()

    if plist_path().exists():
        plist_path().unlink()

    print(f"已移除每日自动推送任务：{LABEL}")


def status() -> None:
    print(f"任务名称：{LABEL}")
    print(f"配置文件：{plist_path()}")
    print(f"配置存在：{'是' if plist_path().exists() else '否'}")

    result = run_launchctl(["print", f"gui/{os.getuid()}/{LABEL}"])

    if result.returncode == 0:
        print("加载状态：已加载")
    else:
        print("加载状态：未加载")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage AI Frontier daily Feishu push LaunchAgent."
    )
    parser.add_argument(
        "action",
        choices=["install", "uninstall", "status"],
        help="install installs/updates the LaunchAgent; uninstall removes it; status shows state",
    )
    parser.add_argument(
        "--time",
        default="09:00",
        type=parse_time,
        help="daily push time in 24h HH:MM, default 09:00",
    )
    args = parser.parse_args()

    if sys.platform != "darwin":
        raise SystemExit("此脚本只适用于 macOS launchd。")

    if args.action == "install":
        install(args.time)
    elif args.action == "uninstall":
        uninstall()
    else:
        status()


if __name__ == "__main__":
    main()
