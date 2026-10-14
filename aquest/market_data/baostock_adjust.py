"""baostock 后复权因子查询（可选依赖，子进程隔离）。

为什么需要它：东方财富的分红送配接口对部分标的会**静默停更** —— 实测 002713
（2026-10-07 查）自 2019-06-21 之后就没有返回过任何记录，但它在 2025-12-30
确实除权过。这类漏记在行情里表现为除权日的巨大跳空，而 :mod:`gap_backfill`
为了不把"停牌 238 天后 -82%"误判成除权，对**跨停牌的跳空一律不推断**。两边
都不管，漏记就永久留在库里。

baostock 的 ``query_adjust_factor`` 提供一串独立的后复权因子，能回答"这一天
到底有没有除权"，并给出准确因子。实测（2026-10-07，10 个已在事件表里的真实
除权日）它与事件表因子的中位偏差 **0.02%**；而用开盘价反推在同一批跨停牌样本
上有约 **4.8% 的系统性偏差** —— 后者不是算错，是"复牌日开盘价 ≠ 理论除权价"
这一真实市场现象，换成任何推断口径都消不掉。所以要么不补，要补就补准的。

为什么必须关进子进程：理由同 :mod:`aquest.adapters.baostock_catalog` ——
baostock 的 socket 挂在全局单例上、没有超时、并发请求会在同一条连接上串数据。
这里同样一次查询一个短命子进程，且调用方串行。

**只查因子，不查行情。** baostock 的日线有已知的覆盖缺陷（ETF 只回溯到
2026-01-05），但复权因子序列是另一回事，实测与事件表吻合到 0.02%。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from collections.abc import Iterable, Mapping, Sequence
from datetime import date

__all__ = [
    "AdjustFactorMap",
    "BaostockAdjustUnavailable",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_TIMEOUT_SECONDS",
    "baostock_available",
    "fetch_adjust_factors",
]

#: 一批查询的子进程硬超时。baostock 单次往返是百毫秒级，40 只一批留足余量；
#: 到期由 ``subprocess`` 直接 kill，主进程不会残留卡死的单例 socket。
DEFAULT_TIMEOUT_SECONDS = 120.0
#: 每批多少只。分批是为了让单批超时有意义 —— 一次塞 5000 只进去，超时就只能
#: 整体重试，而分批后失败只会丢一批。
DEFAULT_BATCH_SIZE = 40

# baostock 的登录/查询/登出必须串行，见模块文档。这里的锁是防御用的：将来若
# 有人把它挪进并发路径，也不该悄悄破坏这个前提。
_SERIAL_LOCK = threading.Lock()

# 子进程里跑完「登录 → 逐只查询 → 登出」。请求从 stdin 读、结果写 stdout，避免
# 把上千条标的塞进 argv。字段用 ``|`` 分隔拼 key，JSON 里不会有歧义。
_CHILD_SCRIPT = """
import io
import json
import sys

# baostock 的 login()/logout() 会往 stdout 打印 "login success!" 之类，直接混进
# JSON 里。先把 stdout 换成缓冲区吞掉它们，写完结果再换回来 —— 比事后从混杂文本
# 里挑 JSON 可靠：那种做法一旦 baostock 改了打印内容就会解析错，而且错得很安静。
_sink = io.StringIO()
_real_stdout = sys.stdout
sys.stdout = _sink

import baostock as bs


def factor_on(symbol: str, day: str):
    \"\"\"当日后复权因子相对上一次记录的增量；当天没有除权就返回 None。

    只查一次序列（从很早的起点到目标日），取最后两条：若最后一条的日期正是
    目标日，说明当天因子变了，也就是当天除权；比值即这次事件的因子增量。
    这样一次往返就能同时回答「有没有」和「是多少」。

    查询本身失败（error_code 非 0）要**抛出去**，不能退化成 None：None 的语义是
    「当天没有除权」，把「没查到」说成「查过了」，漏记就永久留在库里，而报告上
    还写着已经过权威源校验 —— 比不校验更糟。
    \"\"\"
    result = bs.query_adjust_factor(symbol, "1990-01-01", day)
    if result.error_code != "0":
        raise RuntimeError(
            "query_adjust_factor error_code=%s %s" % (result.error_code, result.error_msg)
        )
    rows = []
    while result.next():
        item = result.get_row_data()
        rows.append((item[1], float(item[3])))
    if len(rows) < 2 or rows[-1][0] != day:
        return None
    if rows[-2][1] <= 0:
        return None
    return rows[-1][1] / rows[-2][1]


