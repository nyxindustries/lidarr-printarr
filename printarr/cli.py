"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from printarr import __version__
from printarr.config import (
    Config,
    ConfigError,
    load_config,
    validate_for_lidarr,
)
from printarr.fingerprint import AcoustidClient
from printarr.lidarr import LidarrClient, LidarrError
from printarr.log import get_logger, setup_logging
from printarr.musicbrainz import MusicBrainzClient
from printarr.processor import Processor
from printarr.queueworker import QueueWorker

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="printarr",
        description="Acoustic-fingerprint music identifier, tagger and renamer "
                    "for Lidarr — like namer, but for music.")
    parser.add_argument("-c", "--config", metavar="FILE",
                        help="path to printarr.toml (default: ./printarr.toml, "
                             "~/.config/printarr/printarr.toml, /config/printarr.toml)")
    parser.add_argument("--dry-run", action="store_true",
                        help="log what would happen without changing anything")
    parser.add_argument("--log-level", metavar="LEVEL",
                        help="DEBUG, INFO, WARNING or ERROR")
    parser.add_argument("-V", "--version", action="version",
                        version=f"printarr {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    identify = sub.add_parser(
        "identify", help="identify a folder and print the proposed match (no changes)")
    identify.add_argument("path", type=Path)
    identify.add_argument("--release-group", metavar="MBID",
                          help="MusicBrainz release-group hint")

    process = sub.add_parser(
        "process", help="identify a folder, fix its tags and rename its files")
    process.add_argument("path", type=Path)
    process.add_argument("--release-group", metavar="MBID",
                         help="MusicBrainz release-group hint")
    process.add_argument("--scan", action="store_true",
                         help="trigger a Lidarr DownloadedAlbumsScan afterwards")

    sub.add_parser("queue", help="fix all stuck items in the Lidarr queue once")
    sub.add_parser("watch", help="run continuously: poll the Lidarr queue and "
                                 "process watch folders (starts the web UI when "
                                 "web.enabled is set)")
    sub.add_parser("web", help="serve only the review web UI for manual "
                               "match assignment")
    return parser


def _make_processor(config: Config) -> Processor:
    mb = MusicBrainzClient(contact=config.musicbrainz.contact)
    acoustid = (AcoustidClient(config.acoustid.api_key)
                if config.acoustid.api_key else None)
    return Processor(config, mb, acoustid)


def _print_result(result) -> int:
    if not result.success:
        print(f"no confident match: {result.reason}", file=sys.stderr)
        if result.report and result.report.candidates:
            print("candidates considered:", file=sys.stderr)
            for cand in result.report.candidates[:5]:
                print(f"  {cand['score']:.2f}  {cand['artist']} — {cand['title']} "
                      f"({cand.get('date', '?')}, {cand['tracks']} tracks, "
                      f"{cand['fingerprint_hits']} fingerprint hits)  {cand['release_id']}",
                      file=sys.stderr)
        return 1

    release = result.release
    print(f"{release.artist} — {release.title} ({release.year or '?'})")
    print(f"  release:       https://musicbrainz.org/release/{release.id}")
    print(f"  release group: https://musicbrainz.org/release-group/{release.release_group_id}")
    print(f"  matched {len(result.match.pairs)}/{len(release.tracks)} tracks, "
          f"score {result.match.score:.2f}")
    for pair in result.match.pairs:
        marker = "♪" if pair.fingerprint_matched else " "
        print(f"  {marker} {pair.track.medium}-{pair.track.position:02d} "
              f"{pair.track.title}  <-  {pair.file.path.name}  ({pair.score:.2f})")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        config.general.dry_run = True
    if args.log_level:
        config.general.log_level = args.log_level
    setup_logging(config.general.log_level)

    try:
        if args.command == "identify":
            config.general.dry_run = True
            processor = _make_processor(config)
            result = processor.process_folder(args.path,
                                              release_group_hint=args.release_group)
            return _print_result(result)

        if args.command == "process":
            processor = _make_processor(config)
            result = processor.process_folder(args.path,
                                              release_group_hint=args.release_group)
            code = _print_result(result)
            if code == 0 and args.scan and not config.general.dry_run:
                validate_for_lidarr(config)
                lidarr = LidarrClient(config.lidarr.url, config.lidarr.api_key,
                                      verify_ssl=config.lidarr.verify_ssl)
                worker = QueueWorker(config, lidarr, processor)
                remote = worker.mapper.to_remote(str(args.path.resolve()))
                lidarr.trigger_downloaded_albums_scan(remote)
                print("Lidarr scan triggered")
            return code

        if args.command in ("queue", "watch", "web"):
            validate_for_lidarr(config)
            processor = _make_processor(config)
            lidarr = LidarrClient(config.lidarr.url, config.lidarr.api_key,
                                  verify_ssl=config.lidarr.verify_ssl)
            worker = QueueWorker(config, lidarr, processor)
            if args.command == "queue":
                try:
                    outcomes = worker.run_once()
                except LidarrError as exc:
                    print(f"could not fetch Lidarr queue: {exc}", file=sys.stderr)
                    return 1
                for outcome in outcomes:
                    status = "ok" if outcome.success else "FAILED"
                    print(f"[{status}] {outcome.title}: {outcome.reason}")
                return 0 if all(o.success for o in outcomes) else 1

            from printarr.webui import WebUI
            if args.command == "web":
                WebUI(config, worker).serve_forever()
                return 0
            if config.web.enabled:
                WebUI(config, worker).start_background()
            worker.watch()
            return 0
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    return 0
