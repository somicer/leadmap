# leadmap — Google Maps lead collector for real estate agencies (املاک)

A slow, resumable, polite collector. It splits a city into tiles and searches Google Maps for
"املاک" / "مشاور املاک" on each tile. Places go into SQLite, and an Excel export runs daily.

## Install on a new server (one command)

On the current server, build a portable package. It excludes the database, the browser profile, `config.yaml` (which contains the password), logs and the venv:
```bash
cd /root/map && ./make-package.sh          # → /root/leadmap-YYYYMMDD.tgz
scp /root/leadmap-*.tgz root@NEW_SERVER:/root/
```
On the new server (Ubuntu/Debian, 2+ GB RAM, 3+ GB free disk, non-Iranian IP):
```bash
cd /root && tar xzf leadmap-*.tgz && cd leadmap
./install.sh                                   # asks for city + your e-mail
# or non-interactive:
./install.sh --yes --city "شیراز" --contact you@gmail.com
# with a real domain certificate for the dashboard:
./install.sh --city "شیراز" --contact you@gmail.com --domain my.domain.com \
             --cert /path/fullchain.pem --key /path/privkey.pem
```
The installer:
- checks disk and RAM
- installs system packages and Python ≥ 3.11 (via uv on older distros such as Ubuntu 22.04)
- installs the pinned `requirements.txt` and Chromium, then runs the tests
- writes `config.yaml` with a random dashboard password
- builds the city grid
- installs and starts both systemd services

It prints the dashboard URL and login at the end. Without `--cert`, it makes a self-signed certificate, and the browser warns once. It is safe to re-run: the config, database and profile are kept, and `--reconfigure` rewrites the config. Run `./install.sh --help` for all options.

**Use a real e-mail for `--contact`.** OpenStreetMap returns 403 for fake or example addresses. **Run each city on one server only.** To split cities across servers automatically, use the central panel (below).

All settings are in `config.yaml`: paths, grid, queries, pacing, blocks/backoff, retries and export time.

## Central panel: many servers, one queue of cities

One server (here: this one) runs the **hub**, a page at `https://<dashboard host>:8880/hub` with the same login as the dashboard. It works like this:
- You add cities to a queue there. Each worker server claims one city, builds that city's grid, scrapes it and reports it finished. Then it claims the next city in the queue.
- Workers report progress and upload new places in a heartbeat every 60 s. The hub keeps a merged copy of all places in `data/hub.db`, deduplicated by place id. Its Excel files go to `exports/hub/`: `all_leads.xlsx` with a city column, one `city_<name>.xlsx` per city, and a daily `leads_YYYY-MM-DD.xlsx`. The daily export runs at 23:30 Tehran time or when you click the button.
- If a server already has local progress on a queued city, it resumes that city first.
- If the hub is down, workers keep scraping their current city and upload the backlog later. Nothing is lost.
- Panel actions:
  - reorder the queue
  - **release** an active city back to the queue: the worker drops it and the next server starts that city from scratch
  - **re-queue** a finished city, so its failed tiles are retried
  - pause or resume a worker
  - revoke a worker's token: its city goes back to the queue

Make a server the hub: `./install.sh --hub-server` (or set `hub.server: true` in `config.yaml` and restart `leadmap-dashboard`).

Add a worker server:
1. In the panel, type a name and click «ساخت توکن». Copy the command it shows; the token is shown only once.
2. On the new server, after unpacking `make-package.sh`'s tgz:
   ```bash
   ./install.sh --hub https://panel.example.com:8880 --hub-token <TOKEN> --contact you@gmail.com
   # add --hub-insecure only if the hub uses a self-signed certificate
   ```
   The installer checks the connection with `python main.py hub ping` and fails loudly if it can't reach the hub. No `--city` is needed. On an existing standalone server, the same flags add just the `hub:` section to its `config.yaml`.

With `hub.url` and `hub.token` set, `run` takes its cities from the hub. `run --city X` still scrapes a fixed city. CLI helpers on the hub server: `python main.py hub add-city شیراز مشهد`, `hub add-worker NAME`, `hub status`.

## CLI

| command | what it does |
|---|---|
| `python main.py init --city "کرج"` | fetch the OSM boundary (cached) and build the snake-ordered grid; safe to re-run |
| `python main.py test-tile [--tile ID] [--headed]` | scrape one tile (default: the most central one), print results + mobile ratio, dump raw responses to `debug/test_tile_*/` |
| `python main.py run [--city X] [--headed]` | long-running scraper; resumes from the first pending tile |
| `python main.py status` | tile counts, places, mobiles, backoff state, last events |
| `python main.py export` | run the Excel export now |
| `python main.py retry-failed` | put failed tiles back to pending |
| `python main.py login` | headed browser on `data/profile` to log into Google once (only with `use_profile_login: true`) |

## How it works

