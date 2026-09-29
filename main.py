#!/usr/bin/env python3
"""Google Maps lead collector for any business segment (صنف) — CLI entry point."""
import argparse
import logging
import sys

from leadmap.config import load_config
from leadmap.db import DB
from leadmap.logs import setup_logging

log = logging.getLogger("leadmap")


def local_job(cfg, db, args) -> dict:
    """(city, segment, keywords) for CLI commands: flags, else the hub's current job, else config.yaml."""
    cached = db.get_state("hub_job") or {}
    city = args.city or cached.get("city") or cfg["city"]
    segment = args.segment or (cached.get("segment") if city == cached.get("city") else None) \
        or cfg["search"]["segment"]
    if (city, segment) == (cached.get("city"), cached.get("segment")):
        return {"city": city, "segment": segment, "queries": cached["queries"]}
    return {"city": city, "segment": segment, "queries": cfg.queries(segment)}


def cmd_init(cfg, db, args):
    from leadmap.grid import init_city
    job = local_job(cfg, db, args)
    r = init_city(cfg, db, job["city"], job["segment"])
    print(f"City: {r['result'].get('display_name')}  |  segment: {job['segment']} {job['queries']}")
    print(f"Boundary: {'BOUNDING BOX (no polygon!)' if r['fallback'] else r['result']['geojson']['type']}")
    print(f"Tiles: {r['tiles']} ({r['added']} newly inserted, {r['tiles'] - r['added']} already present)")


def cmd_status(cfg, db, args):
    from leadmap.service import backoff_status
    current = db.get_state("hub_job") or {}
    for city, segment in db.jobs():
        if (args.city and city != args.city) or (args.segment and segment != args.segment):
            continue
        tc = db.tile_counts(city, segment)
        pc = db.place_counts(city, segment)
        mark = "  ← current (hub)" if (city, segment) == (current.get("city"), current.get("segment")) else ""
        print(f"{city} / {segment}{mark}")
        print(f"  Tiles: {sum(tc.values())} total | done {tc.get('done', 0)} | empty {tc.get('empty', 0)} | "
              f"pending {tc.get('pending', 0)} | failed {tc.get('failed', 0)}")
        ratio = f"{pc['mobiles'] / pc['total']:.0%}" if pc["total"] else "-"
        print(f"  Places: {pc['total']} | with phone {pc['phones']} | mobiles {pc['mobiles']} ({ratio})")
    print(f"Backoff: {backoff_status(db)}")
    for r in db.conn.execute("SELECT timestamp, type, detail FROM events ORDER BY id DESC LIMIT 5"):
        print(f"  {r['timestamp']} {r['type']}: {(r['detail'] or '')[:120]}")


def cmd_test_tile(cfg, db, args):
    from leadmap.service import test_tile
    test_tile(cfg, db, local_job(cfg, db, args), args.tile, headed=args.headed)


def cmd_run(cfg, db, args):
    from leadmap.hubclient import HubClient
    from leadmap.service import Service
    if HubClient.configured(cfg) and not (args.city or args.segment):  # jobs come from the central queue
        Service(cfg, db, None, headed=args.headed, hub=HubClient(cfg, db)).run()
    else:
        Service(cfg, db, local_job(cfg, db, args), headed=args.headed).run()


def cmd_hub(cfg, db, args):
    """Central hub admin (on the hub server) and `ping` (on a worker)."""
    if args.hub_cmd == "ping":
        return hub_ping(cfg, db)
    from leadmap.hub import HubDB, HubError
    hub = HubDB(cfg.path("hub_db"))
    try:
        if args.hub_cmd == "add-worker":
            token = hub.add_worker(args.name)
            print(f"Worker {args.name!r} created. Token (shown only once):\n  {token}")
        elif args.hub_cmd == "add-segment":
            hub.set_segment(args.name, args.keywords)
            print(f"segment {args.name!r}: {args.keywords}")
        elif args.hub_cmd == "add-city":
            added = hub.add_jobs(args.names, args.segment)
            print(f"queued for {args.segment}: {', '.join(added) or 'nothing new'}")
        elif args.hub_cmd == "status":
            o = hub.overview()
            for s in o["segments"]:
                print(f"segment {s['name']}: {'، '.join(s['queries'])}")
            for c in o["jobs"]:
                t = c["tiles"]
                done = t.get("done", 0) + t.get("empty", 0)
                print(f"{c['status']:7} {c['city']:<14} {c['segment']:<14} {c['worker'] or '-':<12} "
                      f"tiles {done}/{sum(t.values())}  places {c['places']}  mobiles {c['mobiles']}")
            for w in o["workers"]:
                print(f"worker {w['name']:<14} {'online' if w['online'] else 'OFFLINE'}"
                      f"{' (paused)' if w['paused'] else ''}  "
                      f"job={w['job']['city'] + ' / ' + w['job']['segment'] if w['job'] else '-'}  "
                      f"last_seen={w['last_seen']}")
            print(f"total places {o['totals']['places']}, mobiles {o['totals']['mobiles']}")
    except HubError as e:
        print(f"error: {e}")
        return 1
    finally:
        hub.close()


