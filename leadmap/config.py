"""Config loading. Paths are resolved relative to the project root."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


class Config(dict):
    def queries(self, segment: str) -> list[str]:
        """Keywords of a segment from config.yaml → search.segments."""
        try:
            return list(self["search"]["segments"][segment])
        except KeyError:
            known = ", ".join(self["search"]["segments"])
            raise SystemExit(f"unknown segment {segment!r}; config.yaml → search.segments has: {known}") from None

    def path(self, key: str) -> Path:
        p = Path(self["paths"][key])
        p = p if p.is_absolute() else ROOT / p
        return p


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        cfg = Config(yaml.safe_load(f))
    cfg["paths"].setdefault("hub_db", "data/hub.db")
    cfg["hub"] = cfg.get("hub") or {}
    s = cfg.setdefault("search", {})
    if not s.get("segments"):  # older configs: one keyword list, which was real estate
        s["segments"] = {"املاک": s.get("queries") or ["املاک", "مشاور املاک"]}
    s.setdefault("segment", next(iter(s["segments"])))
    for key in ("cache", "debug", "exports", "logs", "profile"):
        cfg.path(key).mkdir(parents=True, exist_ok=True)
    cfg.path("db").parent.mkdir(parents=True, exist_ok=True)
    return cfg
