#!/usr/bin/env python3
"""首次使用偏好与离线体检；三模块保留逐字节相同副本，Python 3.9+。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

SCRIPT_INTERFACE = "cli"
SCRIPT_INTERFACE_REASON = "共享的首次设置、状态与离线体检入口。"
ROUTES = ("find", "convert", "send", "translate")
ENUMS = {
    "kindle_model": ("paperwhite2", "paperwhite_other", "other_kindle", "no_kindle", "unknown"),
    "send_method": ("email", "browser", "none"),
    "translate_mode": ("zh", "bilingual"),
}
FIELDS = {"version", "goals", "output_dir", "amazon_domain", "kindle_device_name", "updated_at", *ENUMS}
ROUTE_FIELDS = {
    "find": (), "convert": ("kindle_model", "output_dir"),
    "send": ("send_method", "amazon_domain"), "translate": ("translate_mode",),
}
LABELS = {"find": "找书", "convert": "转书", "send": "发到 Kindle", "translate": "翻译"}


class ProfileError(ValueError):
    """可向用户展示的错误；消息不含输入原值。"""


def profile_path() -> Path:
    return Path(os.environ.get("KINDLE_BOOK_PIPELINE_PROFILE") or
                "~/.config/kindle-book-pipeline/profile.json").expanduser()


def validate_profile(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ProfileError("设置必须是 JSON 对象。")
    if set(data) - FIELDS:
        raise ProfileError("含不支持的字段；这里只能保存非敏感偏好，不能保存账号或密码。")
    for key, value in data.items():
        if key == "version":
            if type(value) is not int or value != 1:
                raise ProfileError("version 只支持整数 1。")
        elif key == "goals":
            if not isinstance(value, list) or any(not isinstance(v, str) or v not in ROUTES for v in value):
                raise ProfileError("goals 必须是 find、convert、send、translate 组成的列表。")
        else:
            if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
                raise ProfileError("设置字段必须是非空文字，且不能含控制字符。")
            if key in ENUMS and value not in ENUMS[key]:
                raise ProfileError(f"{key} 的选项不合法；可选：{', '.join(ENUMS[key])}。")
            if key == "amazon_domain" and not re.fullmatch(r"amazon(\.[a-z]{2,3}){1,2}", value):
                raise ProfileError("amazon_domain 请填写亚马逊站点域名，如 amazon.com 或 amazon.co.jp。")
            if key == "output_dir":
                try:
                    Path(value).expanduser()
                except RuntimeError:
                    raise ProfileError("output_dir 无法展开用户目录，请用 ~/ 开头或普通目录路径。") from None
            if key == "updated_at":
                try:
                    datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    raise ProfileError("updated_at 必须是 ISO 日期时间。") from None
    return dict(data)


def read_profile(warn: bool = True) -> Dict[str, Any]:
    try:
        path = profile_path()
        if not path.exists():
            return {}
        return validate_profile(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeError, RuntimeError):
        if warn:
            print("提示：偏好文件损坏、字段非法或无法读取，已按没有设置处理；请运行设置向导修复或 reset 重来。", file=sys.stderr)
        return {}


def preference(key: str, env: str, default: Any = None) -> Any:
    """命令行交给 argparse 覆盖；其余依次为环境变量、偏好、内置值。"""
    value = os.environ.get(env)
    if value:
        return value
    return read_profile().get(key, default)


def default_output_dir() -> Path:
    return Path(preference("output_dir", "EBOOK_TO_KINDLE_OUTPUT_DIR",
                           str(Path.home() / "Documents" / "KindleBooks"))).expanduser()


def save_profile(assignments) -> Dict[str, Any]:
    path = profile_path()
    if path.is_symlink():
        raise ProfileError("偏好路径是符号链接，请换一个普通文件路径。")
    data = read_profile()
    for assignment in assignments:
        key, sep, value = assignment.partition("=")
        if not sep or key not in FIELDS:
            raise ProfileError("请用 --set 字段=值；只接受向导列出的非敏感字段。")
        if key == "goals":
            try:
                value = json.loads(value) if value.startswith("[") else value.split(",") if value else []
            except ValueError:
                raise ProfileError("goals 列表格式不正确，请用 goals=find,convert。") from None
        elif key == "version":
            try:
                value = int(value)
            except ValueError:
                raise ProfileError("version 只支持整数 1。") from None
        data[key] = value
    data.setdefault("version", 1)
    validate_profile(data)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".profile-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return data


def status() -> Dict[str, Any]:
    path = profile_path()
    profile = read_profile()
    missing = []
    if not profile.get("goals"):
        missing.append({"route": "all", "fields": ["goals"], "next_step": "先选你想做的事；也可明确说跳过向导。"})
    for route in profile.get("goals", []):
        fields = [key for key in ROUTE_FIELDS[route] if key not in profile]
        if route == "send" and profile.get("send_method") == "none":
            fields = []
        if fields:
            missing.append({"route": route, "fields": fields, "next_step": f"请补充{LABELS[route]}的这些偏好；运行 doctor 检查软件和账号配置。"})
    return {"path": str(path), "exists": path.exists(), "has_profile": bool(profile),
            "profile": profile, "missing": missing,
            "note": "这里只检查偏好；软件和账号配置请运行 doctor，实际下载、转换和送达需另外验收。"}


def module_script(module: str, name: str) -> Optional[Path]:
    own = Path(__file__).resolve().parents[1]
    local = own / "scripts" / name
    target = local if local.is_file() else own.parent / module / "scripts" / name
    return target if target.is_file() else None


def probe(script: Optional[Path], code: str) -> bool:
    """只运行内置、固定的本地检查，不透传子进程日志或任何凭证。"""
    if script is None:
        return False
    try:
        result = subprocess.run([sys.executable, "-c",
                                 "import sys,json;sys.path.insert(0,sys.argv[1]);" + code,
                                 str(script.parent)], capture_output=True, text=True, timeout=30)
        return result.returncode == 0 and json.loads(result.stdout) is True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def email_ready() -> Dict[str, bool]:
    """仅检查是否有配置及所需字段；不调用有 chmod 副作用的 load_config。"""
    path = Path.home() / ".config" / "kindle" / "email_config.json"
    cfg = {}
    exists = path.is_file()
    if exists:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
        except (OSError, ValueError, UnicodeError):
            pass

    def present(value):
        return isinstance(value, str) and bool(value.strip()) and not value.upper().startswith("YOUR_")

    def value(key, *envs):
        return next((os.environ[n] for n in envs if os.environ.get(n)), cfg.get(key))

    sender = value("sender_email", "KINDLE_FROM_EMAIL")
    required = [value("kindle_email", "KINDLE_TO_EMAIL"), sender,
                value("smtp_user", "KINDLE_SMTP_USER") or sender,
                value("smtp_password", "KINDLE_SMTP_PASS", "KINDLE_SMTP_PASSWORD")]
    # 现有发送脚本会按发件邮箱推断 SMTP 主机和端口，不要求每项显式填写。
    return {"exists": exists, "fields_ready": all(present(v) for v in required)}


def doctor(routes, profile: Dict[str, Any]) -> Dict[str, Any]:
    routes = list(dict.fromkeys(routes or profile.get("goals", [])))
    checks = []

    def add(name, needed, ready, next_step, required=True, detail=""):
        state = "这条路线不需要" if not needed else "可用" if ready else "缺少"
        checks.append({"name": name, "state": state, "required": bool(needed and required),
                       "next_step": next_step if needed and not ready else "", "detail": detail})

    find = "find" in routes
    search = module_script("book-search", "book_search.py")
    gutenberg = find and probe(search, "from booksearch.providers.gutenberg import GutenbergProvider;print(json.dumps(bool(GutenbergProvider().is_ready()[0])))")
    zlib = find and probe(search, "from booksearch.providers.zlibrary import ZLibraryProvider;print(json.dumps(bool(ZLibraryProvider().is_ready()[0])))")
    add("古腾堡找书平台", find, gutenberg, "安装 book-search 模块；先用免登录的古腾堡公版书。",
        detail="仅检查内置 provider 的本地就绪状态；未联网，不加载第三方插件。")
    add("Z-Library（可选）", find, zlib, "找古腾堡不需要它；确需此平台时再按 book-search 登录说明配置自己的账号，不在聊天里提供凭证。", required=False)

    convert = "convert" in routes
    converter = module_script("ebook-to-kindle", "ebook_to_kindle.py")
    core_ready = False
    if convert and converter:
        try:
            result = subprocess.run([sys.executable, str(converter), "doctor"], capture_output=True, text=True, timeout=30)
            core_ready = result.returncode == 0 and json.loads(result.stdout).get("core_ready") is True
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    add("转书运行环境", convert, core_ready,
        "安装 ebook-to-kindle 模块后先查看 bootstrap_runtime.py --component core --print-plan；约 3GB 运行环境须经你同意才下载，再运行 doctor --deep。",
        detail="复用普通 doctor 的文件就绪检查，不代表 deep 核验或一次转换已通过。")

    translate = "translate" in routes
    translator = module_script("ebook-translate", "ebook_translate.py")
    calibre = translate and probe(translator, "import ebook_translate;print(json.dumps(ebook_translate.find_calibre_convert() is not None))")
    add("翻译模块与 Calibre", translate, calibre,
        "安装 ebook-translate 模块；把已有 ebook-convert 加入 PATH 或设置 EBOOK_TO_KINDLE_RUNTIME；需下载软件时先征得你的同意。",
        detail="只检查可执行文件能否找到；翻译由宿主 AI 完成，额度和文学质量未检查。")

    send = "send" in routes
    method = profile.get("send_method", "email")
    use_email = send and method == "email"
    mail = email_ready() if use_email else {"exists": False, "fields_ready": False}
    add("邮件配置文件", use_email, mail["exists"],
        "运行 send_to_kindle_email.py --init-config 生成模板，自己填写；若已用环境变量配齐，可不建文件。", required=False)
    add("邮件必需字段", use_email, mail["fields_ready"],
        "自己配置 Kindle 收件邮箱、发件邮箱和 SMTP 授权码，并在亚马逊添加已认可发件人；不要把密码发给 AI。",
        detail="只检查字段存在，不验证密码、SMTP 连接或亚马逊认可发件人。")
    ego = shutil.which("ego-browser") is not None
    add("ego-browser", send and method != "none", ego,
        "浏览器方式需要 ego-browser；大多数用户可选邮件，无需安装它。", required=method == "browser",
        detail="只检查命令是否存在，不打开浏览器、不检查亚马逊登录。")
    if send:
        add("发送脚本", method != "none", module_script("ebook-to-kindle", "send_to_kindle.py") is not None,
            "请安装 ebook-to-kindle 模块；单独安装找书或翻译模块不含发送脚本。")
    missing = [c["name"] for c in checks if c["required"] and c["state"] == "缺少"]
    return {"routes": routes, "checks": checks, "missing": missing, "ready": bool(routes) and not missing,
            "ego_browser_exists": ego,
            "note": "仅离线检查本地条件；不登录、不联网、不发邮件，也不代表下载、翻译、转换或送达成功。"}


def emit(payload, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if "error" in payload:
        print(payload["error"])
    if "path" in payload:
        print("偏好文件：" + payload["path"])
    if "has_profile" in payload:
        print("已有设置。" if payload["has_profile"] else "没有可用设置，请先回答首次使用问卷；也可明确说跳过。")
        for key, value in payload["profile"].items():
            print(f"  {key}：{value}")
        for row in payload["missing"]:
            print("待补：" + ", ".join(row["fields"]) + "。" + row["next_step"])
    if "checks" in payload:
        if not payload["routes"]:
            print("尚未选择路线；请先回答问卷，或用 doctor --route find。")
        for row in payload["checks"]:
            print(f"{row['name']}：{row['state']}" + ("（可选）" if not row["required"] and row["state"] != "这条路线不需要" else ""))
            if row["next_step"]:
                print("  下一步：" + row["next_step"])
            if row["detail"] and row["state"] != "这条路线不需要":
                print("  " + row["detail"])
    if "message" in payload:
        print(payload["message"])
    if "note" in payload:
        print(payload["note"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="首次使用设置与离线体检（只保存非敏感偏好）")
    parser.add_argument("--json", action="store_true", help="输出 JSON，供 AI 读取")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "save", "doctor", "reset"):
        command = sub.add_parser(name)
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if name == "save":
            command.add_argument("--set", action="append", nargs="+", required=True, metavar="字段=值")
        if name == "doctor":
            command.add_argument("--route", choices=ROUTES, action="append", help="可重复；不写则用已选路线")
        if name == "reset":
            command.add_argument("--yes", action="store_true", help="确认只删除偏好文件")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            payload = status()
        elif args.command == "save":
            save_profile([item for group in args.set for item in group])
            permissions = "文件权限为 600。" if hasattr(os, "fchmod") else "当前平台未设置 600 权限。"
            payload = {**status(), "message": "设置已合并保存，" + permissions + "请按选中路线运行 doctor。"}
        elif args.command == "doctor":
            payload = doctor(args.route, read_profile())
        else:
            path = profile_path()
            if path.is_symlink():
                raise ProfileError("偏好路径是符号链接，请换一个普通文件路径。")
            deleted = args.yes and path.exists()
            if deleted:
                path.unlink()
            payload = {"path": str(path), "deleted": deleted,
                       "message": "只删除偏好文件，不动账号配置和成品。" + ("已重置，下次重走问卷。" if args.yes else "尚未删除；加 --yes 才会执行。")}
        emit(payload, args.json)
        return 3 if args.command == "doctor" and not payload["ready"] else 0
    except ProfileError as exc:
        emit({"error": "操作未完成：" + str(exc)}, args.json)
        return 2
    except (OSError, ValueError, UnicodeError, RuntimeError):
        # 不回显输入或原始异常，避免意外泄露被误填的密码。
        emit({"error": "设置未保存或操作未完成：请检查字段、选项、JSON 格式、路径和写入权限；只接受非敏感偏好。"}, args.json)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
