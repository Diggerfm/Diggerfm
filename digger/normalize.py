"""Track identity normalization.

Cross-source scoring is only as good as the dedup. Two sources naming the
same record differently must collapse to the same key, while a remix must
never collapse into its original.

Two keys are produced for every track:
  work_key   artist + title, all versions collapsed. Used to detect that the
             same record showed up in several sources.
  track_key  work_key + version. Identifies the exact product to buy.
"""

import re
import unicodedata

# Version markers that mean "this is the plain record", not a distinct work.
NEUTRAL_VERSIONS = {
    "", "original mix", "original", "extended mix", "extended",
    "original version", "album version", "radio edit", "edit",
    "club mix", "full length", "main mix",
}

# Everything from these markers onward is a guest credit, not part of identity.
FEAT_RE = re.compile(
    r"\s*[\(\[]?\s*\b(?:feat|ft|featuring|w/)\b\.?\s+.*$",
    re.IGNORECASE,
)

# Trailing label or catalogue tags: "Title [Drumcode]"
BRACKET_TAIL_RE = re.compile(r"\s*\[[^\]]*\]\s*$")

# Parenthesised version suffix: "Title (Someone Remix)"
VERSION_RE = re.compile(r"\s*[\(\[]([^\)\]]*)[\)\]]\s*$")

ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|&|\bvs\.?\b|\bversus\b|\bx\b|\band\b)\s*", re.IGNORECASE)

PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
SPACE_RE = re.compile(r"\s+")


def _fold(text):
    """Lowercase, strip accents, drop punctuation, squeeze whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = text.replace("&", " and ")
    text = PUNCT_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def split_version(title):
    """Return (bare_title, version). Version is '' when the title is plain."""
    if not title:
        return "", ""
    title = BRACKET_TAIL_RE.sub("", str(title).strip())
    version = ""
    m = VERSION_RE.search(title)
    if m:
        candidate = _fold(m.group(1))
        # A parenthesis holding a guest credit is not a version.
        if candidate and not re.match(r"^(feat|ft|featuring|w)\b", candidate):
            version = candidate
            title = title[: m.start()].strip()
    return title, version


def normalize_artist(artist):
    """Fold and alphabetically sort collaborators so order never matters."""
    if not artist:
        return ""
    artist = FEAT_RE.sub("", str(artist))
    parts = [_fold(p) for p in ARTIST_SPLIT_RE.split(artist)]
    parts = [p for p in parts if p]
    return " ".join(sorted(set(parts)))


def normalize_title(title):
    bare, _ = split_version(title)
    return _fold(FEAT_RE.sub("", bare))


def normalize_version(title):
    _, version = split_version(title)
    return "" if version in NEUTRAL_VERSIONS else version


def work_key(artist, title):
    """Identity of the record, every version collapsed."""
    return "%s::%s" % (normalize_artist(artist), normalize_title(title))


def track_key(artist, title):
    """Identity of the exact product, version included."""
    return "%s::%s" % (work_key(artist, title), normalize_version(title))


def parse_combined(text):
    """Split a 'Artist - Title' string as found in tracklists."""
    if not text:
        return "", ""
    for dash in (" - ", " – ", " — ", " -- "):
        if dash in text:
            left, right = text.split(dash, 1)
            return left.strip(), right.strip()
    return "", text.strip()


def iso_date(value):
    """Normalize a publication date to ISO 8601, or None.

    Sources disagree: Beatport sends "2026-09-18", Bandcamp sends
    "31 Jul 2026 08:28:51 GMT". Stored side by side and ordered as text,
    the Bandcamp form sorts by day-of-month, so "newest first" listed the
    31st of any month above the 29th of any other and put a 2011 release
    above a 2026 one. Everything is stored ISO so a string sort is a date
    sort.
    """
    if not value:
        return None
    text = str(value).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:19]
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(text).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        pass
    for fmt in ("%d %b %Y %H:%M:%S %Z", "%d %b %Y %H:%M:%S",
                "%d %b %Y", "%Y%m%d"):
        try:
            import datetime
            return datetime.datetime.strptime(text, fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            continue
    return None
