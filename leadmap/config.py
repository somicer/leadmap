"""Config loading. Paths are resolved relative to the project root."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


class Config(dict):
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
    for key in ("cache", "debug", "exports", "logs", "profile"):
        cfg.path(key).mkdir(parents=True, exist_ok=True)
    cfg.path("db").parent.mkdir(parents=True, exist_ok=True)
    return cfg
