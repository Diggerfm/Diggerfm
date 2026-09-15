"""Publish site/ to GitHub Pages.

The audio previews are about 20 MB a week, so they are not committed onto a
branch that keeps history: each deploy replaces a single-commit orphan
`gh-pages` branch. The main branch keeps the code and never the previews.

    python deploy.py --check     # what would be pushed, pushes nothing
    python deploy.py             # build, commit the orphan branch, push

Set the remote once:
    git remote add origin https://github.com/<user>/<repo>.git
Then in the repository settings, Pages -> Deploy from branch -> gh-pages.
"""

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(ROOT, "site")
BRANCH = "gh-pages"


def run(args, cwd=ROOT, check=True, quiet=False):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError("%s\n%s%s" % (" ".join(args), r.stdout, r.stderr))
    if not quiet and r.stdout.strip():
        print(r.stdout.strip())
    return r


def size_report():
    total = files = 0
    for base, _dirs, names in os.walk(SITE):
        for n in names:
            total += os.path.getsize(os.path.join(base, n))
            files += 1
    return files, total


def main():
    check_only = "--check" in sys.argv

    if not os.path.isfile(os.path.join(SITE, "index.html")):
        print("site/index.html absent. Lance d'abord:")
        print("  python export_ui.py && python build_site.py")
        return 1

    files, total = size_report()
    print("site/: %d fichiers, %.1f Mo" % (files, total / 1048576))

    remote = run(["git", "remote", "-v"], check=False, quiet=True).stdout.strip()
    if not remote:
        print()
        print("Aucun remote git. Configure-le une fois:")
        print("  git init")
        print("  git remote add origin https://github.com/<user>/<repo>.git")
        return 1
    print("remote: %s" % remote.splitlines()[0])

    if check_only:
        print()
        print("--check: rien n'a ete pousse.")
        return 0

    # Build the branch in a scratch worktree so the working copy is untouched.
    tmp = tempfile.mkdtemp(prefix="digger_pages_")
    try:
        run(["git", "worktree", "add", "--detach", tmp], quiet=True)
        # A throwaway branch name, pushed to gh-pages by refspec. Checking
        # out --orphan gh-pages directly worked once and then failed with
        # "a branch named gh-pages already exists": the first deploy leaves
        # the local ref behind, and this runs every week.
        import time as _t
        scratch = "_deploy_%d" % int(_t.time())
        run(["git", "checkout", "--orphan", scratch], cwd=tmp, quiet=True)
        run(["git", "rm", "-rf", "--quiet", "."], cwd=tmp, check=False, quiet=True)
        for name in os.listdir(SITE):
            src = os.path.join(SITE, name)
            dst = os.path.join(tmp, name)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        open(os.path.join(tmp, ".nojekyll"), "w").close()
        run(["git", "add", "-A"], cwd=tmp, quiet=True)
        import datetime
        msg = "site %s" % datetime.date.today().isoformat()
        run(["git", "commit", "-m", msg], cwd=tmp, quiet=True)
        run(["git", "push", "--force", "origin", "HEAD:refs/heads/" + BRANCH],
            cwd=tmp)
        print()
        print("pousse sur %s." % BRANCH)
        print("Settings -> Pages -> Deploy from branch -> %s / (root)" % BRANCH)
    finally:
        run(["git", "worktree", "remove", "--force", tmp], check=False, quiet=True)
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            run(["git", "branch", "-D", scratch], check=False, quiet=True)
        except NameError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
