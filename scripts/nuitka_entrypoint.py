"""Release entry point shared by the protected server and strategy worker bundle."""

from __future__ import annotations

import sys
from collections.abc import Sequence


def split_runtime_args(argv: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    """Resolve the release subcommand while keeping the source CLI compatible."""

    arguments = tuple(str(argument) for argument in argv)
    if arguments and arguments[0] == "worker":
        return "worker", arguments[1:]
    if arguments and arguments[0] == "server":
        return "server", arguments[1:]
    return "server", arguments


def main(argv: Sequence[str] | None = None) -> None:
    mode, arguments = split_runtime_args(sys.argv[1:] if argv is None else argv)
    if mode == "worker":
        if arguments:
            raise SystemExit("strategy worker 不接受命令行参数")
        from selector_app.strategy_discovery.worker import main as worker_main

        worker_main()
        return

    from selector_app.cli import main as server_main

    server_main(arguments)


if __name__ == "__main__":
    main()
