"""baostock 全市场证券目录（可选依赖，子进程隔离）。

为什么只用它做目录、而且必须关进子进程：

- 东方财富的 ``clist`` 目录接口已被针对性封禁（实测见
  ``docs/datasource-survey-2026-09-20.md`` 第二节），标的发现这一步断供；
  baostock 的 ``query_all_stock`` 恰好补上这个缺口，且 ``query_all_stock(day=)``
  返回的是**时点成分**，含已退市标的，没有幸存者偏差。
- baostock 的 ``SocketUtil`` 是**单例**，socket 挂在全局 ``context.default_socket``，
  ``send_msg()`` 无锁地 send 后循环 recv 直到读到结束符。多线程请求/响应会在
  同一条 TCP 连接上交错，轻则数据错位，重则永远等不到结束符。
- 它的 ``connect()`` 创建的 socket **没有设置任何超时**，服务端不响应就是无限期
  挂住，且实测 ``socket.setdefaulttimeout()`` 也救不回来。

所以这里把每次查询都放进一个**独立的短命子进程**：并发风险归零，硬超时由
``subprocess`` 的 timeout 保证（到期直接 kill 子进程），主进程不会残留卡死的
单例 socket。目录一天刷一次，进程启动开销完全无所谓。

日线**不**走 baostock：它的 ETF 历史只回溯到 2026-01-05（无论复权参数怎么设），
会静默产生「看着完整、实则只有几个月」的脏数据。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import date

import pandas as pd

__all__ = [
    "BaostockCatalogError",
    "BaostockUnavailable",
    "baostock_available",
    "fetch_all_securities",
]

# 实测：一次全市场目录查询 26~44 秒（2026-09-20，7401 行）。它比单根 K 线慢两个
# 数量级，超时必须按它自己的量级给，否则「偶发降级」其实就是「每次都超时」。
_DEFAULT_TIMEOUT_SECONDS = 150.0
# baostock 的登录/查询/登出必须串行，见模块文档。目录刷新本身在父进程里只跑一次，
# 这把锁是防御用的：任何将来把它挪进并发路径的改动都不该悄悄破坏这个前提。
_SERIAL_LOCK = threading.Lock()

# 子进程里单独跑完「登录 → 取最近交易日 → 查询 → 登出」。字段用制表符分隔，避免
# 证券简称里的半角逗号把行切坏。退出码区分失败阶段，方便主进程给出可读的报错。
_CHILD_SCRIPT = """
import datetime as dt
import sys

import baostock as bs


def recent_trade_days(limit=5):
    \"\"\"最近几个已发生的交易日，倒序。

    query_all_stock(day=None) 取的是「当天」，周末和节假日会返回 0 行——实测
    2026-09-20（周日）跑就是空。所以先用交易日历定位一个有数据的日子。
    \"\"\"
    today = dt.date.today()
    result = bs.query_trade_dates(
        start_date=(today - dt.timedelta(days=30)).isoformat(),
        end_date=today.isoformat(),
    )
    if result.error_code != "0":
        return []
    days = []
    while result.next():
        row = result.get_row_data()
        if len(row) >= 2 and row[1] == "1":
            days.append(row[0])
    today_text = today.isoformat()
    return [day for day in days if day <= today_text][-limit:][::-1]


def fetch(day):
    result = bs.query_all_stock(day=day)
    if result.error_code != "0":
        sys.stderr.write("query failed: %s\\n" % result.error_msg)
        sys.exit(3)
    rows = []
    while result.next():
        rows.append("\\t".join(result.get_row_data()))
    return rows


login = bs.login()
if login.error_code != "0":
    sys.stderr.write("login failed: %s\\n" % login.error_msg)
    sys.exit(2)
try:
    requested = sys.argv[1] if len(sys.argv) > 1 else ""
    for day in ([requested] if requested else recent_trade_days()):
        rows = fetch(day)
        if rows:
            sys.stdout.write("\\n".join(rows) + "\\n")
            sys.exit(0)
    sys.stderr.write("empty catalog\\n")
    sys.exit(4)
finally:
    bs.logout()
"""

# 子进程返回的列顺序由 baostock 决定：code / tradeStatus / code_name。
_CODE_COLUMN = 0
_STATUS_COLUMN = 1
_NAME_COLUMN = 2
_MARKET_PREFIXES = {"sh": "SH", "sz": "SZ"}


class BaostockCatalogError(RuntimeError):
    """Base class for baostock catalog failures.

    刻意不继承 ``OnlineDataError``：那个类住在 ``online_market_sync`` 里，而那个
    模块要反过来导入本模块，继承会把两个模块锁成循环导入。
    """


class BaostockUnavailable(BaostockCatalogError):
    """Raised when the optional baostock backend cannot serve a catalog."""


#: ``(trade_date, timeout) -> raw tab-separated stdout``。抽出来是为了让单测不需要
#: 真的安装 baostock 或真的联网。
CatalogRunner = Callable[[str, float], str]


def baostock_available() -> bool:
    """Whether the optional baostock dependency is installed in this environment."""

    return importlib.util.find_spec("baostock") is not None


def fetch_all_securities(
    *,
    trade_date: date | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    runner: CatalogRunner | None = None,
) -> pd.DataFrame:
    """Return the whole SH/SZ security catalog as ``代码``/``名称`` rows.

    返回的帧与东方财富目录同构，可以直接喂给 ``_selection_from_eastmoney_list``。
    北京证券交易所的标的会被丢掉——行情库与回测契约目前都是 SH/SZ 两市。
    """

    if timeout <= 0:
        raise ValueError("baostock 目录查询超时时间必须大于 0")
    call = runner or _run_child
    day = trade_date.isoformat() if trade_date is not None else ""
    with _SERIAL_LOCK:
        raw = call(day, timeout)
    return _parse_catalog(raw)


def _run_child(day: str, timeout: float) -> str:
    if not baostock_available():
        raise BaostockUnavailable("未安装可选依赖 baostock，无法使用该证券目录源")
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _CHILD_SCRIPT, day],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # 子进程已经被 kill，这里只负责把「卡死」翻译成可重试的错误。
        raise BaostockUnavailable(f"baostock 目录查询超过 {timeout:g} 秒无响应") from exc
    except OSError as exc:  # pragma: no cover - 解释器缺失属于环境故障
        raise BaostockUnavailable(f"baostock 目录查询子进程无法启动: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"退出码 {completed.returncode}"
        raise BaostockUnavailable(f"baostock 目录查询失败: {detail}")
    return completed.stdout


def _parse_catalog(raw: str) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split("\t")
        # 行尾的制表符会被 strip 掉，所以空简称的行只剩两列。这类标的本身是
        # 有效的，不能因为没名字就把整只股票丢掉。
        if len(fields) <= _STATUS_COLUMN:
            continue
        symbol = fields[_CODE_COLUMN].strip()
        market = _market_for_symbol(symbol)
        if market is None:
            # 北交所（bj.*）以及任何未来新增的市场前缀都在这里被挡掉。
            continue
        code = symbol.partition(".")[2].strip()
        if not code.isdigit() or len(code) != 6:
            continue
        raw_name = fields[_NAME_COLUMN].strip() if len(fields) > _NAME_COLUMN else ""
        name = raw_name or None
        rows.append({"代码": code, "名称": name})
    if not rows:
        raise BaostockUnavailable("baostock 证券目录为空")
    return pd.DataFrame(rows, columns=["代码", "名称"])


def _market_for_symbol(symbol: str) -> str | None:
    prefix = symbol.partition(".")[0].strip().lower()
    return _MARKET_PREFIXES.get(prefix)