- **Grid**: the city polygon comes from Nominatim (custom User-Agent, ≤1 req/s, cached in `data/cache/`). If there is no polygon it falls back to the bounding box and logs a warning. Tiles are 800 m squares in UTM, and only those intersecting the polygon are kept. They are ordered row by row, alternating direction. Tile ids are `city:row:col`, so re-running `init` never duplicates them.
- **Extraction**: the collector opens `https://www.google.com/maps/search/<q>/@lat,lng,16z?hl=fa&gl=ir` and intercepts the `/search?tbm=map` responses. It parses them in `leadmap/parser.py`, which documents the verified field layout and is tested on real captured responses in `tests/fixtures/` (anonymized: names, phones, addresses and ids are fake). The feed's DOM cards are the fallback, merged by place id. It scrolls gradually until the end-of-list marker, or until N scrolls bring nothing new.
- **Detail pages**: a place with no phone in the list data gets its detail page opened once (`details_checked_at`), in a separate tab. This is subject to the hourly cap.
- **Filter**: a place is kept if it lies inside the tile's padded bounds or inside the city polygon. Places are upserted by place id, and `last_seen` is updated.
- **Phones**: Persian/Arabic digits → Latin, `+98`/`0098`/`98` → `0`, `is_mobile = ^09\d{9}$`. Both the raw and normalized numbers are stored.
- **Pacing**: 4–10 s between scroll steps, with occasional 20–60 s pauses. There is a 2–6 min pause between tiles and a 20–40 min break every 8–12 tiles. Hourly caps apply to tiles and detail opens. The active window is 08:00–01:00 Tehran time. One persistent browser session is reused and restarted every 3 h.
- **Blocks**: a block is any of: a `/sorry/` URL, "unusual traffic" text, a reCAPTCHA iframe, HTTP 429, or 3 empty tiles in a row next to productive tiles. On a block, a screenshot and the HTML go to `debug/blocks/`, the browser closes, and the tile goes back to pending. Backoff is 3h → 6h → 12h → 24h, persisted in the DB, and resets after a successful tile. **CAPTCHAs are never solved or bypassed.**
- **Errors**: a tile is retried up to 3 times with growing delays, then marked `failed` (see `retry-failed`). Every error is caught at tile level.
- **Consent page**: EU server IPs get Google's cookie-consent interstitial. The collector clicks "رد کردن همه" (Reject all) once, and the profile keeps the cookie.
- **Export**: runs daily at 23:30 Tehran time inside the service, producing:
  - `exports/leads_YYYY-MM-DD.xlsx`: places first seen that day. The file is skipped, and a log line written, if there are none.
  - `exports/all_leads.xlsx`: cumulative, overwritten each time.

  Both files have the sheets "موبایل" and "همه", RTL layout, a bold frozen header and auto widths.
  `examples/sample_leads.xlsx` shows the format: 10 rows with fake names, numbers and addresses. Real lead lists are never committed (`.gitignore`).
- **Logs**: `logs/run.log` (rotating, 5×5 MB), with one progress line per tile. `debug/` is pruned to `debug_max_mb`.

## Running as a service (systemd)

```bash
cp deploy/leadmap.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now leadmap
systemctl status leadmap
tail -f logs/run.log
systemctl stop leadmap        # graceful: current tile stays pending
```

## Status dashboard

This is a read-only HTTPS page with basic auth, served on a separate port, so x-ui is untouched. It shows:
- progress per city, places and mobiles
- backoff state and recent tiles and events
- a live log tail
- Excel downloads and an "export now" button

It uses only the Python standard library. It never calls Claude, so it costs no tokens.

```bash
cp deploy/leadmap-dashboard.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now leadmap-dashboard
# → https://panel.example.com:8880  (user/password: config.yaml → dashboard)
```

It reuses the certificate set in `config.yaml → dashboard.cert/key` (e.g. from acme.sh) and reloads it automatically after renewal. After 10 failed logins, an IP is locked out for 10 minutes. `config.yaml` holds the password and is chmod 600.

## Alternative: tmux

```bash
tmux new -s leadmap
cd /root/map && .venv/bin/python main.py run
# detach: Ctrl-b d      reattach: tmux attach -t leadmap      stop: Ctrl-c (graceful)
```

## Headed mode / manual login

Only needed for debugging, or if you set `browser.use_profile_login: true`:

```bash
xvfb-run -a .venv/bin/python main.py test-tile --headed     # headed but invisible (debugging)
# To log in you need to *see* the window: use ssh -X, or VNC, then:
.venv/bin/python main.py login
```

Without login, a separate profile `data/profile_anon` is used, so a logged-in profile is never mixed in.

## Files

```
config.yaml           settings
main.py               CLI
leadmap/grid.py       Nominatim + tiling      leadmap/phone.py    phone normalization
leadmap/parser.py     response/DOM parsing    leadmap/scrape.py   one tile / query / detail page
leadmap/pacing.py     delays, caps, window    leadmap/service.py  main loop, backoff, test-tile
leadmap/export.py     Excel                   leadmap/db.py       SQLite (WAL)
leadmap/hub.py        central queue + merged places (hub.html = panel)
leadmap/hubclient.py  worker side: claim / heartbeat / upload / complete
data/leads.db  exports/  logs/  debug/  deploy/leadmap.service
```

## Operational notes

- The disk on this server is nearly full. `debug/` is capped, logs rotate, and each Excel file is small. Watch `df -h /`.
- If Google changes its response format, the parser raises a `ParseError`, and the raw body is saved to `debug/errors/`. Meanwhile the DOM fallback keeps collecting names, coordinates and list-visible phones. Re-inspect the dump, then update the index map at the top of `parser.py`.
