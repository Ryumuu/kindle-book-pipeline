#!/usr/bin/env python3
"""
Send to Kindle by Email Automator
Uploads ebooks (EPUB, PDF, DOCX, TXT, etc.) to Amazon Kindle devices
via Amazon's Send to Kindle E-mail service.

Pure Python standard library (smtplib, email) - zero external dependencies.
Ideal for headless servers, VPS, cloud agents, and local environments.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_DIR = Path.home() / ".config" / "kindle"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "email_config.json"

DEFAULT_KINDLE_EMAIL = os.getenv("KINDLE_TO_EMAIL", "")
DEFAULT_SENDER_EMAIL = os.getenv("KINDLE_FROM_EMAIL", "")

SMTP_PRESETS = {
    "gmail.com": {"host": "smtp.gmail.com", "port": 587, "ssl": False, "starttls": True},
    "googlemail.com": {"host": "smtp.gmail.com", "port": 587, "ssl": False, "starttls": True},
    "qq.com": {"host": "smtp.qq.com", "port": 465, "ssl": True, "starttls": False},
    "foxmail.com": {"host": "smtp.qq.com", "port": 465, "ssl": True, "starttls": False},
    "163.com": {"host": "smtp.163.com", "port": 465, "ssl": True, "starttls": False},
    "126.com": {"host": "smtp.126.com", "port": 465, "ssl": True, "starttls": False},
    "outlook.com": {"host": "smtp-mail.outlook.com", "port": 587, "ssl": False, "starttls": True},
    "hotmail.com": {"host": "smtp-mail.outlook.com", "port": 587, "ssl": False, "starttls": True},
}

SUPPORTED_EXTENSIONS = {
    ".epub", ".pdf", ".doc", ".docx", ".txt", ".rtf",
    ".htm", ".html", ".png", ".gif", ".jpg", ".jpeg", ".bmp"
}
# Amazon's e-mail service no longer accepts these; use an EPUB instead.
KINDLE_NATIVE_EXTENSIONS = {".azw3", ".azw", ".mobi"}


def _env_int(name: str) -> Optional[int]:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw.isdigit() else None


def _tighten_permissions(path: Path) -> None:
    """An older or hand-made config may be group/world readable; it holds an SMTP password."""
    if not hasattr(os, "getuid"):
        return  # Windows has no POSIX owner/mode bits to tighten.
    try:
        info = os.lstat(path)
    except OSError:
        return
    import stat

    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not info.st_mode & 0o077:
        return
    try:
        os.chmod(path, 0o600)
        sys.stderr.write(f"Note: restricted {path} to owner-only (600) because it holds an SMTP password.\n")
    except OSError as exc:
        sys.stderr.write(f"Warning: {path} is readable by other users and could not be fixed: {exc}\n")


def load_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    path = config_path or DEFAULT_CONFIG_PATH
    _tighten_permissions(path)
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            sys.stderr.write(f"Warning: Failed to parse config file {path}: {e}\n")
    return {}


def init_config(target_path: Optional[Path] = None) -> Path:
    path = target_path or DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        _tighten_permissions(path)
        return path

    template = {
        "kindle_email": DEFAULT_KINDLE_EMAIL,
        "sender_email": DEFAULT_SENDER_EMAIL,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_ssl": False,
        "smtp_starttls": True,
        "smtp_user": DEFAULT_SENDER_EMAIL,
        "smtp_password": "YOUR_APP_SPECIFIC_PASSWORD",
        "notes": "For Gmail: Generate a 16-character App Password at https://myaccount.google.com/apppasswords. For QQ/163: use SMTP Authorization Code."
    }
    # The file will hold an SMTP password: create it readable by the owner only.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=2)
    return path


def infer_smtp_settings(sender_email: str) -> Dict[str, Any]:
    domain = sender_email.split("@")[-1].lower() if "@" in sender_email else ""
    return SMTP_PRESETS.get(domain, {
        "host": f"smtp.{domain}" if domain else "localhost",
        "port": 587,
        "ssl": False,
        "starttls": True
    })


def send_to_kindle_email(
    file_path: Path,
    kindle_email: Optional[str] = None,
    sender_email: Optional[str] = None,
    smtp_host: Optional[str] = None,
    smtp_port: Optional[int] = None,
    smtp_user: Optional[str] = None,
    smtp_password: Optional[str] = None,
    use_ssl: Optional[bool] = None,
    use_starttls: Optional[bool] = None,
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    abs_path = file_path.resolve()
    if not abs_path.is_file():
        return {
            "status": "FAIL",
            "error": "FILE_NOT_FOUND",
            "message": f"Source file does not exist: {abs_path}",
        }

    ext = abs_path.suffix.lower()
    if ext in KINDLE_NATIVE_EXTENSIONS:
        sibling = abs_path.with_suffix(".epub")
        if not sibling.is_file():
            return {
                "status": "FAIL",
                "error": "UNSUPPORTED_FORMAT",
                "message": (
                    f"Send to Kindle by e-mail does not accept {ext}. Send an EPUB instead "
                    f"(e.g. convert with --formats azw3,epub), or use the browser method."
                ),
            }
        abs_path, ext = sibling, ".epub"
    if ext not in SUPPORTED_EXTENSIONS:
        return {
            "status": "FAIL",
            "error": "UNSUPPORTED_FORMAT",
            "message": f"Format {ext} is not supported by Amazon Send to Kindle E-mail. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
        }

    file_size_mb = abs_path.stat().st_size / (1024 * 1024)
    if file_size_mb > 25.0:
        return {
            "status": "FAIL",
            "error": "FILE_TOO_LARGE",
            "message": f"File size ({file_size_mb:.2f} MB) exceeds typical email attachment limit (25 MB). Consider using browser Send to Kindle or Calibre USB transfer.",
        }

    # Resolve configuration hierarchy: CLI args > Env vars > Config file > Defaults
    cfg = load_config(config_path)

    to_addr = (
        kindle_email
        or os.getenv("KINDLE_TO_EMAIL")
        or cfg.get("kindle_email")
        or DEFAULT_KINDLE_EMAIL
    )
    from_addr = (
        sender_email
        or os.getenv("KINDLE_FROM_EMAIL")
        or cfg.get("sender_email")
        or DEFAULT_SENDER_EMAIL
    )

    preset = infer_smtp_settings(from_addr)

    host = (
        smtp_host
        or os.getenv("KINDLE_SMTP_HOST")
        or cfg.get("smtp_host")
        or preset.get("host")
    )
    port = (
        smtp_port
        or _env_int("KINDLE_SMTP_PORT")
        or cfg.get("smtp_port")
        or preset.get("port")
    )
    user = (
        smtp_user
        or os.getenv("KINDLE_SMTP_USER")
        or cfg.get("smtp_user")
        or from_addr
    )
    password = (
        smtp_password
        or os.getenv("KINDLE_SMTP_PASS")
        or os.getenv("KINDLE_SMTP_PASSWORD")
        or cfg.get("smtp_password")
    )

    if use_ssl is not None:
        ssl_enabled = use_ssl
    elif "KINDLE_SMTP_SSL" in os.environ:
        ssl_enabled = os.environ["KINDLE_SMTP_SSL"].lower() in ("true", "1", "yes")
    elif "smtp_ssl" in cfg:
        ssl_enabled = bool(cfg["smtp_ssl"])
    else:
        ssl_enabled = preset.get("ssl", False)

    if use_starttls is not None:
        starttls_enabled = use_starttls
    elif "smtp_starttls" in cfg:
        starttls_enabled = bool(cfg["smtp_starttls"])
    else:
        starttls_enabled = preset.get("starttls", not ssl_enabled)

    shown_config = str(config_path or DEFAULT_CONFIG_PATH)
    if not to_addr or "@" not in to_addr or not from_addr or "@" not in from_addr:
        return {
            "status": "FAIL",
            "error": "MISSING_EMAIL_ADDRESS",
            "message": (
                "Kindle address and sender address are required. Set kindle_email / sender_email in "
                f"{shown_config}, or KINDLE_TO_EMAIL / KINDLE_FROM_EMAIL."
            ),
            "config_path": shown_config,
        }

    if not password or password == "YOUR_APP_SPECIFIC_PASSWORD":
        return {
            "status": "FAIL",
            "error": "MISSING_SMTP_CREDENTIALS",
            "message": (
                f"SMTP password is not configured. "
                f"Please set your SMTP App Password in {shown_config} "
                f"or set the KINDLE_SMTP_PASS environment variable."
            ),
            "config_path": shown_config,
        }

    # Build Email Message
    msg = EmailMessage()
    msg["Subject"] = abs_path.stem
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(f"Sending ebook {abs_path.name} to Kindle.")

    mime_type, _ = mimetypes.guess_type(str(abs_path))
    if mime_type:
        maintype, subtype = mime_type.split("/", 1)
    elif ext == ".epub":
        maintype, subtype = "application", "epub+zip"
    else:
        maintype, subtype = "application", "octet-stream"

    with open(abs_path, "rb") as f:
        file_data = f.read()

    msg.add_attachment(
        file_data,
        maintype=maintype,
        subtype=subtype,
        filename=abs_path.name
    )

    # Dispatch via SMTP
    try:
        context = ssl.create_default_context()
        if ssl_enabled:
            server = smtplib.SMTP_SSL(host, int(port), context=context, timeout=60)
        else:
            server = smtplib.SMTP(host, int(port), timeout=60)
        with server:
            if not ssl_enabled and starttls_enabled:
                server.starttls(context=context)
            server.login(user, password)
            server.send_message(msg)

        return {
            "status": "SUCCESS",
            "file": str(abs_path),
            "method": "EMAIL",
            "to": to_addr,
            "from": from_addr,
            "smtp_host": host,
            "message": f"Ebook successfully emailed to {to_addr}. Amazon will convert and sync to your Kindle device upon Wi-Fi connection.",
        }
    except Exception as e:
        return {
            "status": "FAIL",
            "error": "SMTP_SEND_FAILED",
            "message": f"Failed to send email via {host}:{port}: {type(e).__name__}: {e}",
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="send-to-kindle-email",
        description="Wirelessly push ebooks to Amazon Send to Kindle by Email.",
    )
    parser.add_argument("file", nargs="?", type=Path, help="Path to the ebook file to push.")
    parser.add_argument("--to", dest="to_addr", help="Kindle recipient address (default: $KINDLE_TO_EMAIL or config).")
    parser.add_argument("--from", dest="from_addr", help="Approved sender address (default: $KINDLE_FROM_EMAIL or config).")
    parser.add_argument("--smtp-host", help="SMTP server host (e.g. smtp.gmail.com).")
    parser.add_argument("--smtp-port", type=int, help="SMTP server port (e.g. 587 or 465).")
    parser.add_argument("--smtp-user", help="SMTP username.")
    parser.add_argument("--ssl", action="store_true", help="Force SSL connection.")
    parser.add_argument("--starttls", action="store_true", help="Force STARTTLS connection.")
    parser.add_argument("--config", type=Path, help=f"Custom config file (default: {DEFAULT_CONFIG_PATH}).")
    parser.add_argument("--init-config", action="store_true", help="Generate default configuration template if not exists.")

    args = parser.parse_args()

    if args.init_config:
        path = init_config(args.config)
        print(json.dumps({"status": "SUCCESS", "config_path": str(path), "message": "Config initialized."}, ensure_ascii=False, indent=2))
        return 0

    if not args.file:
        parser.print_help()
        return 1

    result = send_to_kindle_email(
        file_path=args.file,
        kindle_email=args.to_addr,
        sender_email=args.from_addr,
        smtp_host=args.smtp_host,
        smtp_port=args.smtp_port,
        smtp_user=args.smtp_user,
        use_ssl=args.ssl if args.ssl else None,
        use_starttls=args.starttls if args.starttls else None,
        config_path=args.config,
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
