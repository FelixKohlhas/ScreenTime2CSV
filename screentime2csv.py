"""
screentime2csv.py — export macOS and iPhone Screen Time / app usage to CSV.

Two data sources are queried and concatenated into a single CSV:

1. knowledgeC.db (`/app/usage` stream)
   — This Mac's local foreground app usage. Schema and join from
     https://rud.is/b/2019/10/28/spelunking-macos-screentime-app-usage-with-r/.
     On macOS 14+ this store no longer receives synced iPhone events, so it
     is Mac-only in practice.

2. Biome (~/Library/Biome/streams/restricted/App.InFocus/remote/<peer-uuid>/)
   — Apple moved synced iPhone app-usage here on macOS 14+. The format is
     binary SEGB pages containing protobuf records. We locate events by the
     protobuf pattern `field 4 (fixed64 timestamp) + field 6 (string bundle)`
     without needing Apple's private .proto schema. Peer UUIDs are resolved
     to device models via ~/Library/Biome/sync/sync.db (DevicePeer table).

Output schema (one row per foreground session, both sources):
    app, usage, start_time, end_time, created_at, tz, device_id, device_model

The script overwrites the output file on every run and pulls everything
matching --since (default: all time). The knowledgeC half used to be
incremental-append; we dropped that because the Biome half has to re-derive
sessions from sorted events and mixing modes in one file was confusing.

Requirements: Full Disk Access for Terminal / your Python interpreter
(System Settings → Privacy & Security → Full Disk Access).
"""

import argparse
import csv
import glob
import os
import re
import sqlite3
import struct
import sys
import time
from collections import defaultdict

# ---------- constants ----------

KNOWLEDGE_DB = os.path.expanduser("~/Library/Application Support/Knowledge/knowledgeC.db")
BIOME_BASE = os.path.expanduser("~/Library/Biome/streams/restricted")
BIOME_SYNC_DB = os.path.expanduser("~/Library/Biome/sync/sync.db")

CFA_EPOCH = 978307200  # 2001-01-01 UTC — CFAbsoluteTime -> unix
DEFAULT_BIOME_STREAM = "App.InFocus"
DEFAULT_MAX_GAP = 300  # seconds; cap duration between consecutive Biome events

CSV_FIELDS = [
    "app", "usage", "start_time", "end_time",
    "created_at", "tz", "device_id", "device_model",
]


# ---------- knowledgeC.db (Mac local) ----------

def query_knowledgec(since_ts):
    """
    Yield rows matching CSV_FIELDS from knowledgeC.db /app/usage. Rows older
    than `since_ts` (unix) are filtered out.
    """
    if not os.path.exists(KNOWLEDGE_DB):
        print(f"warn: {KNOWLEDGE_DB} not found, skipping knowledgeC", file=sys.stderr)
        return
    if not os.access(KNOWLEDGE_DB, os.R_OK):
        print(f"warn: {KNOWLEDGE_DB} is not readable — grant Full Disk Access "
              f"to your terminal/Python and retry", file=sys.stderr)
        return

    # Original query from rud.is (2019). Joins ZSYNCPEER so synced peers would
    # populate device_id/device_model if Apple still wrote them here — on
    # macOS 14+ they don't, so this returns local rows only in practice.
    query = """
    SELECT
        ZOBJECT.ZVALUESTRING                       AS app,
        (ZOBJECT.ZENDDATE - ZOBJECT.ZSTARTDATE)    AS usage,
        (ZOBJECT.ZSTARTDATE + 978307200)           AS start_time,
        (ZOBJECT.ZENDDATE   + 978307200)           AS end_time,
        (ZOBJECT.ZCREATIONDATE + 978307200)        AS created_at,
        ZOBJECT.ZSECONDSFROMGMT                    AS tz,
        ZSOURCE.ZDEVICEID                          AS device_id,
        ZSYNCPEER.ZMODEL                           AS device_model
    FROM ZOBJECT
    LEFT JOIN ZSTRUCTUREDMETADATA
        ON ZOBJECT.ZSTRUCTUREDMETADATA = ZSTRUCTUREDMETADATA.Z_PK
    LEFT JOIN ZSOURCE
        ON ZOBJECT.ZSOURCE = ZSOURCE.Z_PK
    LEFT JOIN ZSYNCPEER
        ON ZSOURCE.ZDEVICEID = ZSYNCPEER.ZDEVICEID
    WHERE ZSTREAMNAME = "/app/usage"
      AND (ZOBJECT.ZSTARTDATE + 978307200) >= ?
    ORDER BY ZOBJECT.ZSTARTDATE ASC
    """
    with sqlite3.connect(f"file:{KNOWLEDGE_DB}?mode=ro", uri=True) as con:
        for row in con.execute(query, (since_ts,)):
            app, usage, start, end, created, tz, dev_id, dev_model = row
            yield [
                app or "",
                int(round(usage or 0)),
                int(round(start or 0)),
                int(round(end or 0)),
                f"{(created or 0):.6f}",
                int(tz) if tz is not None else "",
                dev_id or "",
                dev_model or "Mac (local)",
            ]


