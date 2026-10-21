#!/usr/bin/env python3
"""One command to save your work to GitHub:   .\\ship "what changed"

Scans for secrets, stages everything, commits and pushes. Stops if anything looks wrong."""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def git(*args, capture=False):
    r = subprocess.run(["git", *args], cwd=ROOT, text=True, encoding="utf-8", errors="replace",
                       capture_output=capture)
    return r.stdout.strip() if capture else r.returncode


def main():
    message = " ".join(sys.argv[1:]).strip()
    if not message:
        print('Say what changed in a few words, for example:  .\\ship "add rate limiting"')
        return 2
    if git("rev-parse", "--is-inside-work-tree", capture=True) != "true":
        print("This folder is not a git repository.")
        return 1

    if not git("status", "--porcelain", capture=True):
        ahead = git("rev-list", "--count", "@{u}..HEAD", capture=True)
        if ahead.isdigit() and int(ahead) > 0:
            print(f"Nothing new to commit, but {ahead} commit(s) are not on GitHub yet. Pushing them.")
            return git("push")
        print("Nothing to commit. Everything is already saved.")
        return 0

    git("add", "-A")
    if subprocess.run([sys.executable, str(ROOT / "tools" / "check_secrets.py")], cwd=ROOT).returncode:
        git("reset", "-q")  # unstage, so nothing sensitive is left queued
        print("Nothing was committed. Fix the lines above, then run this again.")
        return 1

    print("\nAbout to save:")
    git("status", "--short")
    if git("commit", "-m", message):
        return 1
    has_upstream = git("rev-parse", "--abbrev-ref", "@{u}", capture=True)
    code = git("push") if has_upstream and not has_upstream.startswith("fatal") else git("push", "-u", "origin", "HEAD")
    if code:
        print("\nThe commit is saved on your PC, but the push failed. Read the message above, then run:  git push")
        return code
    print("\nPushed to GitHub:", git("log", "-1", "--oneline", capture=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
