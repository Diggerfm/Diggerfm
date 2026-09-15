# digger

Weekly track radar for a DJ. Collects new releases, DJ charts, label
rosters and the tracklists of real sets, dedupes across sources, scores
against the library he actually plays, and proposes a short list under an
imposed slot quota.

Python 3.10+, SQLite, no server.

## Install

    python -m venv .venv
    .venv/Scripts/python -m pip install -r requirements.txt
    cp config.example.toml config.toml   # then fill in the Beatport account

Install into the venv, not globally: shazamio pulls numpy 2.x, which breaks
scipy and tensorflow pinned below 2.0 elsewhere on this machine.

`config.toml` holds secrets and is gitignored.

## Use

    python cli.py profile "C:/path/to/rekordbox.xml"   # import the library
    python cli.py analyze --profile -v                 # timbre of his own files
    python cli.py collect all --charts 120             # fill the radar
    python cli.py fingerprint "https://youtu.be/..."   # what a DJ really played
    python cli.py digest --days 10 --export digest.md  # the weekly short list
    python cli.py feedback --import digest.md          # his verdicts back in
    python cli.py feedback --learn                     # re-derive the weights

`weekly.py` runs the whole chain; `ecosystem.config.cjs` schedules it for
Friday 10:00 under PM2, ahead of preparing the weekend's music.

## Sources, and what each is actually worth

Measured live on 2026-09-15, not assumed.

| Source | Access | Yield | Verdict |
|---|---|---|---|
| Beatport charts | OAuth via the public client ID scraped from the v4 docs bundle, plus a real account | 354 tracks off 25 charts in 12 s, **100 % carry ISRC, BPM and key** | The backbone. 10 000 charts, the freshest hours old. |
| Beatport labels | Same client | 16 tracks off 6 labels, including one not yet released | Catches records before any DJ charts them. |
| Bandcamp | Undocumented discover endpoint, payload p and s | 309 tracks, **100 % carry an mp3-128 stream URL** | Second source, and the only one handing over audio for free. |
| Set fingerprinting | yt-dlp plus Shazam through shazamio, no key | 14 tracks off a 74 minute Boiler Room, 53 % of probes matched | What a DJ played, not what he claims to support. |
| Mixcloud | Public API, no auth | **0 tracklists out of 20 sets sampled** | Dead for tracks. The sections field exists in the schema and is never filled. |
| 1001Tracklists | No official API | not integrated | The available scraper lists captcha as an unsolved task. Fingerprinting replaces it. |
| SoundCloud | Client Credentials, 99 USD/year Artist Pro | not integrated | Search works and is unmetered, but bpm is uploader-supplied and usually null, and invalid_client on the documented flow is an open bug. Revisit. |
| Chartmetric / Soundcharts | 350 and 250 USD per month | not integrated | They sell artist career tracking. He needs club track tracking. Wrong product, before the price. |

Beatport killed API v3 and no longer grants developer credentials the normal
way, so there is no application to send. The client ID is scraped from their
own docs bundle. This works today and can break whenever they rebuild it.

## Two findings that reshaped the design

**Cross-source agreement is not the core signal.** The plan assumed a track
appearing on several sources at once was the thing to rank on. Measured,
Bandcamp and Beatport overlapped on 1 work out of 644, which is 0.16 %. They
are different economies and barely intersect. What does exist is inside
Beatport: 8 tracks out of 345 were charted by more than one DJ off only 25
charts out of 10 000 available. So corroboration counts distinct DJ charts,
and cross-source agreement is a small bonus. See `score.corroboration`.

**Local key detection is not usable and local BPM only half is.** Validated
against Beatport ground truth on 12 tracks: key scored 5/12 even counting
enharmonic spellings as correct, and BPM was right to within 3 above 0.90
confidence and wrong every single time below it. Beatport supplies both
authoritatively. So the audio layer surfaces BPM only above the confidence
floor, never presents a key, and earns its keep on timbre instead.

## Layout

    digger/
      normalize.py     track identity, the piece everything else rests on
      db.py            SQLite schema, sightings are append-only evidence
      score.py         affinity + corroboration + the imposed slot quota
      features.py      timbre computed locally with librosa
      feedback.py      his verdicts, and re-deriving the weights from them
      config.py        secrets, read from the gitignored config.toml
      sources/
        beatport.py    OAuth flow, DJ charts, label rosters
        bandcamp.py    discover endpoint, then release drill-down
        mixcloud.py    kept, measured, currently worthless for tracks
      fingerprint/
        audio.py       fetch a set, cut it into probes
        identify.py    Shazam recognition
        sets.py        collapse probes into plays
      profile/
        rekordbox.py   XML import: the ADN, and the set functions
    cli.py
    weekly.py
    tests/

## Design notes

**Two identity keys, not one.** `work_key` is artist plus title with every
version collapsed, and answers whether this is the same record.
`track_key` adds the version, and answers whether this is the same product
to buy. A remix must never collapse into its original, an extended edit
must.

**Play count beats declared taste.** The Rekordbox export already records
what he really played. When two files share a `track_key`, the original and
the extended edit say, their plays are summed rather than one overwriting
the other. Aggregation happens in Python so a re-import stays idempotent.

**Set function, read for free.** His brief asks for tracks classified as
warm-up, peak, after, transition tool. Those are roles in a night, not
genres, and the digest slots are roles too. Rekordbox already holds this:
DJs name playlists after exactly these roles, so `profile/rekordbox.py`
reads them rather than making him tag 150 tracks. A playlist called 2024 or
Ibiza is left unclassified on purpose; guessing would poison the ADN.

**The slot quota is imposed, not derived.** A pure ranker collapses onto the
same handful of artists every week. Ten bullseye, five modernisation, three
bridge and two wildcards are reserved by construction. The bridge slot is
the interesting one: those tracks are picked because they are the way into
his set, not because they are his sound.

**Timbre weights are measured, not guessed.** Cohen's d between 124 tracks
in his genre territory and 96 deliberate negative controls: energy 0.99,
onset rate 0.57, dynamics 0.42, percussive ratio 0.26, brightness 0.02. An
earlier table had brightness at 1.1 and energy at 0.8, which was backwards.
Honest caveat: this was measured against genre, a stand-in for his taste
that the brief explicitly rejects, on n=220. `feedback.learn_weights`
re-derives them from his own verdicts and refuses to run on thin evidence.

**No compatibility percentage.** A model with no calibration has no basis
for saying 94 %. `score.explain` gives plain reasons instead.

## Known limitations

- Bandcamp sub-genre filtering does not work through the discover payload
  (techno returns the same 752 total as plain electronic), so tag filtering
  runs client-side on the release page tags.
- Fingerprinting finds nothing that was never released: a 20 minute stretch
  of the tested Boiler Room returned no match at all, almost certainly dubs
  and white labels.
- Timbre has no ground truth to validate against, unlike BPM and key. It is
  internally consistent and therefore comparable between tracks, which is
  what affinity needs, but it is plausible rather than proven until his
  feedback confirms it.