# ---------- Biome (iPhone + other synced peers) ----------

def load_biome_peers():
    """Return {peer_uuid: (model, platform)} from Biome sync.db."""
    if not os.path.exists(BIOME_SYNC_DB):
        return {}
    peers = {}
    try:
        con = sqlite3.connect(f"file:{BIOME_SYNC_DB}?mode=ro", uri=True)
        for uuid, model, platform in con.execute(
            "SELECT device_identifier, model, platform FROM DevicePeer"
        ):
            peers[uuid] = (model or "", platform)
        con.close()
    except sqlite3.Error as e:
        print(f"warn: could not read {BIOME_SYNC_DB}: {e}", file=sys.stderr)
    return peers


def biome_device_label(platform, model):
    """Human label for the platform integer stored in DevicePeer.
    Observed: 2 = iOS, 3 = macOS. Fall back to build-number/model."""
    if platform == 2:
        return f"iPhone ({model})" if model else "iPhone"
    if platform == 3:
        return f"Mac ({model})" if model else "Mac"
    return model or f"platform-{platform}"


# field 4 = fixed64 start timestamp, field 6 = length-delimited bundle id
_SEGB_EVENT_RE = re.compile(rb"\x21(.{8})\x32([\x01-\x7f])", re.DOTALL)


def parse_segb_page(path):
    """
    Yield (ts_unix, bundle_id) pairs from one SEGB page.
    Robust against the SEGB record framing we don't fully decode — every
    complete event contains the `0x21 <8-byte double> 0x32 <len> <bundle>`
    protobuf subsequence.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return
    if data[:4] != b"SEGB":
        return
    for m in _SEGB_EVENT_RE.finditer(data):
        ts_bytes = m.group(1)
        name_len = m.group(2)[0]
        name_start = m.end()
        if name_start + name_len > len(data):
            continue
        name = data[name_start:name_start + name_len]
        if b"." not in name[:20]:
            continue
        try:
            ts_cfa = struct.unpack("<d", ts_bytes)[0]
        except struct.error:
            continue
        # Sanity: CFAbsoluteTime roughly in 2022..2030
        if not (6.5e8 < ts_cfa < 9.5e8):
            continue
        try:
            bundle = name.decode("utf-8")
        except UnicodeDecodeError:
            continue
        yield (ts_cfa + CFA_EPOCH, bundle)


def collect_biome_events(stream, since_ts, include_local):
    """
    Walk a Biome stream and return {peer_uuid_or_None: sorted [(ts, bundle)]}.
    `None` is this Mac's local stream and is skipped unless include_local=True.
    """
    root = os.path.join(BIOME_BASE, stream)
    if not os.path.isdir(root):
        print(f"warn: {root} not found, skipping Biome", file=sys.stderr)
        return {}
    by_peer = defaultdict(list)

    if include_local:
        local_dir = os.path.join(root, "local")
        if os.path.isdir(local_dir):
            for f in sorted(glob.glob(os.path.join(local_dir, "*"))):
                if os.path.isfile(f):
                    for ts, bundle in parse_segb_page(f):
                        if ts >= since_ts:
                            by_peer[None].append((ts, bundle))

    remote_dir = os.path.join(root, "remote")
    if os.path.isdir(remote_dir):
        for peer in sorted(os.listdir(remote_dir)):
            peer_path = os.path.join(remote_dir, peer)
            if not os.path.isdir(peer_path):
                continue
            for f in sorted(glob.glob(os.path.join(peer_path, "*"))):
                if os.path.isfile(f):
                    for ts, bundle in parse_segb_page(f):
                        if ts >= since_ts:
                            by_peer[peer].append((ts, bundle))

    for peer in by_peer:
        by_peer[peer].sort(key=lambda x: x[0])
    return by_peer


def biome_events_to_rows(by_peer, peers_meta, max_gap, tz_offset, now):
    """
    Merge consecutive same-app events per peer into sessions and emit CSV
    rows. Each closed session's end time is extended toward the next event
    in the stream (capped at max_gap seconds) so short events followed by
    a gap still register as active use. The very last session in the
    stream has no successor and just spans its observed events.
    """
    for peer, events in by_peer.items():
        if not events:
            continue
        model, platform = peers_meta.get(peer or "", ("", None))
        if peer is None:
            device_model = "Mac (local)"
            device_id = ""
        else:
            device_model = biome_device_label(platform, model)
            device_id = peer

        cur_bundle = None
        cur_start = None
        cur_last = None
        for ts, bundle in events:
            if cur_bundle is None:
                cur_bundle, cur_start, cur_last = bundle, ts, ts
                continue
            if bundle == cur_bundle and (ts - cur_last) <= max_gap:
                cur_last = ts
                continue
            # Close previous session, extending end toward the next event.
            end = cur_last + min(max_gap, ts - cur_last)
            yield _biome_row(cur_bundle, cur_start, end,
                             now, tz_offset, device_id, device_model)
            cur_bundle, cur_start, cur_last = bundle, ts, ts
        if cur_bundle is not None:
            yield _biome_row(cur_bundle, cur_start, cur_last,
                             now, tz_offset, device_id, device_model)


def _biome_row(bundle, start, end, now, tz, device_id, device_model):
    return [
        bundle,
        max(int(round(end - start)), 0),
        int(round(start)),
        int(round(end)),
        f"{now:.6f}",
        tz,
        device_id,
        device_model,
    ]


# ---------- CLI ----------

def parse_since(s):
    """Accept '7d', '24h', '30m', a unix epoch, or None (== all time)."""
    if s is None:
        return 0.0
    s = s.strip()
    if s.endswith("d"):
        return time.time() - int(s[:-1]) * 86400
    if s.endswith("h"):
        return time.time() - int(s[:-1]) * 3600
    if s.endswith("m"):
        return time.time() - int(s[:-1]) * 60
    return float(s)


def current_tz_offset():
    """Seconds east of UTC, matching knowledgeC's ZSECONDSFROMGMT."""
    return -time.altzone if time.daylight else -time.timezone


