"""Config loading. Secrets live in config.toml, which is gitignored."""

import os

try:
    import tomllib
except ImportError:
    import tomli as tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.toml")


def load(path=None):
    path = path or CONFIG_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(
            "missing %s. Copy config.example.toml and fill it in." % path)
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def beatport_credentials(cfg=None):
    cfg = cfg or load()
    bp = cfg.get("beatport", {})
    if not bp.get("username") or not bp.get("password"):
        raise ValueError("beatport.username / beatport.password not set in config.toml")
    return bp["username"], bp["password"]
