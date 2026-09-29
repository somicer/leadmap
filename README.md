# leadmap

**Google Maps lead collector for any business segment · جمع‌آوری لید هر صنفی از گوگل‌مپ**

[English](#english) · [فارسی](#فارسی)

---

## English

A slow, resumable, polite collector of business leads from Google Maps. It splits a city into tiles and, on each tile, searches the keywords of a **segment** (صنف): for example «میوه فروشی» → میوه فروشی، میوه، میوه فروش، تره بار, or «املاک» → املاک، مشاور املاک. Any trade works; you define the segments. Places go into SQLite, and an Excel export runs daily. A central panel can split a queue of jobs (city × segment) across many servers.

### Segments

A segment is a name plus the keywords searched on every tile. Each (city, segment) pair has its own grid and progress. A shop found under two segments is a lead in both lists; a phone found under one segment is reused for the other without opening its detail page again.
- **Standalone server**: segments live in `config.yaml → search.segments`; `search.segment` is the default. Use `--segment` with `init`, `run`, `test-tile` and `status`.
- **With the central panel**: segments are managed in the panel (section «صنف‌ها»). Editing keywords there reaches the busy workers on their next heartbeat.

```yaml
search:
  segments:
    املاک: ["املاک", "مشاور املاک"]
    میوه فروشی: ["میوه فروشی", "میوه", "میوه فروش", "تره بار"]
  segment: املاک
```

### Install on a new server (one command)

On an existing server, build a portable package. It excludes the database, the browser profile, `config.yaml` (which contains the password), logs and the venv:
```bash
cd /root/map && ./make-package.sh          # → /root/leadmap-YYYYMMDD.tgz
scp /root/leadmap-*.tgz root@NEW_SERVER:/root/
```
Or clone this repository. On the new server (Ubuntu/Debian, 2+ GB RAM, 3+ GB free disk, non-Iranian IP):
```bash
cd /root && tar xzf leadmap-*.tgz && cd leadmap
./install.sh                                   # asks for city + your e-mail
# or non-interactive:
./install.sh --yes --city "شیراز" --contact you@gmail.com
# another segment (added to config.yaml and made the default):
./install.sh --yes --city "شیراز" --segment "میوه فروشی" --keywords "میوه فروشی,میوه,میوه فروش,تره بار" --contact you@gmail.com
# with a real domain certificate for the dashboard:
./install.sh --city "شیراز" --contact you@gmail.com --domain my.domain.com \
             --cert /path/fullchain.pem --key /path/privkey.pem
```
The installer:
- checks disk and RAM
- installs system packages and Python ≥ 3.11 (via uv on older distros such as Ubuntu 22.04)
- installs the pinned `requirements.txt` and Chromium, then runs the tests
- writes `config.yaml` with a random dashboard password
- builds the grid for the city and segment
- installs and starts both systemd services

It prints the dashboard URL and login at the end. Without `--cert`, it makes a self-signed certificate, and the browser warns once. It is safe to re-run: the config, database and profile are kept, and `--reconfigure` rewrites the config. Run `./install.sh --help` for all options.

**Use a real e-mail for `--contact`.** OpenStreetMap returns 403 for fake or example addresses. **Run each (city, segment) on one server only.** To split the work across servers automatically, use the central panel (below).

All settings are in `config.yaml`: paths, grid, queries, pacing, blocks/backoff, retries and export time.

### Central panel: many servers, one queue of jobs

One server runs the **hub**, a page at `https://<dashboard host>:8880/hub` with the same login as the dashboard. It works like this:
- You define segments and their keywords there, then queue jobs: pick one or more segments and type a list of cities; each city × segment becomes one job. Each worker server claims one job, builds that city's grid for the segment, scrapes it with the segment's keywords and reports it finished. Then it claims the next job in the queue.
- Workers report progress and upload new places in a heartbeat every 60 s. The hub keeps a merged copy of all places in `data/hub.db`, deduplicated per place and segment. Its Excel files go to `exports/hub/`: `all_leads.xlsx` with segment and city columns, one `<segment>__<city>.xlsx` per job, and a daily `leads_YYYY-MM-DD.xlsx`. The daily export runs at 23:30 Tehran time or when you click the button.
- If a server already has local progress on a queued job, it resumes that job first.
- If the hub is down, workers keep scraping their current job and upload the backlog later. Nothing is lost.
- Panel actions:
  - add, edit or delete segments (a segment with queued or active jobs can't be deleted)
  - reorder the queue
  - **release** an active job back to the queue: the worker drops it and the next server starts it from scratch
  - **re-queue** a finished job, so its failed tiles are retried
  - pause or resume a worker
  - revoke a worker's token: its job goes back to the queue

Make a server the hub: `./install.sh --hub-server` (or set `hub.server: true` in `config.yaml` and restart `leadmap-dashboard`).

Add a worker server:
1. In the panel, type a name and click «ساخت توکن» (create token). Copy the command it shows; the token is shown only once.
2. On the new server, after unpacking the package:
   ```bash
   ./install.sh --hub https://panel.example.com:8880 --hub-token <TOKEN> --contact you@gmail.com
   # add --hub-insecure only if the hub uses a self-signed certificate
   ```
   The installer checks the connection with `python main.py hub ping` and fails loudly if it can't reach the hub. No `--city` is needed. On an existing standalone server, the same flags add just the `hub:` section to its `config.yaml`.

With `hub.url` and `hub.token` set, `run` takes its jobs from the hub. `run --city X --segment Y` still scrapes a fixed job. CLI helpers on the hub server: `python main.py hub add-segment "میوه فروشی" "میوه فروشی" میوه "تره بار"`, `hub add-city --segment "میوه فروشی" شیراز مشهد`, `hub add-worker NAME`, `hub status`.

### CLI

| command | what it does |
|---|---|
| `python main.py init --city "کرج" [--segment S]` | fetch the OSM boundary (cached) and build the snake-ordered grid for the city and segment; safe to re-run |
| `python main.py test-tile [--city X] [--segment S] [--tile ID] [--headed]` | scrape one tile (default: the most central one), print results + mobile ratio, dump raw responses to `debug/test_tile_*/` |
| `python main.py run [--city X] [--segment S] [--headed]` | long-running scraper; resumes from the first pending tile |
| `python main.py status [--city X] [--segment S]` | per city and segment: tile counts, places, mobiles; backoff state, last events |
| `python main.py export` | run the Excel export now |
| `python main.py retry-failed [--city X] [--segment S]` | put failed tiles back to pending |
| `python main.py login` | headed browser on `data/profile` to log into Google once (only with `use_profile_login: true`) |
| `python main.py hub add-segment / add-city / add-worker / status / ping` | central panel helpers (see above) |

### How it works

- **Grid**: the city polygon comes from Nominatim (custom User-Agent, ≤1 req/s, cached in `data/cache/`). If there is no polygon it falls back to the bounding box and logs a warning. Tiles are 800 m squares in UTM, and only those intersecting the polygon are kept. They are ordered row by row, alternating direction. Tile ids are `segment/city:row:col`, so re-running `init` never duplicates them.
- **Extraction**: for each keyword of the segment, the collector opens `https://www.google.com/maps/search/<q>/@lat,lng,16z?hl=fa&gl=ir` and intercepts the `/search?tbm=map` responses. It parses them in `leadmap/parser.py`, which documents the verified field layout and is tested on real captured responses in `tests/fixtures/` (anonymized: names, phones, addresses and ids are fake). The feed's DOM cards are the fallback, merged by place id. It scrolls gradually until the end-of-list marker, or until N scrolls bring nothing new.
- **Detail pages**: a place with no phone in the list data gets its detail page opened once (`details_checked_at`), in a separate tab. This is subject to the hourly cap.
- **Filter**: a place is kept if it lies inside the tile's padded bounds or inside the city polygon. Places are upserted by place id and segment, and `last_seen` is updated. Google's own category is kept in the `category` column, so off-topic results of a broad keyword are easy to filter in Excel.
- **Phones**: Persian/Arabic digits → Latin, `+98`/`0098`/`98` → `0`, `is_mobile = ^09\d{9}$`. Both the raw and normalized numbers are stored.
- **Pacing**: 4–10 s between scroll steps, with occasional 20–60 s pauses. There is a 2–6 min pause between tiles and a 20–40 min break every 8–12 tiles. Hourly caps apply to tiles and detail opens. The active window is 08:00–01:00 Tehran time. One persistent browser session is reused and restarted every 3 h.
- **Blocks**: a block is any of: a `/sorry/` URL, "unusual traffic" text, a reCAPTCHA iframe, HTTP 429, or 3 empty tiles in a row next to productive tiles. On a block, a screenshot and the HTML go to `debug/blocks/`, the browser closes, and the tile goes back to pending. Backoff is 3h → 6h → 12h → 24h, persisted in the DB, and resets after a successful tile. **CAPTCHAs are never solved or bypassed.**
- **Errors**: a tile is retried up to 3 times with growing delays, then marked `failed` (see `retry-failed`). Every error is caught at tile level.
- **Consent page**: EU server IPs get Google's cookie-consent interstitial. The collector clicks "رد کردن همه" (Reject all) once, and the profile keeps the cookie.
- **Export**: runs daily at 23:30 Tehran time inside the service, producing:
  - `exports/leads_YYYY-MM-DD.xlsx`: places first seen that day. The file is skipped, and a log line written, if there are none.
  - `exports/all_leads.xlsx`: cumulative, overwritten each time.

  Both files have segment and city columns, the sheets "موبایل" (mobile) and "همه" (all), RTL layout, a bold frozen header and auto widths.
  `examples/sample_leads.xlsx` shows the format: 10 rows with fake names, numbers and addresses. Real lead lists are never committed (`.gitignore`).
- **Logs**: `logs/run.log` (rotating, 5×5 MB), with one progress line per tile. `debug/` is pruned to `debug_max_mb`.

### Running as a service (systemd)

```bash
cp deploy/leadmap.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now leadmap
systemctl status leadmap
tail -f logs/run.log
systemctl stop leadmap        # graceful: current tile stays pending
```

### Status dashboard

This is a read-only HTTPS page with basic auth, served on its own port, so other web services on the server are untouched. It shows:
- progress per city, places and mobiles
- backoff state and recent tiles and events
- a live log tail
- Excel downloads and an "export now" button

It uses only the Python standard library.

```bash
cp deploy/leadmap-dashboard.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now leadmap-dashboard
# → https://panel.example.com:8880  (user/password: config.yaml → dashboard)
```

It uses the certificate set in `config.yaml → dashboard.cert/key` (e.g. from acme.sh) and reloads it automatically after renewal. After 10 failed logins, an IP is locked out for 10 minutes. `config.yaml` holds the password and is chmod 600.

### Alternative: tmux

```bash
tmux new -s leadmap
cd /root/map && .venv/bin/python main.py run
# detach: Ctrl-b d      reattach: tmux attach -t leadmap      stop: Ctrl-c (graceful)
```

### Headed mode / manual login

Only needed for debugging, or if you set `browser.use_profile_login: true`:

```bash
xvfb-run -a .venv/bin/python main.py test-tile --headed     # headed but invisible (debugging)
# To log in you need to *see* the window: use ssh -X, or VNC, then:
.venv/bin/python main.py login
```

Without login, a separate profile `data/profile_anon` is used, so a logged-in profile is never mixed in.

### Files

```
config.yaml           settings (not in git; start from config.example.yaml)
main.py               CLI
leadmap/grid.py       Nominatim + tiling      leadmap/phone.py    phone normalization
leadmap/parser.py     response/DOM parsing    leadmap/scrape.py   one tile / query / detail page
leadmap/pacing.py     delays, caps, window    leadmap/service.py  main loop, backoff, test-tile
leadmap/export.py     Excel                   leadmap/db.py       SQLite (WAL)
leadmap/hub.py        segments, job queue, merged places (hub.html = panel)
leadmap/hubclient.py  worker side: claim / heartbeat / upload / complete
data/leads.db  exports/  logs/  debug/  deploy/leadmap.service
```

### Operational notes

- On a small disk: `debug/` is capped, logs rotate, and each Excel file is small. Watch `df -h /`.
- If Google changes its response format, the parser raises a `ParseError`, and the raw body is saved to `debug/errors/`. Meanwhile the DOM fallback keeps collecting names, coordinates and list-visible phones. Re-inspect the dump, then update the index map at the top of `parser.py`.

---

## فارسی

ابزاری آرام، قابل‌ادامه و مؤدب برای جمع‌آوری لید کسب‌وکارها از گوگل‌مپ. شهر را به خانه‌های کوچک (تایل) تقسیم می‌کند و در هر خانه کلیدواژه‌های یک **صنف** را جستجو می‌کند؛ مثلاً «میوه فروشی» ← میوه فروشی، میوه، میوه فروش، تره بار، یا «املاک» ← املاک، مشاور املاک. هر صنفی را می‌شود تعریف کرد. نتایج در SQLite ذخیره می‌شوند و هر روز خروجی اکسل ساخته می‌شود. یک پنل مرکزی هم می‌تواند صفی از کارها (شهر × صنف) را بین چند سرور تقسیم کند.

### صنف‌ها

هر صنف یک نام است با کلیدواژه‌هایی که در هر خانه جستجو می‌شوند. هر ترکیب (شهر، صنف) شبکه و پیشرفت جداگانه دارد. مغازه‌ای که در دو صنف پیدا شود، در هر دو لیست لید می‌آید؛ و اگر شماره‌اش در یک صنف پیدا شده باشد، برای صنف دیگر دوباره صفحه‌ی جزئیاتش باز نمی‌شود.
- **سرور مستقل**: صنف‌ها در `config.yaml → search.segments` هستند و `search.segment` پیش‌فرض است. با `init`، `run`، `test-tile` و `status` از `--segment` استفاده کنید.
- **با پنل مرکزی**: صنف‌ها در خود پنل (بخش «صنف‌ها») مدیریت می‌شوند. اگر کلیدواژه‌ها را آنجا ویرایش کنید، سرورهای در حال کار در heartbeat بعدی تغییر را می‌گیرند.

```yaml
search:
  segments:
    املاک: ["املاک", "مشاور املاک"]
    میوه فروشی: ["میوه فروشی", "میوه", "میوه فروش", "تره بار"]
  segment: املاک
```

### نصب روی سرور جدید (با یک دستور)

روی سرور فعلی یک بسته‌ی قابل‌انتقال بسازید. دیتابیس، پروفایل مرورگر، `config.yaml` (که رمز در آن است)، لاگ‌ها و venv در بسته نمی‌آیند:
```bash
cd /root/map && ./make-package.sh          # → /root/leadmap-YYYYMMDD.tgz
scp /root/leadmap-*.tgz root@NEW_SERVER:/root/
```
یا همین مخزن را clone کنید. روی سرور جدید (اوبونتو/دبیان، حداقل ۲ گیگ رم، ۳ گیگ فضای خالی، IP غیرایرانی):
```bash
cd /root && tar xzf leadmap-*.tgz && cd leadmap
./install.sh                                   # شهر و ایمیل را می‌پرسد
# یا بدون سؤال:
./install.sh --yes --city "شیراز" --contact you@gmail.com
# صنف دیگر (به config.yaml اضافه و پیش‌فرض می‌شود):
./install.sh --yes --city "شیراز" --segment "میوه فروشی" --keywords "میوه فروشی,میوه,میوه فروش,تره بار" --contact you@gmail.com
# با گواهی دامنه‌ی واقعی برای داشبورد:
./install.sh --city "شیراز" --contact you@gmail.com --domain my.domain.com \
             --cert /path/fullchain.pem --key /path/privkey.pem
```
نصب‌کننده این کارها را انجام می‌دهد:
- فضای دیسک و رم را بررسی می‌کند
- بسته‌های سیستمی و پایتون ۳.۱۱ به بالا را نصب می‌کند (روی توزیع‌های قدیمی‌تر مثل اوبونتو ۲۲.۰۴ از طریق uv)
- `requirements.txt` و Chromium را نصب می‌کند و تست‌ها را اجرا می‌کند
- `config.yaml` را با یک رمز تصادفی برای داشبورد می‌سازد
- شبکه‌ی خانه‌های شهر را برای آن صنف می‌سازد
- هر دو سرویس systemd را نصب و اجرا می‌کند

در پایان آدرس داشبورد و نام کاربری و رمز را چاپ می‌کند. بدون `--cert` یک گواهی self-signed ساخته می‌شود و مرورگر یک بار هشدار می‌دهد. اجرای دوباره‌ی نصب‌کننده بی‌خطر است: تنظیمات، دیتابیس و پروفایل حفظ می‌شوند و `--reconfigure` تنظیمات را از نو می‌نویسد. همه‌ی گزینه‌ها: `./install.sh --help`.

**برای `--contact` ایمیل واقعی بدهید.** OpenStreetMap به ایمیل‌های ساختگی خطای 403 می‌دهد. **هر ترکیب شهر و صنف را فقط روی یک سرور اجرا کنید.** برای تقسیم خودکار کار بین سرورها از پنل مرکزی استفاده کنید (پایین‌تر).

همه‌ی تنظیمات در `config.yaml` است: مسیرها، شبکه، عبارت‌های جستجو، سرعت، بلاک و backoff، تلاش مجدد و ساعت خروجی.

### پنل مرکزی: چند سرور، یک صف کار

یکی از سرورها **هاب** را اجرا می‌کند؛ صفحه‌ای در `https://<آدرس داشبورد>:8880/hub` با همان نام کاربری و رمز داشبورد. روش کار:
- در پنل صنف‌ها و کلیدواژه‌هایشان را تعریف می‌کنید، بعد کار به صف اضافه می‌کنید: یک یا چند صنف را انتخاب می‌کنید و لیست شهرها را می‌نویسید؛ هر «شهر × صنف» یک کار می‌شود. هر سرور یک کار برمی‌دارد، شبکه‌ی آن شهر را برای آن صنف می‌سازد، با کلیدواژه‌های صنف جمع‌آوری می‌کند و تمام‌شدنش را گزارش می‌دهد. بعد کار بعدی صف را برمی‌دارد.
- سرورها هر ۶۰ ثانیه پیشرفت کار و مکان‌های جدید را می‌فرستند. هاب نسخه‌ی ادغام‌شده‌ی همه‌ی مکان‌ها را، بدون تکراری در هر صنف، در `data/hub.db` نگه می‌دارد. فایل‌های اکسلش در `exports/hub/` است: `all_leads.xlsx` با ستون‌های صنف و شهر، یک `<صنف>__<شهر>.xlsx` برای هر کار، و فایل روزانه‌ی `leads_YYYY-MM-DD.xlsx`. خروجی روزانه ساعت ۲۳:۳۰ به وقت تهران یا با زدن دکمه ساخته می‌شود.
- اگر سروری از قبل روی کاری از صف پیشرفت داشته باشد، اول همان را ادامه می‌دهد.
- اگر هاب در دسترس نباشد، سرورها کار فعلی را ادامه می‌دهند و داده‌ها را بعداً می‌فرستند. چیزی از دست نمی‌رود.
- کارهایی که از پنل می‌شود کرد:
  - افزودن، ویرایش یا حذف صنف (صنفی که کار در صف یا در حال انجام دارد حذف نمی‌شود)
  - تغییر ترتیب صف
  - **برگرداندن** کار فعال به صف: سرور آن را رها می‌کند و سرور بعدی از اول شروعش می‌کند
  - **صف دوباره** برای کار تمام‌شده، تا خانه‌های ناموفقش دوباره امتحان شوند
  - توقف یا ادامه‌ی کار یک سرور
  - باطل کردن توکن یک سرور: کارش به صف برمی‌گردد

هاب کردن یک سرور: `./install.sh --hub-server` (یا در `config.yaml` مقدار `hub.server: true` را بگذارید و `leadmap-dashboard` را ری‌استارت کنید).

اضافه کردن سرور جدید:
1. در پنل اسم سرور را بنویسید و «ساخت توکن» را بزنید. دستوری که نشان می‌دهد را کپی کنید؛ توکن فقط یک بار نمایش داده می‌شود.
2. روی سرور جدید، بعد از باز کردن بسته:
   ```bash
   ./install.sh --hub https://panel.example.com:8880 --hub-token <TOKEN> --contact you@gmail.com
   # فقط اگر گواهی هاب self-signed است --hub-insecure را اضافه کنید
   ```
   نصب‌کننده اتصال را با `python main.py hub ping` بررسی می‌کند و اگر به هاب وصل نشود با خطا متوقف می‌شود. `--city` لازم نیست. روی سروری که قبلاً مستقل نصب شده، همین گزینه‌ها فقط بخش `hub:` را به `config.yaml` اضافه می‌کنند.

وقتی `hub.url` و `hub.token` تنظیم باشند، دستور `run` کارها را از هاب می‌گیرد. `run --city X --segment Y` همچنان یک کار ثابت را جمع‌آوری می‌کند. دستورهای کمکی روی سرور هاب: `python main.py hub add-segment "میوه فروشی" "میوه فروشی" میوه "تره بار"`، `hub add-city --segment "میوه فروشی" شیراز مشهد`، `hub add-worker NAME`، `hub status`.

### دستورها

| دستور | کار |
|---|---|
| `python main.py init --city "کرج" [--segment S]` | مرز شهر را از OSM می‌گیرد (با کش) و شبکه‌ی مارپیچ را برای آن شهر و صنف می‌سازد؛ اجرای دوباره بی‌خطر است |
| `python main.py test-tile [--city X] [--segment S] [--tile ID] [--headed]` | فقط یک خانه را جمع‌آوری می‌کند (پیش‌فرض: مرکزی‌ترین)، نتایج و درصد موبایل را چاپ می‌کند و پاسخ‌های خام را در `debug/test_tile_*/` ذخیره می‌کند |
| `python main.py run [--city X] [--segment S] [--headed]` | اجرای طولانی‌مدت؛ از اولین خانه‌ی باقی‌مانده ادامه می‌دهد |
| `python main.py status [--city X] [--segment S]` | برای هر شهر و صنف: تعداد خانه‌ها، مکان‌ها و موبایل‌ها؛ وضعیت backoff و آخرین رویدادها |
| `python main.py export` | همین الان خروجی اکسل می‌سازد |
| `python main.py retry-failed [--city X] [--segment S]` | خانه‌های ناموفق را به صف برمی‌گرداند |
| `python main.py login` | مرورگر قابل‌مشاهده روی `data/profile` برای یک بار ورود به گوگل (فقط با `use_profile_login: true`) |
| `python main.py hub add-segment / add-city / add-worker / status / ping` | دستورهای پنل مرکزی (بالا را ببینید) |

### نحوه‌ی کار

- **شبکه**: مرز شهر از Nominatim گرفته می‌شود (با User-Agent اختصاصی، حداکثر یک درخواست در ثانیه، کش در `data/cache/`). اگر مرز چندضلعی نباشد، از مستطیل محدوده استفاده می‌شود و هشدار ثبت می‌شود. خانه‌ها مربع‌های ۸۰۰ متری در UTM هستند و فقط آن‌هایی که با شهر تلاقی دارند نگه داشته می‌شوند. ترتیبشان ردیف‌به‌ردیف و مارپیچ است. شناسه‌ی خانه `صنف/شهر:ردیف:ستون` است، پس اجرای دوباره‌ی `init` خانه‌ی تکراری نمی‌سازد.
- **استخراج**: برای هر کلیدواژه‌ی صنف، برنامه آدرس `https://www.google.com/maps/search/<q>/@lat,lng,16z?hl=fa&gl=ir` را باز می‌کند و پاسخ‌های `/search?tbm=map` را می‌گیرد. این پاسخ‌ها در `leadmap/parser.py` تجزیه می‌شوند. ساختار فیلدها در همان فایل مستند شده و روی پاسخ‌های واقعی ذخیره‌شده در `tests/fixtures/` تست شده است (ناشناس‌شده: نام، تلفن، آدرس و شناسه‌ها ساختگی‌اند). کارت‌های صفحه (DOM) پشتیبان هستند و بر اساس place id ادغام می‌شوند. صفحه آرام‌آرام اسکرول می‌شود تا به انتهای لیست برسد یا N اسکرول پشت‌سرهم چیز جدیدی نیاورد.
- **صفحه‌ی جزئیات**: اگر مکانی در لیست تلفن نداشته باشد، صفحه‌ی جزئیاتش یک بار در تب جدا باز می‌شود (`details_checked_at`). این کار سقف ساعتی دارد.
- **فیلتر**: مکانی نگه داشته می‌شود که داخل محدوده‌ی خانه (با کمی حاشیه) یا داخل مرز شهر باشد. مکان‌ها بر اساس place id و صنف ثبت یا به‌روز می‌شوند و `last_seen` تازه می‌شود. دسته‌بندی خود گوگل در ستون `category` می‌ماند، پس نتایج نامربوطِ یک کلیدواژه‌ی کلی را راحت می‌شود در اکسل فیلتر کرد.
- **تلفن**: ارقام فارسی و عربی به لاتین تبدیل می‌شوند، `+98`، `0098` و `98` به `0`، و `is_mobile = ^09\d{9}$`. هم شماره‌ی خام و هم شماره‌ی یکدست‌شده ذخیره می‌شود.
- **سرعت**: ۴ تا ۱۰ ثانیه بین هر اسکرول، گاهی مکث ۲۰ تا ۶۰ ثانیه‌ای. بین خانه‌ها ۲ تا ۶ دقیقه مکث و هر ۸ تا ۱۲ خانه ۲۰ تا ۴۰ دقیقه استراحت. برای خانه‌ها و صفحه‌های جزئیات سقف ساعتی هست. ساعت کاری ۸ صبح تا ۱ بامداد به وقت تهران است. یک نشست مرورگر ثابت استفاده و هر ۳ ساعت ری‌استارت می‌شود.
- **بلاک**: هر کدام از این‌ها بلاک حساب می‌شود: آدرس `/sorry/`، متن «unusual traffic»، کادر reCAPTCHA، خطای HTTP 429، یا ۳ خانه‌ی خالی پشت‌سرهم کنار خانه‌های پرنتیجه. در صورت بلاک، اسکرین‌شات و HTML در `debug/blocks/` ذخیره می‌شود، مرورگر بسته می‌شود و خانه به صف برمی‌گردد. زمان انتظار ۳ ← ۶ ← ۱۲ ← ۲۴ ساعت است، در دیتابیس ذخیره می‌شود و بعد از یک خانه‌ی موفق صفر می‌شود. **کپچا هرگز حل یا دور زده نمی‌شود.**
- **خطا**: هر خانه تا ۳ بار با فاصله‌ی بیشتر دوباره امتحان می‌شود و بعد `failed` می‌شود (`retry-failed` را ببینید). هر خطایی در سطح خانه گرفته می‌شود و سرویس را از کار نمی‌اندازد.
- **صفحه‌ی رضایت کوکی**: روی IPهای اروپایی، گوگل صفحه‌ی رضایت کوکی نشان می‌دهد. برنامه یک بار «رد کردن همه» را می‌زند و پروفایل کوکی را نگه می‌دارد.
- **خروجی**: هر روز ساعت ۲۳:۳۰ به وقت تهران داخل سرویس ساخته می‌شود:
  - `exports/leads_YYYY-MM-DD.xlsx`: مکان‌هایی که آن روز برای اولین بار دیده شده‌اند. اگر چیزی نباشد، فایل ساخته نمی‌شود و فقط در لاگ ثبت می‌شود.
  - `exports/all_leads.xlsx`: همه‌ی مکان‌ها، هر بار بازنویسی می‌شود.

  هر دو فایل ستون‌های صنف و شهر و برگه‌های «موبایل» و «همه» دارند، راست‌به‌چپ، با سرستون ثابت و پررنگ و عرض ستون خودکار.
  `examples/sample_leads.xlsx` قالب خروجی را نشان می‌دهد: ۱۰ ردیف با نام، شماره و آدرس ساختگی. لیست‌های لید واقعی هرگز در گیت قرار نمی‌گیرند (`.gitignore`).
- **لاگ**: `logs/run.log` (چرخشی، ۵ فایل ۵ مگابایتی) با یک خط پیشرفت برای هر خانه. پوشه‌ی `debug/` تا سقف `debug_max_mb` پاک‌سازی می‌شود.

### اجرا به‌صورت سرویس (systemd)

```bash
cp deploy/leadmap.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now leadmap
systemctl status leadmap
tail -f logs/run.log
systemctl stop leadmap        # توقف تمیز: خانه‌ی فعلی در صف می‌ماند
```

### داشبورد وضعیت

یک صفحه‌ی فقط‌خواندنی HTTPS با رمز، روی پورت جداگانه، تا سرویس‌های وب دیگر سرور دست نخورند. این‌ها را نشان می‌دهد:
- پیشرفت هر شهر، تعداد مکان‌ها و موبایل‌ها
- وضعیت backoff، آخرین خانه‌ها و رویدادها
- لاگ زنده
- دانلود فایل‌های اکسل و دکمه‌ی «ساخت خروجی همین الان»

فقط از کتابخانه‌ی استاندارد پایتون استفاده می‌کند.

```bash
cp deploy/leadmap-dashboard.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now leadmap-dashboard
# → https://panel.example.com:8880  (نام کاربری و رمز: config.yaml → dashboard)
```

از گواهی تنظیم‌شده در `config.yaml → dashboard.cert/key` استفاده می‌کند (مثلاً از acme.sh) و بعد از تمدید، خودکار دوباره بارگذاری‌اش می‌کند. بعد از ۱۰ ورود ناموفق، آن IP برای ۱۰ دقیقه مسدود می‌شود. رمز در `config.yaml` با دسترسی 600 نگه داشته می‌شود.

### روش جایگزین: tmux

```bash
tmux new -s leadmap
cd /root/map && .venv/bin/python main.py run
# جدا شدن: Ctrl-b d      برگشتن: tmux attach -t leadmap      توقف: Ctrl-c (تمیز)
```

### حالت مرورگر قابل‌مشاهده / ورود دستی

فقط برای عیب‌یابی یا وقتی `browser.use_profile_login: true` باشد لازم است:

```bash
xvfb-run -a .venv/bin/python main.py test-tile --headed     # مرورگر واقعی ولی نامرئی (عیب‌یابی)
# برای ورود باید پنجره را ببینید: از ssh -X یا VNC استفاده کنید، بعد:
.venv/bin/python main.py login
```

بدون ورود، پروفایل جداگانه‌ی `data/profile_anon` استفاده می‌شود تا پروفایلِ واردشده هیچ‌وقت قاطی نشود.

### فایل‌ها

```
config.yaml           تنظیمات (در گیت نیست؛ از config.example.yaml شروع کنید)
main.py               دستورها
leadmap/grid.py       Nominatim و تقسیم به خانه    leadmap/phone.py    یکدست‌سازی تلفن
leadmap/parser.py     تجزیه‌ی پاسخ و صفحه         leadmap/scrape.py   یک خانه / جستجو / صفحه‌ی جزئیات
leadmap/pacing.py     مکث‌ها، سقف‌ها، ساعت کاری    leadmap/service.py  حلقه‌ی اصلی، backoff، test-tile
leadmap/export.py     اکسل                         leadmap/db.py       SQLite (WAL)
leadmap/hub.py        صنف‌ها، صف کارها و ادغام مکان‌ها (hub.html = صفحه‌ی پنل)
leadmap/hubclient.py  سمت سرور کارگر: گرفتن شهر / گزارش / ارسال / پایان
data/leads.db  exports/  logs/  debug/  deploy/leadmap.service
```

### نکته‌های نگهداری

- روی دیسک کوچک: `debug/` سقف دارد، لاگ‌ها چرخشی‌اند و فایل‌های اکسل کوچک‌اند. فضای دیسک را با `df -h /` زیر نظر داشته باشید.
- اگر گوگل قالب پاسخ‌هایش را عوض کند، parser خطای `ParseError` می‌دهد و پاسخ خام در `debug/errors/` ذخیره می‌شود. در این مدت، پشتیبان DOM همچنان نام، مختصات و تلفن‌های قابل‌مشاهده در لیست را جمع می‌کند. فایل ذخیره‌شده را بررسی کنید و نقشه‌ی اندیس‌ها در بالای `parser.py` را به‌روز کنید.