def main():
    ap = argparse.ArgumentParser(
        description="Export Mac + iPhone Screen Time app usage to CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Sources (both on by default):\n"
            "  knowledgeC    ~/Library/Application Support/Knowledge/knowledgeC.db\n"
            "  Biome         ~/Library/Biome/streams/restricted/<stream>/remote/\n"
            "\n"
            "Both sources require Full Disk Access on macOS 14+."
        ),
    )
    ap.add_argument("-o", "--output", default="output.csv",
                    help="CSV output path (default: output.csv)")
    ap.add_argument("-d", "--delimiter", default=",", help="CSV delimiter")
    ap.add_argument("--since", default=None,
                    help="Only include events newer than: '7d', '24h', '30m', "
                         "or a unix epoch. Default: all time.")
    ap.add_argument("--no-knowledge", action="store_true",
                    help="Skip the knowledgeC.db (Mac-local) source")
    ap.add_argument("--no-biome", action="store_true",
                    help="Skip the Biome (iPhone + synced peers) source")
    ap.add_argument("--biome-stream", default=DEFAULT_BIOME_STREAM,
                    help=f"Biome stream name (default: {DEFAULT_BIOME_STREAM}); "
                         "try ScreenTime.AppUsage if App.InFocus is empty")
    ap.add_argument("--biome-max-gap", type=int, default=DEFAULT_MAX_GAP,
                    help=f"Seconds between Biome events to still count as the "
                         f"same session (default: {DEFAULT_MAX_GAP})")
    ap.add_argument("--include-biome-local", action="store_true",
                    help="Also include this Mac's local Biome stream "
                         "(usually redundant with knowledgeC)")
    ap.add_argument("--summary", action="store_true",
                    help="Print per-device top-apps summary to stderr after writing")
    args = ap.parse_args()

    delimiter = args.delimiter.replace("\\t", "\t")
    since_ts = parse_since(args.since)
    tz_offset = current_tz_offset()
    now = time.time()

    rows = []

    if not args.no_knowledge:
        rows.extend(query_knowledgec(since_ts))

    if not args.no_biome:
        peers_meta = load_biome_peers()
        by_peer = collect_biome_events(
            args.biome_stream, since_ts, args.include_biome_local
        )
        rows.extend(biome_events_to_rows(
            by_peer, peers_meta, args.biome_max_gap, tz_offset, now
        ))

    # Sort chronologically so Mac + iPhone rows interleave naturally.
    rows.sort(key=lambda r: r[2])

    with open(args.output, "w", newline="") as f:
        w = csv.writer(f, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
        w.writerow(CSV_FIELDS)
        w.writerows(rows)

    print(f"wrote {len(rows)} rows to {args.output}", file=sys.stderr)

    if args.summary:
        from collections import Counter
        per_dev = Counter()
        per_dev_secs = defaultdict(float)
        per_app_secs = defaultdict(lambda: defaultdict(float))
        for r in rows:
            per_dev[r[7]] += 1
            per_dev_secs[r[7]] += r[1]
            per_app_secs[r[7]][r[0]] += r[1]
        for dev, n in per_dev.most_common():
            total_h = per_dev_secs[dev] / 3600
            print(f"\n[{dev}] {n} sessions, {total_h:.1f}h", file=sys.stderr)
            top = sorted(per_app_secs[dev].items(), key=lambda x: -x[1])[:10]
            for app, secs in top:
                print(f"  {secs/60:6.0f}m  {app}", file=sys.stderr)


if __name__ == "__main__":
    main()
