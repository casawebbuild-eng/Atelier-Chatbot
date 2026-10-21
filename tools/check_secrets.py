#!/usr/bin/env python3
"""Stops a commit that contains a key, a password or a tunnel address.

  python tools/check_secrets.py          scan what is staged for the next commit
  python tools/check_secrets.py --all    scan every file git tracks

To allow one line on purpose, end it with:  # secret-ok
"""
import re
import subprocess
import sys

PATTERNS = [
    ("a JWT-style key", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("a Supabase secret key", re.compile(r"sb_secret_[A-Za-z0-9_-]{10,}")),
    ("a Google API key", re.compile(r"AIza[0-9A-Za-z_-]{30,}")),
    ("an OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("a long hex secret", re.compile(r"\b[0-9a-fA-F]{40,}\b")),
    ("a tunnel address", re.compile(r"https?://[a-z0-9-]+\.ngrok(-free)?\.(app|dev|io)")),
]
BAD_NAMES = re.compile(r"(^|/)(\.env(\..*)?|[^/]*\.pem|[^/]*\.key|id_rsa)$")
SELF = "tools/check_secrets.py"


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


def mask(s):
    return f"{s[:4]}... ({len(s)} characters)"


def scan_line(path, number, line, found):
    if "secret-ok" in line:
        return
    # n8n exports carry the instance's public identifier ("meta": {"instanceId": "<64 hex>"}). It is not a key.
    if re.search(r'"instanceId"\s*:', line):
        return
    # A client key is stored only as a SHA-256 hash in the brand file ("adminKeyHash"). A hash is not the key.
    if re.search(r'"adminKeyHash"\s*:', line):
        return
    for label, rx in PATTERNS:
        m = rx.search(line)
        if m:
            found.append(f"  {path}:{number}  {label}: {mask(m.group(0))}")


def staged():
    found, path, number = [], None, 0
    for name in git("diff", "--cached", "--name-only", "--diff-filter=AM").splitlines():
        if BAD_NAMES.search(name):
            found.append(f"  {name}  a file that normally holds secrets")
    for line in git("diff", "--cached", "-U0", "--no-color", "--diff-filter=AM").splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            number = int(m.group(1)) - 1 if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            number += 1
            if path and path != SELF:
                scan_line(path, number, line[1:], found)
    return found


def everything():
    found = []
    for name in git("ls-files").splitlines():
        if name == SELF:
            continue
        if BAD_NAMES.search(name):
            found.append(f"  {name}  a file that normally holds secrets")
            continue
        try:
            with open(name, encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    scan_line(name, i, line, found)
        except (OSError, UnicodeDecodeError):
            continue
    return found


def main():
    found = everything() if "--all" in sys.argv else staged()
    if not found:
        print("Secret scan: clean.")
        return 0
    print("\nSTOPPED: this looks like a key, a password or a tunnel address.\n")
    print("\n".join(found))
    print("\nMove it into an environment variable. If a line is safe on purpose, end it with  # secret-ok")
    print("To commit anyway (only if you are sure):  git commit --no-verify\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