def hub_ping(cfg, db):
    from leadmap.hubclient import HubClient, HubUnavailable
    if not HubClient.configured(cfg):
        print("hub.url / hub.token are not set in config.yaml")
        return 1
    try:
        r = HubClient(cfg, db).heartbeat({"phase": "installed"}, force=True)
    except HubUnavailable as e:
        print(f"hub NOT reachable: {e}")
        return 1
    print(f"hub OK: {cfg['hub']['url']} (current assignment: {r.get('assignment') or 'none'})")


def cmd_export(cfg, db, args):
    from leadmap.export import export_all
    export_all(cfg, db)


def cmd_retry_failed(cfg, db, args):
    n = db.reset_failed(args.city, args.segment)
    db.event("retry_failed", f"{n} tiles reset")
    print(f"{n} failed tiles reset to pending")


def cmd_dashboard(cfg, db, args):
    from leadmap.dashboard import serve
    db.close()
    serve(cfg)


def cmd_login(cfg, db, args):
    from leadmap.browser import manual_login
    manual_login(cfg)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", help="path to config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    seg_help = "segment (صنف) from config.yaml → search.segments"
    p = sub.add_parser("init", help="build the tile grid for a city and segment")
    p.add_argument("--city")
    p.add_argument("--segment", help=seg_help)
    p = sub.add_parser("run", help="long-running scraper (hub mode unless --city/--segment is given)")
    p.add_argument("--city")
    p.add_argument("--segment", help=seg_help)
    p.add_argument("--headed", action="store_true")
    p = sub.add_parser("test-tile", help="scrape one tile and dump raw responses")
    p.add_argument("--city")
    p.add_argument("--segment", help=seg_help)
    p.add_argument("--tile", help="tile id (default: the tile closest to the city centre)")
    p.add_argument("--headed", action="store_true")
    p = sub.add_parser("export", help="run the Excel export now")
    p.add_argument("--city")
    p = sub.add_parser("status")
    p.add_argument("--city")
    p.add_argument("--segment")
    p = sub.add_parser("retry-failed", help="reset failed tiles to pending")
    p.add_argument("--city")
    p.add_argument("--segment")
    sub.add_parser("dashboard", help="HTTPS status page (see config: dashboard)")
    sub.add_parser("login", help="open a headed browser on the persistent profile to log in")
    p = sub.add_parser("hub", help="central hub admin (queue of cities for many servers)")
    hs = p.add_subparsers(dest="hub_cmd", required=True)
    hs.add_parser("add-worker", help="create a worker and print its token").add_argument("name")
    p = hs.add_parser("add-segment", help="create or update a segment and its keywords")
    p.add_argument("name")
    p.add_argument("keywords", nargs="+")
    p = hs.add_parser("add-city", help="queue cities for a segment")
    p.add_argument("--segment", required=True)
    p.add_argument("names", nargs="+")
    hs.add_parser("status", help="queue and workers")
    hs.add_parser("ping", help="worker: check the connection to the central hub")
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg, args.cmd if args.cmd in ("run", "dashboard") else "cli")
    db = DB(cfg.path("db"))
    try:
        return {"init": cmd_init, "run": cmd_run, "test-tile": cmd_test_tile, "export": cmd_export,
         "status": cmd_status, "retry-failed": cmd_retry_failed, "login": cmd_login,
         "dashboard": cmd_dashboard, "hub": cmd_hub}[args.cmd](cfg, db, args)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
