"""Assemble the static site.

One HTML file with the payload inlined and the audio served alongside it.
Inlined rather than fetched because a `fetch` of a sibling JSON fails from
`file://`, so an inlined payload is the one form that opens identically from
disk, from a local server and from GitHub Pages. The payload is small once
the audio lives in its own files.

    python build_site.py            # write site/
    python build_site.py --serve    # write it, then serve it on :8765
"""

import http.server
import json
import os
import shutil
import socketserver
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(ROOT, "site")
TEMPLATE = os.path.join(ROOT, "ui", "template.html")
PAYLOAD = os.path.join(SITE, "payload.json")

SKELETON = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="description" content="Radar hebdomadaire de morceaux pour DJ : charts, labels, nouveautes, et sets identifies a l'oreille.">
<meta name="theme-color" content="#12100E">
%s
</head>
<body>
%s
</body>
</html>
"""


def build():
    if not os.path.exists(PAYLOAD):
        print("payload absent. Lance d'abord: python export_ui.py")
        return 1
    with open(TEMPLATE, encoding="utf-8") as fh:
        tpl = fh.read()
    with open(PAYLOAD, encoding="utf-8") as fh:
        payload = fh.read()

    # </script> inside JSON would end the tag early.
    payload = payload.replace("</", "<\\/")

    head, body = [], []
    for block in tpl.split("\n"):
        head.append(block) if block.startswith(("<title", "<link", "<style")) else None
    # Simpler and safer than line sniffing: everything up to the first
    # element that belongs in the body stays in the head.
    marker = "<header>"
    idx = tpl.index(marker)
    head_html = tpl[:idx].strip()
    body_html = tpl[idx:].replace("__PAYLOAD__", payload)

    os.makedirs(SITE, exist_ok=True)
    out = os.path.join(SITE, "index.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(SKELETON % (head_html, body_html))

    # GitHub Pages would otherwise run the output through Jekyll.
    open(os.path.join(SITE, ".nojekyll"), "w").close()

    audio_dir = os.path.join(SITE, "audio")
    n_audio = len([f for f in os.listdir(audio_dir)]) if os.path.isdir(audio_dir) else 0
    total = os.path.getsize(out)
    if os.path.isdir(audio_dir):
        total += sum(os.path.getsize(os.path.join(audio_dir, f))
                     for f in os.listdir(audio_dir))
    data = json.loads(open(PAYLOAD, encoding="utf-8").read())
    print("site/index.html  %.0f Ko" % (os.path.getsize(out) / 1024))
    print("site/audio       %d extraits" % n_audio)
    print("total            %.1f Mo" % (total / 1048576))
    print("  digest %d | sets %d | tendances %d | nouveautes %d"
          % (len(data["digest"]), len(data["sets"]),
             len(data["trends"]), len(data["fresh"])))
    return 0


def serve(port=8765):
    os.chdir(SITE)
    handler = http.server.SimpleHTTPRequestHandler
    handler.extensions_map[".webm"] = "audio/webm"
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        print("http://127.0.0.1:%d  (ctrl-c pour arreter)" % port)
        httpd.serve_forever()


if __name__ == "__main__":
    rc = build()
    if rc == 0 and "--serve" in sys.argv:
        serve()
    sys.exit(rc)
