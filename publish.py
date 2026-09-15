"""Rebuild the site from what is already in the database, and push it.

The distinction that matters day to day: collecting and scoring are
separate. Nothing about the ranking is stored, it is derived at query time
from the sightings table, so changing a weight, a quota or a piece of
copy needs no new data at all. Only new charts, new sets and new releases
need a collect.

    python publish.py            # export, build, deploy
    python publish.py --local    # export and build, serve on :8765
    python publish.py --dry      # export and build, push nothing
"""

import subprocess
import sys
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable


def step(name, args):
    print()
    print("=== %s ===" % name, flush=True)
    r = subprocess.run([PY, "-X", "utf8"] + args, cwd=ROOT)
    if r.returncode != 0:
        print("ECHEC a l'etape: %s" % name)
        sys.exit(r.returncode)


def main():
    dry = "--dry" in sys.argv
    local = "--local" in sys.argv

    step("export du payload", ["export_ui.py"])
    step("assemblage du site", ["build_site.py"])

    if local:
        step("serveur local", ["build_site.py", "--serve"])
        return 0
    if dry:
        print()
        print("--dry: rien n'a ete pousse. Le site est dans site/.")
        return 0

    step("deploiement", ["deploy.py"])
    print()
    print("https://diggerfm.github.io/Diggerfm/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