login = bs.login()
if login.error_code != "0":
    # baostock 登录失败不抛异常，只回一个 error_code。不查它，后面每一次查询都会
    # 安静地「查不到」，文档承诺的 503 就永远轮不到出场。
    sys.stdout = _real_stdout
    sys.stderr.write("baostock 登录失败 error_code=%s %s\\n" % (login.error_code, login.error_msg))
    sys.exit(3)

answers = {}
failures = 0
first_failure = ""
requests = []
try:
    requests = json.loads(sys.stdin.read())
    for market, code, day in requests:
        try:
            value = factor_on("%s.%s" % (market.lower(), code), day)
        except Exception as exc:
            # 单只失败不该让整批白跑：这只标成"查不到"，其余照常返回。
            failures += 1
            first_failure = first_failure or str(exc)
            value = None
        if value is not None:
            answers["%s|%s|%s" % (market, code, day)] = value
finally:
    bs.logout()
if requests and failures == len(requests):
    # 全军覆没不是"这一批都没除权"，而是这条源没通：交回空结果会被上层当成
    # 权威结论，所以这里非零退出，让父进程把它翻成 BaostockAdjustUnavailable。
    sys.stdout = _real_stdout
    sys.stderr.write(
        "baostock 整批 %d 个标的查询全部失败：%s\\n" % (failures, first_failure)
    )
    sys.exit(4)
# logout 的打印也要落进缓冲区，所以等它之后再换回真实 stdout 写结果。
sys.stdout = _real_stdout
sys.stdout.write(json.dumps(answers))
"""

#: ``(market, code, ex_date) -> 当日除权因子``。只含**确认当天除权**的条目，
#: 查不到的不在里面 —— 调用方据此区分"没除权"和"没查到"。
AdjustFactorMap = Mapping[tuple[str, str, date], float]


class BaostockAdjustUnavailable(RuntimeError):
    """因子查询不可用：没装可选依赖、子进程起不来、超时、登录失败，或整批查询全部失败。

    它的语义是**权威源没答上话**，不是「这些天都没有除权」。把前者当成后者，漏记
    就会永久留在库里而报告上还写着已校验。所以调用方要么整体重试，要么明确降级并
    把这个缺口说出去 —— 维护接口选的是前者：503 且不动任何数据。
    """


def baostock_available() -> bool:
    if importlib.util.find_spec("baostock") is None:
        return False
    return True


def _run_child(requests: Sequence[tuple[str, str, str]], timeout: float) -> str:
    if not baostock_available():
        raise BaostockAdjustUnavailable("未安装可选依赖 baostock，无法查询复权因子")
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _CHILD_SCRIPT],
            input=json.dumps(list(requests)),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # 子进程已被 kill，这里只负责把「卡死」翻译成可重试的错误。
        raise BaostockAdjustUnavailable(f"baostock 因子查询超过 {timeout:g} 秒无响应") from exc
    except OSError as exc:  # pragma: no cover - 解释器缺失属于环境故障
        raise BaostockAdjustUnavailable(f"baostock 因子查询子进程无法启动: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"退出码 {completed.returncode}"
        raise BaostockAdjustUnavailable(f"baostock 因子查询失败: {detail}")
    return completed.stdout


def fetch_adjust_factors(
    requests: Iterable[tuple[str, str, date | str]],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[tuple[str, str, date], float]:
    """批量判定"某日是否除权"，并给出该次事件的因子增量。

    ``requests`` 每项为 ``(market, code, ex_date)``。返回**只包含当天确实除权**
    的条目；没除权的、查不到的都不在结果里。

    分批串行执行：baostock 并发会在单例 socket 上串数据，这里宁可慢也不冒
    数据错位的风险。查询量是"被跳过跳空"的量级（数百），串行完全够用。

    单个标的查不到（退市、代码不认识、这一次接口报错）只会缺项；**登录失败或整批
    全部失败**会抛 :class:`BaostockAdjustUnavailable` —— 那种情况下"结果里没有"根本
    不构成证据，不能替权威源回答「没有除权」。
    """

    normalized: list[tuple[str, str, str]] = []
    for market, code, day in requests:
        resolved = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
        normalized.append((str(market), str(code), resolved.isoformat()))
    if not normalized:
        return {}

    answers: dict[tuple[str, str, date], float] = {}
    with _SERIAL_LOCK:
        for start in range(0, len(normalized), batch_size):
            batch = normalized[start : start + batch_size]
            raw = _run_child(batch, timeout)
            try:
                parsed = json.loads(raw)
            except ValueError as exc:
                raise BaostockAdjustUnavailable(
                    f"baostock 因子查询返回了无法解析的内容: {exc}"
                ) from exc
            for key, value in parsed.items():
                market, code, day = key.split("|", 2)
                answers[(market, code, date.fromisoformat(day))] = float(value)
    return answers
