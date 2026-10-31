"""首次向导的两项离线回归：副本一致；偏好生命周期和实际入口优先级。"""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
MODULES = ("book-search", "ebook-to-kindle", "ebook-translate")


class PipelineSetupTests(unittest.TestCase):
    def test_copies_identical(self):
        for relative in ("scripts/pipeline_setup.py", "references/onboarding.md"):
            with self.subTest(relative=relative):
                copies = [(ROOT / module / relative).read_bytes() for module in MODULES]
                self.assertEqual(copies[0], copies[1])
                self.assertEqual(copies[1], copies[2])

    def test_profile_read_write_and_precedence(self):
        script = ROOT / "book-search/scripts/pipeline_setup.py"
        spec = importlib.util.spec_from_file_location("onboarding_test", script)
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            profile = home / "preferences/profile.json"
            env = {k: v for k, v in os.environ.items() if not k.startswith(
                ("KINDLE_", "EBOOK_", "ZLIB_", "BOOK_SEARCH_"))}
            env.update(HOME=tmp, KINDLE_BOOK_PIPELINE_PROFILE=str(profile),
                       EBOOK_TO_KINDLE_RUNTIME=str(home / "runtime"), PYTHONDONTWRITEBYTECODE="1")

            def run(*args, code=None, extra=None, expected=0):
                result = subprocess.run([sys.executable] + (["-c", code] if code else [str(script)]) + list(args),
                                        env={**env, **(extra or {})}, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
                return result

            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(setup.profile_path(), profile)
                with mock.patch.dict(os.environ, {"KINDLE_BOOK_PIPELINE_PROFILE": ""}):
                    self.assertEqual(setup.profile_path(), home / ".config/kindle-book-pipeline/profile.json")
                self.assertFalse(json.loads(run("status", "--json").stdout)["has_profile"])
                run("save", "--set", "goals=find,convert,send,translate", "kindle_model=paperwhite2",
                    "output_dir=~/saved", "amazon_domain=amazon.com", "kindle_device_name=Example Kindle",
                    "translate_mode=bilingual", "send_method=email", "--json")
                run("save", "--set", "send_method=none", "--set", "send_method=email")
                saved = json.loads(profile.read_text(encoding="utf-8"))
                self.assertEqual(saved["goals"], list(setup.ROUTES))
                self.assertEqual(saved["translate_mode"], "bilingual")
                self.assertEqual(saved["version"], 1)
                self.assertIn("updated_at", saved)
                self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o600)
                self.assertTrue(json.loads(run("--json", "status").stdout)["has_profile"])

                convert_code = (
                    "import sys,json;sys.path.insert(0,sys.argv.pop(1));"
                    "from ebook_kindle.cli import build_parser;"
                    "print(json.dumps(str(build_parser().parse_args(['convert','sample.epub']+sys.argv[1:]).output_dir)))")
                pack_code = (
                    "import sys,json;sys.path.insert(0,sys.argv.pop(1));import ebook_translate;"
                    "ebook_translate.build_pack=lambda ws,out,*a,**k: print(json.dumps(str(out)));"
                    "sys.argv=['ebook_translate','pack','--workspace','unused']+sys.argv[1:];ebook_translate.main()")
                send_code = (
                    "import sys;sys.path.insert(0,sys.argv.pop(1));import send_to_kindle;"
                    "send_to_kindle.send_to_kindle=lambda **kw: {'status':'SUCCESS','domain':kw['domain'],'device':kw['device']};"
                    "sys.argv=['send_to_kindle','sample.epub']+sys.argv[1:];send_to_kindle.main()")
                for level in ("builtin", "profile", "environment", "cli"):
                    if level == "builtin":
                        profile.unlink()
                    else:
                        profile.write_text(json.dumps(saved), encoding="utf-8")
                    extra = {}
                    output = home / ("Documents/KindleBooks" if level == "builtin" else "saved")
                    domain = "amazon.co.jp" if level == "builtin" else "amazon.com"
                    device = None if level == "builtin" else "Example Kindle"
                    cli_output, cli_send = [], []
                    if level in ("environment", "cli"):
                        extra = {"EBOOK_TO_KINDLE_OUTPUT_DIR": str(home / "environment"),
                                 "KINDLE_AMAZON_DOMAIN": "amazon.co.uk", "KINDLE_DEVICE_NAME": "Environment Kindle"}
                        output, domain, device = home / "environment", "amazon.co.uk", "Environment Kindle"
                    if level == "cli":
                        output, domain, device = home / "command", "amazon.de", "CLI Kindle"
                        cli_output = ["--output-dir", str(output)]
                        cli_send = ["--domain", domain, "--device", device]
                    for module, code in (("ebook-to-kindle", convert_code), ("ebook-translate", pack_code)):
                        with self.subTest(level=level, module=module):
                            actual = json.loads(run(str(ROOT / module / "scripts"), *cli_output, code=code, extra=extra).stdout)
                            expected_output = output.resolve() if module == "ebook-translate" else output
                            self.assertEqual(actual, str(expected_output))
                            print(f"优先级 {level} / {module}：" + str(Path(actual).relative_to(home.resolve() if module == "ebook-translate" else home)))
                    with self.subTest(level=level, module="send"):
                        actual = json.loads(run(str(ROOT / "ebook-to-kindle/scripts"), *cli_send, code=send_code, extra=extra).stdout)
                        self.assertEqual((actual["domain"], actual["device"]), (domain, device))
                        print(f"优先级 {level} / send：domain={domain}, device={device}")

                for invalid in ({"send_method": "invalid"}, {"version": True}, {"goals": "find"},
                                {"output_dir": "~this-user-does-not-exist-27/books"},
                                {"amazon_domain": "https://amazon.com"}, {"smtp_password": "do-not-echo"}):
                    with self.subTest(invalid=next(iter(invalid))):
                        profile.write_text(json.dumps(invalid), encoding="utf-8")
                        result = run("status", "--json")
                        self.assertFalse(json.loads(result.stdout)["has_profile"])
                        self.assertIn("损坏", result.stderr)
                        self.assertNotIn("do-not-echo", result.stderr + result.stdout)
                profile.write_text("{ broken", encoding="utf-8")
                result = run(str(ROOT / "ebook-to-kindle/scripts"), code=convert_code)
                self.assertEqual(json.loads(result.stdout), str(home / "Documents/KindleBooks"))
                self.assertIn("损坏", result.stderr)
                profile.write_text(json.dumps(saved), encoding="utf-8")
                original = profile.read_bytes()
                for assignment in ("smtp_password=do-not-echo", "send_method=invalid", "goals=unknown", "version=2"):
                    result = run("save", "--set", assignment, "--json", expected=2)
                    self.assertNotIn("do-not-echo", result.stdout + result.stderr)
                    self.assertEqual(profile.read_bytes(), original)

                run("reset")
                self.assertTrue(profile.exists())
                config = home / ".config/kindle/email_config.json"
                config.parent.mkdir(parents=True)
                config.write_text(json.dumps({"kindle_email": "YOUR_KINDLE_EMAIL", "sender_email": "YOUR_SENDER_EMAIL",
                                              "smtp_password": "YOUR_APP_SPECIFIC_PASSWORD"}), encoding="utf-8")
                self.assertFalse(setup.email_ready()["fields_ready"])
                for route, expected in (("find", 0), ("convert", 3), ("send", 3), ("translate", 3)):
                    result = json.loads(run("doctor", "--route", route, "--json", expected=expected).stdout)
                    self.assertEqual(result["ready"], expected == 0)
                    self.assertTrue(any(c["state"] == "这条路线不需要" for c in result["checks"]))
                # 凭证仅为临时夹具，检查器不得回显、改权限或发送。
                config.write_text(json.dumps({"kindle_email": "reader@example.invalid", "sender_email": "sender@example.invalid",
                                              "smtp_password": "fixture-secret"}), encoding="utf-8")
                config.chmod(0o644)
                result = run("doctor", "--route", "send", "--json")
                self.assertNotIn("fixture-secret", result.stdout + result.stderr)
                self.assertNotIn("reader@example.invalid", result.stdout + result.stderr)
                self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o644)
                with mock.patch.object(setup.shutil, "which", return_value=None), mock.patch.object(
                        setup, "email_ready", side_effect=AssertionError("browser/none must not read mail config")):
                    browser = setup.doctor(["send"], {"send_method": "browser"})
                    self.assertEqual(browser["missing"], ["ego-browser"])
                    self.assertTrue(setup.doctor(["send"], {"send_method": "none"})["ready"])
                config_before = config.read_bytes()
                run("reset", "--yes", "--json")
                self.assertFalse(profile.exists())
                self.assertEqual(config.read_bytes(), config_before)
                self.assertFalse(json.loads(run("status", "--json").stdout)["has_profile"])
                # 原子替换失败保留旧文件，临时写入不会遗留。
                setup.save_profile(["goals=find"])
                original = profile.read_bytes()
                with mock.patch.object(setup.os, "replace", side_effect=OSError("fixture failure")):
                    with self.assertRaises(OSError):
                        setup.save_profile(["goals=convert"])
                self.assertEqual(profile.read_bytes(), original)
                self.assertFalse(list(profile.parent.glob(".profile-*")))


if __name__ == "__main__":
    unittest.main()
