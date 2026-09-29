"""Playwright browser session (persistent profile, one reused context)."""
import logging
import os
import shutil
import time

from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)


class Browser:
    def __init__(self, cfg, headed: bool = False):
        self.cfg = cfg
        self.headed = headed
        self._pw = None
        self.context = None
        self.page = None
        self.started_at = 0.0

    def start(self):
        b = self.cfg["browser"]
        profile = self.cfg.path("profile")
        if not b.get("use_profile_login"):
            # Without login we still keep a persistent profile (consent cookies etc.),
            # but a separate one so a logged-in profile is never touched.
            profile = profile.parent / "profile_anon"
        profile.mkdir(parents=True, exist_ok=True)
        _clear_stale_lock(profile)
        self._pw = sync_playwright().start()
        w, h = b["viewport"]
        self.context = self._pw.chromium.launch_persistent_context(
            str(profile),
            channel="chromium",          # full Chromium (new headless), not headless shell
            headless=not self.headed and b.get("headless", True),
            viewport={"width": w, "height": h},
            locale=b["locale"],
            timezone_id=b["timezone"],
            args=["--disable-blink-features=AutomationControlled", "--lang=fa-IR",
                  "--disk-cache-size=52428800"],  # 50 MB: disk is small
            extra_http_headers={"Accept-Language": "fa-IR,fa;q=0.9,en;q=0.6"},
        )
        self.context.set_default_navigation_timeout(b["nav_timeout_s"] * 1000)
        self.context.set_default_timeout(b["nav_timeout_s"] * 1000)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.started_at = time.monotonic()
        log.info("Browser started (headless=%s, profile=%s)", not self.headed, profile.name)

    def age_hours(self) -> float:
        return (time.monotonic() - self.started_at) / 3600 if self.context else 0.0

    def close(self):
        for fn in (lambda: self.context and self.context.close(), lambda: self._pw and self._pw.stop()):
            try:
                fn()
            except Exception as e:  # browser may already be dead
                log.debug("close: %s", e)
        self.context = self.page = self._pw = None
        log.info("Browser closed")

    @property
    def alive(self) -> bool:
        return self.context is not None


CONSENT_LABELS = ("رد کردن همه", "Reject all", "پذیرفتن همه", "Accept all")


def handle_consent(page) -> bool:
    """Dismiss Google's cookie-consent interstitial (shown for EU IPs). Not a CAPTCHA."""
    if "consent.google." not in page.url:
        return False
    for label in CONSENT_LABELS:
        btn = page.locator(f'button[aria-label="{label}"]').first
        if btn.count():
            time.sleep(1.5)
            btn.click()
            page.wait_for_url(lambda u: "consent.google." not in u, timeout=30000)
            log.info("Consent page dismissed (%s)", label)
            return True
    raise RuntimeError("consent page without known buttons")


def _clear_stale_lock(profile):
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = profile / name
        if p.is_symlink() or p.exists():
            try:
                p.unlink()
            except OSError:
                pass


def manual_login(cfg):
    """Open a headed browser on the login profile; the user logs in and closes it."""
    if not os.environ.get("DISPLAY"):
        print("No DISPLAY. Run via: xvfb-run -a ... is not useful for login; use a desktop or "
              "SSH X forwarding (ssh -X) / VNC, then: python main.py login")
    cfg["browser"]["use_profile_login"] = True
    b = Browser(cfg, headed=True)
    b.start()
    b.page.goto("https://accounts.google.com/")
    print("Log in in the opened window, then close the window (or press Ctrl+C here).")
    try:
        while b.context.pages:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    b.close()


def prune_dir(path, max_mb: float):
    """Delete oldest files until the directory fits in max_mb."""
    files = sorted((p for p in path.rglob("*") if p.is_file()), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    while files and total > max_mb * 1_000_000:
        p = files.pop(0)
        total -= p.stat().st_size
        p.unlink(missing_ok=True)
    for d in sorted((p for p in path.rglob("*") if p.is_dir()), reverse=True):
        if not any(d.iterdir()):
            shutil.rmtree(d, ignore_errors=True)
