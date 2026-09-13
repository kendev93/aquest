"""Command-line entry point for launching the standalone web app."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="指标实验室")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)
    if args.reload:
        uvicorn.run("selector_app.web.app:app", host=args.host, port=args.port, reload=True)
        return
    from selector_app.web.app import app

    uvicorn.run(app, host=args.host, port=args.port)
