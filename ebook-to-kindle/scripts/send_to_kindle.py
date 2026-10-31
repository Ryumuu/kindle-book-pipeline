#!/usr/bin/env python3
"""
Send to Kindle Automator
Uploads ebooks (EPUB, PDF, DOCX, TXT, ...) to Amazon Send to Kindle via
ego-browser and optionally asks Amazon to deliver the upload to one device.
Send to Kindle does not accept AZW3/MOBI; for those the sibling .epub is sent.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from pipeline_setup import preference

NODE_SCRIPT_TEMPLATE = r"""
(async () => {
  const filePath = __FILE_PATH__;
  const domain = __DOMAIN__;
  const targetDevice = __DEVICE__;
  const deliverToDevice = __DELIVER__;
  const maxWaitSeconds = parseInt(__TIMEOUT__, 10) || 180;
  const expectedTitle = __TITLE__;

  // Kept outside try so a late failure still reports what already happened on Amazon's side.
  let task = null;
  let keepTask = false;
  let stage = 'prepare';            // prepare -> send-clicked -> uploaded -> delivery-clicked
  let deliveryStatus = null;
  let assignedDevice = null;
  let note = '';
  let result = null;

  try {
    // 1. Initialize task space
    task = await useOrCreateTaskSpace('send to kindle');

    const sendUrl = 'https://www.' + domain + '/sendtokindle';
    const pdocsUrl = 'https://www.' + domain + '/hz/mycd/digital-console/contentlist/pdocs/dateDsc/';
    const listDocs = () => js(String.raw`(() => {
      return Array.from(document.querySelectorAll('input[id$=":KindlePDoc"]')).map(b => {
        const asin = b.id.split(':')[0];
        let el = b;
        for (let i = 0; i < 6 && el && !el.querySelector('.digital_entity_title, .digital_entity_title_no_link'); i++) el = el.parentElement;
        const t = el && el.querySelector('.digital_entity_title, .digital_entity_title_no_link');
        return { asin, title: t ? t.innerText.trim() : '' };
      });
    })()`).then(rows => rows || []);

    // 2. Open Send to Kindle page
    await openOrReuseTab(sendUrl, { wait: true, timeout: 30 });
    await wait(2);

    // Check if logged in
    const loginCheck = await js(String.raw`(() => {
      const navAccount = document.querySelector('#nav-link-accountList, #nav-your-amazon');
      const isSignInPage = window.location.href.includes('/signin') || !!document.querySelector('input[type="password"]');
      return {
        isSignIn: isSignInPage,
        userText: navAccount ? navAccount.innerText : ''
      };
    })()`);

    if (loginCheck.isSignIn) {
      result = {
        status: 'FAIL',
        error: 'NOT_LOGGED_IN',
        message: 'Please log in to https://www.' + domain + ' in ego-browser before sending.'
      };
      keepTask = true;
      return;
    }

    // Remember the newest document before uploading. The list is sorted newest first, so the upload
    // can only be a row that appears ABOVE that document afterwards; an older copy with the same title
    // that loads late always shows up below it. The baseline is trusted only when two consecutive reads
    // return the same non-empty ASIN sequence; otherwise it stays unknown (null) and nothing is picked
    // by "is it new?" — no list-based upload confirmation and no explicit delivery.
    let knownAsins = null;
    let newestBefore = null;
    await gotoAndWait(pdocsUrl, { timeout: 25 });
    const seen = new Set();
    let previous = null;
    for (let i = 0; i < 8; i++) {
      await wait(2);
      const rows = await listDocs();
      const key = rows.map(r => r.asin).join('|');
      rows.forEach(r => seen.add(r.asin));
      if (rows.length > 0 && key === previous) {
        knownAsins = seen;
        newestBefore = rows[0].asin;
        break;
      }
      previous = key;
    }
    await gotoAndWait(sendUrl, { timeout: 30 });
    await wait(2);

    const norm = (t) => String(t || '').toLowerCase().replace(/[\s\p{P}\p{S}]+/gu, '');
    const wanted = norm(expectedTitle);
    const findNewUpload = async () => {
      if (!knownAsins) return [];
      const rows = await listDocs();
      const cut = rows.findIndex(r => r.asin === newestBefore);
      if (cut < 0) return [];  // cannot place the new rows relative to the old list
      const fresh = rows.slice(0, cut).filter(r => !knownAsins.has(r.asin));
      const exact = fresh.filter(r => wanted && norm(r.title) === wanted);
      const loose = fresh.filter(r => {
        const got = norm(r.title);
        return got && wanted && (got.includes(wanted) || wanted.includes(got));
      });
      return exact.length ? exact : loose;
    };

    // 3. Set up CDP file chooser interception
    await cdp('Page.setInterceptFileChooserDialog', { enabled: true });

    let backendNodeId = null;
    const chooserPromise = new Promise((resolve) => {
      const check = setInterval(async () => {
        const events = await drainEvents();
        for (const ev of events) {
          if (ev.method === 'Page.fileChooserOpened') {
            clearInterval(check);
            resolve(ev.params);
            return;
          }
        }
      }, 200);
      setTimeout(() => {
        clearInterval(check);
        resolve(null);
      }, 15000);
    });

    // Click the file chooser trigger
    await click('#s2k-dnd-add-your-files-button', { label: 'select file button' });
    const chooserEvent = await chooserPromise;

    if (chooserEvent && chooserEvent.backendNodeId) {
      backendNodeId = chooserEvent.backendNodeId;
    } else {
      // Fallback: look for file input backendNodeId directly
      const inputDoc = await cdp('DOM.getDocument', { depth: 3 });
      const findInput = (node) => {
        if (node.nodeName === 'INPUT' && node.attributes) {
          const idx = node.attributes.indexOf('type');
          if (idx !== -1 && node.attributes[idx + 1] === 'file') return node.backendNodeId;
        }
        if (node.children) {
          for (const c of node.children) {
            const found = findInput(c);
            if (found) return found;
          }
        }
        return null;
      };
      backendNodeId = findInput(inputDoc.root);
    }

    if (!backendNodeId) {
      throw new Error('Could not obtain file input backendNodeId from Send to Kindle page.');
    }

    // 4. Inject file
    await cdp('DOM.setFileInputFiles', {
      files: [filePath],
      backendNodeId: backendNodeId
    });

    // 5. Wait for Send button to become enabled
    let readyToSend = false;
    for (let i = 0; i < 20; i++) {
      await wait(1);
      const btnState = await js(String.raw`(() => {
        const btn = document.getElementById('s2k-r2s-send-button');
        return btn ? { exists: true, disabled: btn.disabled, visible: btn.offsetParent !== null } : { exists: false };
      })()`);
      if (btnState.exists && !btnState.disabled && btnState.visible) {
        readyToSend = true;
        break;
      }
    }

    if (!readyToSend) {
      throw new Error('Send button did not become ready after file injection.');
    }

    // 6. Click Send button
    stage = 'send-clicked';
    await click('#s2k-r2s-send-button', { label: 'click send button' });

    // 7. Wait for upload to complete ("Your file is on its way")
    const successTexts = ['on its way', 'on their way', '您的文件已在途中', '文件已在途中'];
    let uploadSuccess = false;
    for (let i = 0; i < Math.max(15, Math.ceil(maxWaitSeconds / 2)); i++) {
      await wait(2);
      const pageText = await js(String.raw`document.body.innerText`);
      if (successTexts.some(t => pageText.includes(t))) {
        uploadSuccess = true;
        break;
      }
    }

    if (!uploadSuccess) {
      // The confirmation text depends on the page language; fall back to looking for the new document.
      for (let attempt = 0; attempt < 3 && !uploadSuccess; attempt++) {
        await gotoAndWait(pdocsUrl, { timeout: 25 });
        await wait(5 + attempt * 5);
        uploadSuccess = (await findNewUpload()).length > 0;
      }
    }

    if (!uploadSuccess) {
      throw new Error('Upload timed out or was not acknowledged by Send to Kindle.');
    }

    stage = 'uploaded';
    deliveryStatus = 'CLOUD_UPLOADED';
    note = 'Uploaded to your Kindle library; devices pick it up on their next sync.';

    // 8. Optional: push this one document to a chosen device via Manage Content & Devices.
    //    Only a document that was not in the library before this upload is touched; never "select all".
    if (deliverToDevice) {
      let row = null;
      let ambiguous = false;
      for (let attempt = 0; knownAsins && attempt < 6 && !row && !ambiguous; attempt++) {
        await gotoAndWait(pdocsUrl, { timeout: 25 });
        await wait(3 + attempt * 2);
        const hits = await findNewUpload();
        if (hits.length === 1) row = hits[0];
        else if (hits.length > 1) ambiguous = true;
      }

      const cancelDialog = (asin) => js(String.raw`(() => {
        const d = document.querySelector('#DELIVER_OR_REMOVE_FROM_DEVICE_DIALOG_' + ${JSON.stringify(asin)} + ' [id$="_CANCEL"]');
        if (d) d.click();
      })()`);

      if (!knownAsins) {
        note = 'Uploaded, but the library list could not be read before the upload, so the new copy cannot be told apart from older ones; skipped explicit device delivery.';
      } else if (ambiguous) {
        note = 'Uploaded, but several new documents match this title; skipped explicit device delivery.';
      } else if (!row) {
        note = 'Uploaded, but the new document was not found in Manage Content yet; skipped explicit device delivery.';
      } else {
        const opened = await js(String.raw`(() => {
          const asin = ${JSON.stringify(row.asin)};
          const dialog = document.getElementById('DELIVER_OR_REMOVE_FROM_DEVICE_DIALOG_' + asin);
          const confirm = dialog && dialog.querySelector('[id$="_CONFIRM"]');
          const trigger = confirm && document.getElementById(confirm.id.replace(/_CONFIRM$/, ''));
          if (!trigger) return { error: 'deliver button not found' };
          trigger.click();
          return { ok: true };
        })()`);
        const readDevices = () => js(String.raw`(() => {
          const asin = ${JSON.stringify(row.asin)};
          return Array.from(document.querySelectorAll('#deliver_to_device_list_' + asin + ' li')).map((li, n) => {
            const v = li.querySelector('[class*="action_list_value"]');
            const box = li.querySelector('input[type="checkbox"]');
            return { id: box ? box.id : 'deliver_to_device_list_' + asin + '_' + n, name: (v ? v.innerText : li.innerText).trim() };
          });
        })()`).then(rows => rows || []);

        // The dialog fills its device list asynchronously; read it only after it has appeared.
        let devices = opened && opened.error ? { error: opened.error } : { error: 'device list did not load' };
        if (opened && opened.ok) {
          for (let i = 0; i < 10; i++) {
            await wait(1);
            const rows = await readDevices();
            if (rows.length) { devices = { list: rows }; break; }
          }
        }

        const list = (devices && devices.list) || [];
        const names = list.map(d => d.name);
        let target = null;
        if (devices && devices.error) {
          note = 'Uploaded, but ' + devices.error + '; skipped explicit device delivery.';
        } else if (targetDevice) {
          const want = targetDevice.trim().toLowerCase();
          const exact = list.filter(d => d.name.toLowerCase() === want);
          const loose = list.filter(d => d.name.toLowerCase().includes(want));
          const hits = exact.length ? exact : loose;
          if (hits.length === 1) target = hits[0];
          else if (hits.length > 1) note = 'Uploaded, but "' + targetDevice + '" matches several devices: ' + hits.map(d => d.name).join(', ') + '. Pass the full device name.';
          else note = 'Uploaded, but device "' + targetDevice + '" not found. Available: ' + names.join(', ');
        } else if (list.length === 1) {
          target = list[0];
        } else {
          note = 'Uploaded. Several devices found; pass --device to push to one of: ' + names.join(', ');
        }

        if (target) {
          const picked = await js(String.raw`(() => {
            const asin = ${JSON.stringify(row.asin)};
            const targetId = ${JSON.stringify(target.id)};
            const boxes = Array.from(document.querySelectorAll('#deliver_to_device_list_' + asin + ' input[type="checkbox"]'));
            if (!boxes.some(b => b.id === targetId)) return { ok: false, reason: 'device checkbox not found' };
            for (const b of boxes) {
              const want = b.id === targetId;
              if (b.checked !== want) (document.getElementById(b.id + '_checkmark') || b).click();
            }
            const selected = boxes.filter(b => b.checked).map(b => b.id);
            if (selected.length !== 1 || selected[0] !== targetId) return { ok: false, reason: 'could not select exactly one device' };
            const dialog = document.getElementById('DELIVER_OR_REMOVE_FROM_DEVICE_DIALOG_' + asin);
            const btn = dialog && dialog.querySelector('[id$="_CONFIRM"]');
            if (!btn) return { ok: false, reason: 'confirm button not found' };
            if (btn.disabled || btn.getAttribute('aria-disabled') === 'true' || /disabled/.test(btn.className || '')) return { ok: false, reason: 'confirm button is disabled' };
            return { ok: true };
          })()`);
          let clicked = null;
          if (picked && picked.ok) {
            // From here on Amazon may have the request even if the reply below is lost.
            stage = 'delivery-clicked';
            assignedDevice = target.name;
            clicked = await js(String.raw`(() => {
              const asin = ${JSON.stringify(row.asin)};
              const targetId = ${JSON.stringify(target.id)};
              // Re-check the selection in the same call as the click: the page may have changed it meanwhile.
              const boxes = Array.from(document.querySelectorAll('#deliver_to_device_list_' + asin + ' input[type="checkbox"]'));
              const selected = boxes.filter(b => b.checked).map(b => b.id);
              if (selected.length !== 1 || selected[0] !== targetId) return { clicked: false, reason: 'device selection changed before confirming' };
              const dialog = document.getElementById('DELIVER_OR_REMOVE_FROM_DEVICE_DIALOG_' + asin);
              const btn = dialog && dialog.querySelector('[id$="_CONFIRM"]');
              if (!btn || btn.disabled || btn.getAttribute('aria-disabled') === 'true') return { clicked: false, reason: 'confirm button not clickable' };
              btn.click();
              return { clicked: true };
            })()`);
          }
          if (clicked && clicked.clicked === false) {
            stage = 'uploaded';
            assignedDevice = null;
            await cancelDialog(row.asin);
            note = 'Uploaded, but ' + clicked.reason + '; skipped explicit device delivery.';
          } else if (picked && picked.ok && !clicked) {
            deliveryStatus = 'DELIVERY_UNCONFIRMED';
            note = 'Uploaded; delivery to ' + target.name + ' may have been submitted but could not be confirmed. Check Manage Content before retrying.';
          } else if (clicked) {
            await wait(3);
            const stillOpen = await js(String.raw`(() => {
              const d = document.getElementById('DELIVER_OR_REMOVE_FROM_DEVICE_DIALOG_' + ${JSON.stringify(row.asin)});
              return !!(d && d.offsetParent !== null);
            })()`);
            // Only a definite "dialog closed" counts; an empty or odd reply is unknown, not success.
            if (stillOpen === false) {
              deliveryStatus = 'DELIVERY_REQUESTED';
              note = 'Uploaded and asked Amazon to deliver it to ' + target.name + '.';
            } else {
              deliveryStatus = 'DELIVERY_UNCONFIRMED';
              note = 'Uploaded; delivery to ' + target.name + ' may have been submitted, but Amazon did not confirm it (dialog still open or unreadable). Check Manage Content before retrying.';
            }
          } else {
            await cancelDialog(row.asin);
            note = 'Uploaded, but ' + ((picked && picked.reason) || 'device selection failed') + '; skipped explicit device delivery.';
          }
        } else if (opened && opened.ok) {
          await cancelDialog(row.asin);
        }
      }
    }

    result = {
      status: 'SUCCESS',
      file: filePath,
      domain: domain,
      deliveryStatus: deliveryStatus,
      device: assignedDevice,
      message: note
    };

  } catch (err) {
    const reason = (err && err.message) || String(err);
    if (stage === 'prepare') {
      result = { status: 'FAIL', error: 'EXECUTION_ERROR', message: reason };
    } else if (stage === 'send-clicked') {
      result = {
        status: 'FAIL',
        error: 'UPLOAD_UNCONFIRMED',
        message: 'Send was clicked but the upload could not be confirmed (' + reason + '). Check your Kindle library before retrying.'
      };
    } else {
      // The upload already succeeded; keep that fact instead of reporting a plain failure.
      if (stage === 'delivery-clicked') deliveryStatus = 'DELIVERY_UNCONFIRMED';
      result = {
        status: 'SUCCESS',
        file: filePath,
        domain: domain,
        deliveryStatus: deliveryStatus,
        device: assignedDevice,
        message: (stage === 'delivery-clicked'
          ? 'Uploaded; delivery may have been submitted but could not be confirmed'
          : 'Uploaded to your Kindle library, but explicit device delivery failed') +
          ' (' + reason + '). Check Manage Content before retrying.'
      };
    }
  } finally {
    if (!result) result = { status: 'FAIL', error: 'EXECUTION_ERROR', message: 'Script ended without a result.' };
    if (task) {
      try {
        await completeTaskSpace(task.id, { keep: keepTask });
      } catch (err) {
        result.cleanupWarning = 'Could not close the ego-browser task space: ' + ((err && err.message) || String(err));
      }
    }
    cliLog(JSON.stringify(result));
  }
})();
"""


DEFAULT_DOMAIN = preference("amazon_domain", "KINDLE_AMAZON_DOMAIN", "amazon.co.jp")
DEFAULT_DEVICE = preference("kindle_device_name", "KINDLE_DEVICE_NAME")
SEND_TO_KINDLE_EXTENSIONS = {".epub", ".pdf", ".doc", ".docx", ".txt", ".rtf", ".htm", ".html", ".png", ".gif", ".jpg", ".jpeg", ".bmp"}
KINDLE_ONLY_EXTENSIONS = {".azw3", ".azw", ".mobi"}
DOMAIN_PATTERN = re.compile(r"amazon(\.[a-z]{2,3}){1,2}")


def build_node_script(
    file_path: str,
    domain: str,
    target_device: Optional[str],
    deliver: bool,
    timeout: int,
    title: Optional[str] = None,
) -> str:
    values = {
        "__FILE_PATH__": json.dumps(str(file_path)),
        "__DOMAIN__": json.dumps(domain),
        "__DEVICE__": json.dumps(target_device or ""),
        "__DELIVER__": "true" if deliver else "false",
        "__TIMEOUT__": str(int(timeout)),
        "__TITLE__": json.dumps(title or Path(file_path).stem),
    }
    # One pass over the template only: inserted values are never rescanned for placeholders.
    pattern = re.compile("|".join(re.escape(key) for key in values))
    return pattern.sub(lambda match: values[match.group(0)], NODE_SCRIPT_TEMPLATE)


def send_to_kindle(
    file_path: Path,
    domain: str = DEFAULT_DOMAIN,
    device: Optional[str] = DEFAULT_DEVICE,
    deliver: bool = True,
    timeout: int = 180,
    method: str = "auto",
    title: Optional[str] = None,
) -> Dict[str, Any]:
    if not DOMAIN_PATTERN.fullmatch(domain or ""):
        return {
            "status": "FAIL",
            "error": "INVALID_DOMAIN",
            "message": f"Expected an Amazon marketplace such as amazon.com or amazon.co.jp, got {domain!r}.",
        }

    abs_path = file_path.resolve()
    if not abs_path.is_file():
        return {
            "status": "FAIL",
            "error": "FILE_NOT_FOUND",
            "message": f"Source file does not exist: {abs_path}",
        }

    if abs_path.suffix.lower() in KINDLE_ONLY_EXTENSIONS:
        sibling = abs_path.with_suffix(".epub")
        if not sibling.is_file():
            return {
                "status": "FAIL",
                "error": "UNSUPPORTED_FORMAT",
                "message": (
                    f"Send to Kindle does not accept {abs_path.suffix}. Convert with "
                    "--formats azw3,epub and send the .epub, or copy the file over USB."
                ),
            }
        abs_path = sibling

    valid_extensions = SEND_TO_KINDLE_EXTENSIONS
    if abs_path.suffix.lower() not in valid_extensions:
        return {
            "status": "FAIL",
            "error": "UNSUPPORTED_FORMAT",
            "message": f"Format {abs_path.suffix} is not supported by Send to Kindle. Supported: {', '.join(sorted(valid_extensions))}",
        }

    import shutil
    has_ego_browser = bool(shutil.which("ego-browser"))

    if method == "email" or (method == "auto" and not has_ego_browser):
        script_dir = Path(__file__).resolve().parent
        email_script = script_dir / "send_to_kindle_email.py"
        cmd = [sys.executable, str(email_script), str(abs_path)]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return {"status": "FAIL", "error": "EMAIL_PUSH_TIMEOUT", "message": "E-mail push did not finish within 300s."}
        try:
            return json.loads(res.stdout)
        except Exception:
            return {
                "status": "FAIL",
                "error": "EMAIL_PUSH_FAILED",
                "stdout": res.stdout.strip(),
                "stderr": res.stderr.strip(),
            }

    if not has_ego_browser:
        return {
            "status": "FAIL",
            "error": "EGO_BROWSER_NOT_FOUND",
            "message": "ego-browser is not available in current environment. Use --method email for cloud/headless agents.",
        }

    node_script = build_node_script(str(abs_path), domain, device, deliver, timeout, title)

    try:
        proc = subprocess.run(
            ["ego-browser", "nodejs"],
            input=node_script,
            text=True,
            capture_output=True,
            timeout=timeout + 240,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "FAIL",
            "error": "BROWSER_PUSH_TIMEOUT",
            "message": (
                f"ego-browser did not finish within {timeout + 240}s. The upload or delivery may already have "
                "happened; check your Kindle library (Manage Content & Devices) before retrying."
            ),
        }

    combined_lines = (proc.stdout + "\n" + proc.stderr).strip().splitlines()
    for line in reversed(combined_lines):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    return {
        "status": "FAIL",
        "error": "NO_JSON_OUTPUT",
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "exit_code": proc.returncode,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="send-to-kindle",
        description="Wirelessly push ebooks (EPUB, PDF, DOCX, TXT, ...) to Amazon Send to Kindle via ego-browser or e-mail. AZW3/MOBI are swapped for the sibling .epub.",
    )
    parser.add_argument("file", type=Path, help="Path to the ebook file to push.")
    parser.add_argument("--domain", default=DEFAULT_DOMAIN, help="Amazon marketplace domain (CLI > $KINDLE_AMAZON_DOMAIN > profile.json > amazon.co.jp).")
    parser.add_argument("--device", default=DEFAULT_DEVICE, help="Device name (CLI > $KINDLE_DEVICE_NAME > profile.json > no device). Exact or unique partial name; browser method only.")
    parser.add_argument("--title", help="Document title as it will appear in your Kindle library (default: file name). Used to find the upload.")
    parser.add_argument("--no-deliver", action="store_true", help="Only upload to the cloud library; skip the explicit push to a device.")
    parser.add_argument("--method", choices=["auto", "browser", "email"], default="auto", help="Delivery method: 'browser' (CDP via ego-browser), 'email' (Send to Kindle by E-mail), or 'auto' (detect environment).")
    parser.add_argument("--timeout", type=int, default=180, help="Upload timeout in seconds (default: 180).")

    args = parser.parse_args()
    result = send_to_kindle(
        file_path=args.file,
        domain=args.domain,
        device=args.device,
        deliver=not args.no_deliver,
        timeout=args.timeout,
        method=args.method,
        title=args.title,
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
