"""baostock 复权因子查询的行为约束。

重点是两个"安静失败"的坑：

1. **baostock 的 ``login()`` / ``logout()`` 会往 stdout 打印.** 子进程用它把结果
   传回来，这些打印会混进 JSON 里。早期版本因此整批解析失败，而报告上只显示
   "查不到"，看不出是解析错了。现在子进程先把 stdout 换成缓冲区吞掉它们，
   这里用一个假 baostock **真实执行子进程**来钉住这一点（不碰网络）。

2. **解析不出结果必须报错，不能返回空字典.** 空字典的语义是"当天没有除权"，
   把"没查到"当成"权威源确认没有"，漏记就永远补不回来，而且报告上还显示
   "已校验过" —— 比不校验更糟。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from selector_app.market_data import baostock_adjust as module
from selector_app.market_data.baostock_adjust import (
    BaostockAdjustUnavailable,
    fetch_adjust_factors,
)

KEY = ("SZ", "002713", date(2025, 12, 30))

# 假 baostock：照真实库的脾气来 —— login/logout 往 stdout 打印，
# query_adjust_factor 交回两行因子（末行日期命中目标日，比值即当次因子）。
_FAKE_BAOSTOCK = '''
def login():
    print("login success!")
    return type("R", (), {"error_code": "0"})()


def logout():
    print("logout success!")


def query_adjust_factor(symbol, start, end):
    class RS:
        error_code = "0"

        def __init__(self):
            self.rows = [["x", "2020-01-01", "1", "1.0"],
                         ["x", "2025-12-30", "1", "1.3514"]]
            self.cur = None

        def next(self):
            if self.rows:
                self.cur = self.rows.pop(0)
                return True
            return False

        def get_row_data(self):
            return self.cur

    return RS()
'''


def test_child_output_is_bare_json(tmp_path: Path) -> None:
    """子进程必须只吐 JSON —— 不能被 login/logout 的打印污染。

    真实执行子进程，让它 import 一个会打印的假 baostock：若吞打印这一步被改坏，
    这里 ``json.loads`` 就会失败，而不是"安静地查不到"。
    """

    (tmp_path / "baostock.py").write_text(_FAKE_BAOSTOCK, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    completed = subprocess.run(
        [sys.executable, "-c", module._CHILD_SCRIPT],
        input=json.dumps([["SZ", "002713", "2025-12-30"]]),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    parsed = json.loads(completed.stdout)
    assert parsed == {"SZ|002713|2025-12-30": pytest.approx(1.3514)}


def test_unparsable_output_raises_instead_of_reporting_no_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解析不出来就报错，绝不能当成"当天没除权"。

    返回空字典等于替权威源回答了"没有事件"，漏记会被永久固化，而且报告上还
    写着已校验 —— 这是这条通道最危险的失败方式。
    """

    monkeypatch.setattr(
        module,
        "_run_child",
        lambda requests, timeout: "login success!\n{}logout success!",
    )
    with pytest.raises(BaostockAdjustUnavailable):
        fetch_adjust_factors([KEY])


def test_answer_keys_round_trip_back_to_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回的键必须是 ``(market, code, date)``，不能退化成字符串。

    调用方用它做``是否除权``的查表；键的类型一旦和请求对不上，查表永远落空，
    表现为"全都确认没有除权" —— 同样是安静的错。
    """

    monkeypatch.setattr(
        module,
        "_run_child",
        lambda requests, timeout: json.dumps({"SZ|002713|2025-12-30": 1.3514}),
    )
    answers = fetch_adjust_factors([KEY])
    assert answers == {KEY: pytest.approx(1.3514)}


def test_empty_requests_do_not_spawn_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:  # pragma: no cover
        raise AssertionError("没有请求时不该启动子进程")

    monkeypatch.setattr(module, "_run_child", fail)
    assert fetch_adjust_factors([]) == {}
