# ScreenTime2CSV

ScreenTime2CSV is a Python script that exports macOS + iPhone Screen Time / app usage to CSV. It reads two sources and writes one unified CSV:

1. **`knowledgeC.db`** — `~/Library/Application Support/Knowledge/knowledgeC.db`, the legacy `/app/usage` stream. On macOS 14+ this only contains Mac-local rows.
2. **Biome** — `~/Library/Biome/streams/restricted/App.InFocus/remote/<peer-uuid>/`, a binary SEGB + protobuf telemetry store. On macOS 14+ Apple moved synced iPhone Screen Time here, so this is where iPhone (and other synced peer) app usage now lives.

Peer UUIDs are resolved to device models via `~/Library/Biome/sync/sync.db`.

Requirements:
- Mac signed into the same iCloud account as the iPhone (for iPhone data)
- Screen Time → Share Across Devices enabled on both (for iPhone data)
- Terminal/your Python interpreter has Full Disk Access (System Settings → Privacy & Security → Full Disk Access)

Background on the knowledgeC side: [Exporting and analyzing iOS Screen Time usage](https://felixkohlhas.com/projects/screentime/). Background on the Biome side: [rud.is — spelunking macOS Screen Time](https://rud.is/b/2019/10/28/spelunking-macos-screentime-app-usage-with-r/).

## Usage

```
usage: screentime2csv.py [-h] [-o OUTPUT] [-d DELIMITER] [--since SINCE]
                         [--no-knowledge] [--no-biome]
                         [--biome-stream BIOME_STREAM]
                         [--biome-max-gap BIOME_MAX_GAP]
                         [--include-biome-local] [--summary]
```

Output schema (one row per foreground session, both sources):

```
app, usage, start_time, end_time, created_at, tz, device_id, device_model
```

### Examples

```bash
# Export everything (Mac + iPhone) to output.csv
python3 screentime2csv.py -o output.csv
```

```bash
# Just the past 7 days, with a per-device top-apps summary
python3 screentime2csv.py -o output.csv --since 7d --summary
```

```bash
# Mac only (skip Biome)
python3 screentime2csv.py --no-biome -o output.csv
```

```bash
# iPhone only (skip knowledgeC)
python3 screentime2csv.py --no-knowledge -o output_iphone.csv
```

```bash
# TSV instead of CSV
python3 screentime2csv.py -o output.tsv -d '\t'
```

### Useful flags

- `-o / --output` — output file path (default: `output.csv`)
- `-d / --delimiter` — CSV delimiter (default: `,`)
- `--since` — only include events newer than `7d` / `24h` / `30m` / a unix epoch (default: all time)
- `--no-knowledge` — skip the knowledgeC.db (Mac-local) source
- `--no-biome` — skip the Biome (iPhone + synced peers) source
- `--biome-stream` — Biome stream name (default: `App.InFocus`); try `ScreenTime.AppUsage` if `App.InFocus` is empty
- `--biome-max-gap` — seconds between Biome events to still count as one session (default: `300`)
- `--include-biome-local` — also pull this Mac's local Biome stream (usually redundant with knowledgeC)
- `--summary` — print per-device top-apps to stderr after writing