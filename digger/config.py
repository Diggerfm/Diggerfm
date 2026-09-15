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


def discogs_credentials(cfg=None):
    """Token and User-Agent. The token is optional: without it the API still
    answers, at 25 requests a minute instead of 60."""
    cfg = cfg or load()
    d = cfg.get("discogs", {})
    return (d.get("token") or None,
            d.get("user_agent")
            or "DiggerFM/0.1 +https://diggerfm.github.io/Diggerfm/")
