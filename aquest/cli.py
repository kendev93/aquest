"""Command-line entry point for launching the standalone web app."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING

import uvicorn

if TYPE_CHECKING:
    from aquest.market_data.store import DuckDbMarketDataStore

_SERVE_COMMANDS = {"serve"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="selector-app",
        description="A股策略研究",
    )
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="启动 Web 服务（默认）")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8123)
    serve.add_argument("--reload", action="store_true")

    export = subparsers.add_parser("export", help="导出用户资产备份（zip）")
    export.add_argument(
        "-o",
        "--output",
        default=".",
        help="备份文件路径，或一个目录（默认当前目录，自动命名）",
    )

    restore = subparsers.add_parser("import", help="导入用户资产备份（合并，跳过已存在）")
    restore.add_argument("archive", help="备份 zip 路径")

    manifest = subparsers.add_parser("manifest", help="查看备份文件里有什么")
    manifest.add_argument("archive", help="备份 zip 路径")

    return parser


def _run_server(host: str, port: int, *, reload: bool) -> None:
    if reload:
        uvicorn.run("aquest.web.app:app", host=host, port=port, reload=True)
        return
    from aquest.web.app import app

    uvicorn.run(app, host=host, port=port)


def _open_store() -> DuckDbMarketDataStore:
    from aquest.market_data.store import DuckDbMarketDataStore

    return DuckDbMarketDataStore()


def _run_export(output: str) -> int:
    from aquest.market_data.transfer import export_user_assets

    summary = export_user_assets(_open_store(), output)
    print(f"已导出 {summary.path}（{summary.bytes_written} 字节）")
    for table, count in summary.counts.items():
        print(f"  {table}: {count}")
    return 0


def _run_import(archive: str) -> int:
    from aquest.market_data.transfer import import_user_assets

    summary = import_user_assets(_open_store(), archive)
    print(f"导入完成：新增 {summary.imported_total} 条，跳过 {summary.skipped_total} 条")
    for table, count in summary.imported.items():
        print(f"  {table}: +{count}（跳过 {summary.skipped[table]}）")
    return 0


def _run_manifest(archive: str) -> int:
    from aquest.market_data.transfer import read_manifest

    manifest = read_manifest(archive)
    print(f"格式: {manifest.get('format')} v{manifest.get('format_version')}")
    print(f"导出时间: {manifest.get('exported_at')}")
    print(f"schema: {manifest.get('schema_version')}")
    counts = manifest.get("counts")
    if isinstance(counts, dict):
        for table, count in counts.items():
            print(f"  {table}: {count}")
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    arguments = list(argv if argv is not None else sys.argv[1:])

    # 保持向后兼容：不带子命令时等同于 serve，老的 --host/--port 调用继续可用。
    if arguments and arguments[0] not in {"export", "import", "manifest", *_SERVE_COMMANDS}:
        arguments = ["serve", *arguments]
    elif not arguments:
        arguments = ["serve"]

    parser = _build_parser()
    args = parser.parse_args(arguments)

    if args.command == "export":
        raise SystemExit(_run_export(args.output))
    if args.command == "import":
        raise SystemExit(_run_import(args.archive))
    if args.command == "manifest":
        raise SystemExit(_run_manifest(args.archive))
    _run_server(args.host, args.port, reload=bool(getattr(args, "reload", False)))
