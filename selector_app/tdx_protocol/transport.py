from __future__ import annotations

import socket
import time
from types import TracebackType
from typing import TYPE_CHECKING, TypeVar

from .codec import HEADER_SIZE, decode_frame_header, decompress_body
from .errors import TdxConnectionError, TdxDecodeError

if TYPE_CHECKING:
    from .commands import Command

T = TypeVar("T")

_SETUP_COMMANDS = (
    bytes.fromhex("0c0218930001030003000d0001"),
    bytes.fromhex("0c0218940001030003000d0002"),
    bytes.fromhex(
        "0c031899000120002000db0fd5d0c9ccd6a4a8af0000008fc22540130000d500c9ccbdf0d7ea00000002"
    ),
)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        if not chunk:
            raise TdxConnectionError("连接被服务器关闭")
        result.extend(chunk)
    return bytes(result)


class TdxConnection:
    """Minimal synchronous TDX TCP connection."""

    def __init__(self, host: str, port: int = 7709, timeout: float = 15.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._socket: socket.socket | None = None

    def connect(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect((self.host, self.port))
            self._socket = sock
            for setup in _SETUP_COMMANDS:
                sock.sendall(setup)
                header = decode_frame_header(_recv_exact(sock, HEADER_SIZE))
                if header[3] > 0:
                    _recv_exact(sock, header[3])
        except (OSError, TdxConnectionError, TdxDecodeError) as exc:
            sock.close()
            self._socket = None
            raise TdxConnectionError(f"无法连接 {self.host}:{self.port}: {exc}") from exc

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    def set_timeout(self, timeout: float) -> None:
        """Apply a bounded timeout to the current socket and future reconnects."""

        if timeout <= 0:
            raise ValueError("TDX 连接超时时间必须大于 0")
        self.timeout = timeout
        if self._socket is not None:
            self._socket.settimeout(timeout)

    def execute(self, command: Command[T]) -> T:
        sock = self._socket
        if sock is None:
            raise TdxConnectionError("未连接，请先调用 connect()")
        try:
            sock.sendall(command.build_request())
            header = decode_frame_header(_recv_exact(sock, HEADER_SIZE))
            raw_body = _recv_exact(sock, header[3]) if header[3] else b""
            return command.parse_response(decompress_body(raw_body, header))
        except (OSError, TdxConnectionError, TdxDecodeError) as exc:
            raise TdxConnectionError(f"通信错误: {exc}") from exc

    def __enter__(self) -> TdxConnection:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()


def ping_host(host: str, port: int = 7709, timeout: float = 5.0) -> float | None:
    started = time.monotonic()
    connection = TdxConnection(host, port, timeout)
    try:
        connection.connect()
        return time.monotonic() - started
    except TdxConnectionError:
        return None
    finally:
        connection.close()
