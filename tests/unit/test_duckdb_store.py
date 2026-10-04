from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from threading import Event, Thread
from time import monotonic

import duckdb
import pandas as pd
import pytest

from selector_app.market_data.models import InstrumentRef
from selector_app.market_data.store import DuckDbMarketDataStore, default_data_dir
from selector_app.market_data.store_base import _WRITE_LOCK, BACKTEST_HISTORY_LIMIT


def _bars(*rows: tuple[str, str, str, float, str]) -> pd.DataFrame:
    values = rows or (
        ("SH", "600000", "2024-01-02", 10.0, "local"),
        ("SH", "600000", "2024-01-03", 11.0, "local"),
    )
    return pd.DataFrame(
        [
            {
                "market": market,
                "code": code,
                "instrument_type": "stock",
                "date": pd.Timestamp(day),
                "open": close - 0.5,
                "high": close + 0.5,
                "low": close - 1.0,
                "close": close,
                "volume": 1000.0,
                "amount": close * 1000,
                "source": source,
                "price_basis": "hfq",
                "bar_status": "completed",
            }
            for market, code, day, close, source in values
        ]
    )


def test_default_data_dir_uses_indicator_lab_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SELECTOR_DATA_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert default_data_dir() == tmp_path / ".indicator-lab"


def test_store_initializes_schema_and_reads_one_symbol(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(), price_basis="hfq")

    frame = store.read_bars("SH", "600000", start_date=date(2024, 1, 1))

    assert list(frame["date"].dt.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-03"]
    assert list(frame["close"]) == [10.0, 11.0]


def test_store_excludes_provisional_bars_by_default(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    frame = _bars()
    frame.loc[1, "bar_status"] = "provisional"
    store.replace_local_bars("SH", "600000", "stock", frame, price_basis="hfq")

    completed = store.read_bars("SH", "600000")
    all_bars = store.read_bars("SH", "600000", include_provisional=True)

    assert len(completed) == 1
    assert len(all_bars) == 2


def test_online_rows_fill_missing_dates_but_never_replace_local(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(), price_basis="hfq")
    online = _bars(
        ("SH", "600000", "2024-01-03", 99.0, "online"),
        ("SH", "600000", "2024-01-04", 12.0, "online"),
    )
    store.upsert_online_bars(online)

    frame = store.read_bars("SH", "600000")

    assert list(frame["close"]) == [10.0, 11.0, 12.0]
    assert list(frame["source"]) == ["local", "local", "online"]


def test_stale_raw_rows_are_invisible_to_reads_and_coverage(tmp_path: Path) -> None:
    """旧 raw 数据不能伪装成有效行情。

    读取默认只认 hfq：本地 vipdoc 导入落的不复权行（显式 price_basis="raw"，
    与 local_import 同参）既不会混进研究，也不会让覆盖区间误报"数据还在"——
    否则全量补齐会跳过这些标的，用户重下之后它们依然是空的。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(), price_basis="raw")

    assert store.read_bars("SH", "600000").empty
    ref = next(
        ref for ref in store.list_instruments(include_missing=True) if ref.code == "600000"
    )
    assert (ref.first_date, ref.last_date, ref.bar_count) == (None, None, 0)

    # 在线补上 hfq 后，读取与覆盖区间都只反映 hfq 行；raw 行留在库里，
    # 仅供维护视角显式读取。
    store.upsert_online_bars(
        _bars(
            ("SH", "600000", "2024-01-02", 10.0, "online"),
            ("SH", "600000", "2024-01-03", 11.0, "online"),
        )
    )
    frame = store.read_bars("SH", "600000")
    assert frame["source"].tolist() == ["online", "online"]
    ref = next(
        ref for ref in store.list_instruments(include_missing=True) if ref.code == "600000"
    )
    assert (ref.first_date, ref.last_date, ref.bar_count) == (
        date(2024, 1, 2),
        date(2024, 1, 3),
        2,
    )
    assert len(store.read_bars("SH", "600000", price_basis="raw")) == 2
    assert len(store.read_bars("SH", "600000", price_basis=None)) == 4


def test_batch_query_returns_sorted_instrument_groups(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars(
        "SH", "600001", "stock", _bars(("SH", "600001", "2024-01-02", 8.0, "local")),
        price_basis="hfq",
    )
    store.replace_local_bars("SH", "600000", "stock", _bars(), price_basis="hfq")

    frame = store.read_many_bars(
        [InstrumentRef("SH", "600000", "stock"), InstrumentRef("SH", "600001", "stock")]
    )

    assert list(zip(frame["market"], frame["code"], strict=True)) == [
        ("SH", "600000"),
        ("SH", "600000"),
        ("SH", "600001"),
    ]


def test_batch_query_with_empty_selection_returns_empty_frame(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(), price_basis="hfq")

    assert store.read_many_bars([]).empty


def test_batch_query_can_include_bounded_warmup_bars_before_window(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    rows = _bars(
        ("SH", "600000", "2024-01-01", 10.0, "local"),
        ("SH", "600000", "2024-01-02", 11.0, "local"),
        ("SH", "600000", "2024-01-03", 12.0, "local"),
        ("SH", "600000", "2024-01-04", 13.0, "local"),
        ("SH", "600000", "2024-01-05", 14.0, "local"),
    )
    store.replace_local_bars("SH", "600000", "stock", rows, price_basis="hfq")

    frame = store.read_many_bars(
        [InstrumentRef("SH", "600000", "stock")],
        start_date=date(2024, 1, 4),
        end_date=date(2024, 1, 5),
        warmup_bars=2,
    )

    assert list(frame["date"].dt.strftime("%Y-%m-%d")) == [
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
        "2024-01-05",
    ]


def test_store_status_reports_counts_and_range(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(), price_basis="hfq")

    status = store.status()

    assert status.database_path == str(tmp_path / "market.duckdb")
    assert status.instrument_count == 1
    assert status.bar_count == 2
    assert status.data_start == "2024-01-02"
    assert status.data_end == "2024-01-03"


def test_store_status_waits_for_an_in_process_writer(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(), price_basis="hfq")
    writer_started = Event()
    release_writer = Event()
    reader_finished = Event()
    observed = []

    def hold_write_lock() -> None:
        with _WRITE_LOCK:
            writer_started.set()
            assert release_writer.wait(timeout=2)

    def read_status() -> None:
        observed.append(store.status())
        reader_finished.set()

    writer = Thread(target=hold_write_lock)
    writer.start()
    assert writer_started.wait(timeout=2)

    reader = Thread(target=read_status)
    reader.start()
    started_waiting = monotonic()
    assert not reader_finished.wait(timeout=0.1)
    release_writer.set()
    reader.join(timeout=2)
    writer.join(timeout=2)

    assert reader_finished.is_set()
    assert monotonic() >= started_waiting
    assert observed[0].bar_count == 2


def test_store_persists_instrument_name_metadata(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(), price_basis="hfq")

    assert store.update_instrument_names({("SH", "600000"): "浦发银行"}) == 1

    ref = store.list_instruments()[0]
    assert ref.name == "浦发银行"


def test_read_only_store_can_query_but_cannot_write(tmp_path: Path) -> None:
    database = tmp_path / "market.duckdb"
    store = DuckDbMarketDataStore(database)
    store.replace_local_bars("SH", "600000", "stock", _bars(), price_basis="hfq")
    read_only = DuckDbMarketDataStore(database, read_only=True)

    assert len(read_only.read_bars("SH", "600000")) == 2
    with pytest.raises(RuntimeError, match="只读"):
        read_only.set_meta("test", "value")


def test_store_retries_transient_duckdb_lock(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "market.duckdb"
    store = DuckDbMarketDataStore(database)
    store.replace_local_bars("SH", "600000", "stock", _bars(), price_basis="hfq")
    original_connect = duckdb.connect
    attempts = 0

    def flaky_connect(path, *, read_only=False, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise duckdb.IOException("Could not set lock on file: transient lock")
        return original_connect(path, read_only=read_only, **kwargs)

    monkeypatch.setattr(duckdb, "connect", flaky_connect)

    assert len(store.read_bars("SH", "600000")) == 2
    assert attempts == 3


def test_backup_database_creates_recoverable_copy(tmp_path: Path) -> None:
    database = tmp_path / "market.duckdb"
    store = DuckDbMarketDataStore(database)
    store.replace_local_bars("SH", "600000", "stock", _bars(), price_basis="hfq")

    backup = store.backup_database()

    assert backup.is_file()
    assert backup.read_bytes() == database.read_bytes()


def test_backup_database_retains_only_the_latest_copy(tmp_path: Path) -> None:
    database = tmp_path / "market.duckdb"
    store = DuckDbMarketDataStore(database)
    store.replace_local_bars("SH", "600000", "stock", _bars(), price_basis="hfq")

    first = store.backup_database()
    second = store.backup_database()

    backups = list(tmp_path.glob("market.bak-*.duckdb"))
    assert first != second
    assert not first.exists()
    assert backups == [second]
    assert second.read_bytes() == database.read_bytes()


def test_schema_upgrade_backups_existing_database_before_initialization(tmp_path: Path) -> None:
    database = tmp_path / "market.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "CREATE TABLE data_meta (key VARCHAR PRIMARY KEY, value VARCHAR, updated_at TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO data_meta VALUES ('schema_version', '0', CURRENT_TIMESTAMP)"
        )

    DuckDbMarketDataStore(database).status()

    backups = list(tmp_path.glob("market.bak-*.duckdb"))
    assert len(backups) == 1


def test_schema_upgrade_backfills_provider_for_legacy_online_rows(tmp_path: Path) -> None:
    database = tmp_path / "market.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            """
            CREATE TABLE data_meta (
                key VARCHAR PRIMARY KEY,
                value VARCHAR,
                updated_at TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT INTO data_meta VALUES ('schema_version', '9', CURRENT_TIMESTAMP)"
        )
        connection.execute(
            """
            CREATE TABLE instruments (
                market VARCHAR NOT NULL,
                code VARCHAR NOT NULL,
                name VARCHAR,
                instrument_type VARCHAR NOT NULL,
                board VARCHAR NOT NULL,
                source_path VARCHAR,
                first_date DATE,
                last_date DATE,
                bar_count BIGINT NOT NULL,
                source_state VARCHAR NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                PRIMARY KEY (market, code)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE daily_bars (
                market VARCHAR NOT NULL,
                code VARCHAR NOT NULL,
                instrument_type VARCHAR NOT NULL,
                board VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                open DOUBLE NOT NULL,
                high DOUBLE NOT NULL,
                low DOUBLE NOT NULL,
                close DOUBLE NOT NULL,
                volume DOUBLE NOT NULL,
                amount DOUBLE NOT NULL,
                source VARCHAR NOT NULL,
                price_basis VARCHAR NOT NULL,
                bar_status VARCHAR NOT NULL,
                imported_at TIMESTAMP NOT NULL,
                UNIQUE (market, code, trade_date, price_basis)
            )
            """
        )
        connection.execute(
            "INSERT INTO instruments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "SH",
                "600000",
                "浦发银行",
                "stock",
                "main",
                None,
                "2024-01-02",
                "2024-01-02",
                1,
                "active",
                datetime.now(),
            ],
        )
        connection.execute(
            "INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "SH",
                "600000",
                "stock",
                "main",
                "2024-01-02",
                10.0,
                11.0,
                9.0,
                10.5,
                1000.0,
                10000.0,
                "online",
                "hfq",
                "completed",
                datetime.now(),
            ],
        )

    store = DuckDbMarketDataStore(database)

    ref = store.list_instruments()[0]
    bar = store.read_bars("SH", "600000").iloc[0]

    # 历史在线行由已移除的 TDX 通道写入，迁移仍按 tdx 标记保留原貌。
    assert ref.provider == "tdx"
    assert bar["provider"] == "tdx"


def test_screen_history_keeps_only_the_latest_ten_results(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    result = {
        "market": "SH",
        "code": "600000",
        "signal_date": 20240103,
        "last_close": 11.0,
        "matched_signals": ["indicator_three.prepare_rally"],
        "match_count": 1,
        "indicator_values": {"indicator_three.var1": 9.0},
    }
    meta = {
        "total_candidates": 1,
        "total_scanned": 1,
        "total_signals": 1,
        "errors": 0,
        "skipped": 0,
        "failure_reasons": {},
        "skip_reasons": {},
    }

    for index in range(12):
        store.save_screen_history(
            f"job-{index}",
            results=[result],
            meta=meta,
            created_at=datetime(2024, 1, 1 + index, tzinfo=timezone.utc),
        )

    history = store.list_screen_history()
    loaded = store.read_screen_history("job-11")

    assert len(history) == 10
    assert [item["history_id"] for item in history] == [
        f"job-{index}" for index in range(11, 1, -1)
    ]
    assert loaded is not None
    assert loaded["results"] == [result]
    assert loaded["meta"] == meta
    assert store.read_screen_history("job-0") is None


def test_pooled_read_session_reuses_one_connection_per_thread(tmp_path: Path) -> None:
    """回归：开启复用时同一线程必须复用同一条只读连接（每次建连约 9.5 ms）。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(), price_basis="hfq")
    pooled = DuckDbMarketDataStore(
        tmp_path / "market.duckdb", read_only=True, pool_read_connections=True
    )

    with pooled.read_session() as first:
        with pooled.read_session() as second:
            assert first is second

    assert list(pooled.read_bars("SH", "600000")["close"]) == [10.0, 11.0]


def test_unpooled_read_session_opens_a_fresh_connection(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(), price_basis="hfq")
    plain = DuckDbMarketDataStore(tmp_path / "market.duckdb", read_only=True)

    with plain.read_session() as first:
        with plain.read_session() as second:
            assert first is not second

    assert list(plain.read_bars("SH", "600000")["close"]) == [10.0, 11.0]


def test_pooled_read_session_is_per_thread(tmp_path: Path) -> None:
    """不同线程各自持有连接，避免跨线程共用 DuckDB 连接。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(), price_basis="hfq")
    pooled = DuckDbMarketDataStore(
        tmp_path / "market.duckdb", read_only=True, pool_read_connections=True
    )
    # 持有对象引用，否则连接被回收后 id() 会被复用，断言会失真。
    seen: list[object] = []

    def collect() -> None:
        with pooled.read_session() as connection:
            seen.append(connection)

    with pooled.read_session() as main_connection:
        threads = [Thread(target=collect) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert all(connection is not main_connection for connection in seen)
    assert seen[0] is not seen[1]


def test_delete_bars_drops_only_the_requested_source(tmp_path: Path) -> None:
    """撤销一次本地导入：只删 local 行，在线同步写下的行必须留下。

    本地写入是"同 key 删 + 插"，被覆盖掉的在线行不会自己回来，所以回退之后
    这一段要重新在线同步补齐 —— 这是回退入口的语义，不是"恢复覆盖前的数据"。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.upsert_online_bars(
        _bars(
            ("SH", "600000", "2024-01-02", 10.0, "online"),
            ("SZ", "000001", "2024-01-02", 5.0, "online"),
        )
    )
    store.upsert_local_bars(_bars(("SH", "600000", "2024-01-02", 10.0, "local")))

    assert store.delete_bars(source="local") == 1
    assert store.read_bars("SH", "600000").empty
    assert store.read_bars("SZ", "000001")["source"].tolist() == ["online"]


def test_delete_bars_honours_the_date_window(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.upsert_local_bars(
        _bars(
            ("SH", "600000", "2024-01-02", 10.0, "local"),
            ("SH", "600000", "2024-01-05", 11.0, "local"),
            ("SH", "600000", "2024-01-09", 12.0, "local"),
        ),
        price_basis="hfq",
    )

    removed = store.delete_bars(
        source="local",
        start_date=date(2024, 1, 3),
        end_date=date(2024, 1, 6),
    )

    assert removed == 1
    assert [str(value.date()) for value in store.read_bars("SH", "600000")["date"].tolist()] == [
        "2024-01-02",
        "2024-01-09",
    ]


def test_delete_bars_realigns_instrument_coverage(tmp_path: Path) -> None:
    """删除要同步覆盖元数据：库以为自己还有那段历史时，先删后下只补首尾两段。

    在线同步靠 ``instruments.first_date/last_date`` 判断"哪些区间已经有了"，所以
    删掉 daily_bars 却留着旧区间，等于让重下跳过中间那一段，而那段已经删没了。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.upsert_online_bars(
        _bars(
            ("SH", "600000", "2024-01-02", 10.0, "online"),
            ("SH", "600000", "2024-01-05", 11.0, "online"),
            ("SZ", "000001", "2024-01-05", 5.0, "online"),
        )
    )

    assert store.delete_bars(source="online", codes=["600000"]) == 2

    coverage = {ref.code: ref for ref in store.list_instruments(include_missing=True)}
    assert coverage["600000"].first_date is None
    assert coverage["600000"].last_date is None
    assert coverage["600000"].bar_count == 0
    # 没被删的标的保持原样：重算只该反映真实剩余的行。
    assert coverage["000001"].first_date == date(2024, 1, 5)
    assert coverage["000001"].last_date == date(2024, 1, 5)
    assert coverage["000001"].bar_count == 1


def test_reset_instrument_coverage_zeroes_stale_metadata(tmp_path: Path) -> None:
    """删除一行都没命中时也要能重置覆盖：上一次重刷删了没补回来就是这种状态。

    此时 ``delete_bars`` 不会触发重算（没删到行），留着旧区间会让这次重下只补
    首尾两段——而首尾两段对一只早已上市的票都是空的，等于什么都没下。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.upsert_online_bars(
        _bars(
            ("SH", "600000", "2024-01-02", 10.0, "online"),
            ("SZ", "000001", "2024-01-02", 5.0, "online"),
        )
    )

    assert store.reset_instrument_coverage([("SH", "600000")]) == 1

    coverage = {ref.code: ref for ref in store.list_instruments(include_missing=True)}
    assert (coverage["600000"].first_date, coverage["600000"].last_date) == (None, None)
    assert coverage["600000"].bar_count == 0
    # 没点名的标的不能被顺手清掉。
    assert coverage["000001"].first_date == date(2024, 1, 2)
    # 已经清过一次就没有可清的了。
    assert store.reset_instrument_coverage([("SH", "600000")]) == 0


def test_delete_bars_recomputes_coverage_from_remaining_rows(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.upsert_local_bars(
        _bars(
            ("SH", "600000", "2024-01-02", 10.0, "local"),
            ("SH", "600000", "2024-01-05", 11.0, "local"),
            ("SH", "600000", "2024-01-09", 12.0, "local"),
        ),
        price_basis="hfq",
    )

    removed = store.delete_bars(
        source="local",
        start_date=date(2024, 1, 3),
        end_date=date(2024, 1, 6),
    )

    assert removed == 1
    ref = next(ref for ref in store.list_instruments() if ref.code == "600000")
    assert (ref.first_date, ref.last_date, ref.bar_count) == (
        date(2024, 1, 2),
        date(2024, 1, 9),
        2,
    )


# --------------------------------------------------------------------------
# 试验台账（候选池 #20：跨运行取最好的那一次，需要记账才能校正）
# --------------------------------------------------------------------------


def test_research_trial_ledger_round_trip(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    assert store.list_research_trials() == []

    store.save_research_trial(
        "trial-1",
        data_fingerprint="abc123",
        start_date="2020-01-01",
        end_date="2024-12-31",
        tested_conditions=900,
    )
    rows = store.list_research_trials()

    assert len(rows) == 1
    assert rows[0]["history_id"] == "trial-1"
    assert rows[0]["data_fingerprint"] == "abc123"
    assert rows[0]["start_date"] == "2020-01-01"
    assert rows[0]["end_date"] == "2024-12-31"
    assert rows[0]["tested_conditions"] == 900
    # 时间要能读回来做排序，不能是个字符串。
    assert isinstance(rows[0]["created_at"], datetime)


def test_research_trial_ledger_is_not_capped_by_history_limit(tmp_path: Path) -> None:
    """台账是累计计数：历史记录被淘汰不该把轮数一起抹掉。

    删掉一条历史不会撤销「用户当时看过它给的最好结果」，选择偏差还在。跟着
    backtest_history 截到 20 条会让 K 少算，Šidák 校正就不足——正好是把 #20 想
    防的东西又放回去。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    for index in range(BACKTEST_HISTORY_LIMIT + 5):
        store.save_research_trial(
            f"trial-{index}",
            data_fingerprint=f"fp-{index}",
            start_date="2020-01-01",
            end_date="2024-12-31",
            tested_conditions=100,
        )

    assert len(store.list_research_trials()) == BACKTEST_HISTORY_LIMIT + 5


def test_research_trial_same_id_overwrites(tmp_path: Path) -> None:
    """同一条历史重跑只更新，不累加成两条——否则轮数会被自己抬高。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    for count in (900, 1200):
        store.save_research_trial(
            "trial-1",
            data_fingerprint="abc123",
            start_date="2020-01-01",
            end_date="2024-12-31",
            tested_conditions=count,
        )

    rows = store.list_research_trials()
    assert len(rows) == 1
    assert rows[0]["tested_conditions"] == 1200


def test_research_trial_rejects_bad_input(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    with pytest.raises(ValueError, match="研究历史 ID 不能为空"):
        store.save_research_trial(
            "  ",
            data_fingerprint="fp",
            start_date="2020-01-01",
            end_date="2024-12-31",
            tested_conditions=10,
        )
    with pytest.raises(ValueError, match="开始日期不能晚于结束日期"):
        store.save_research_trial(
            "trial-1",
            data_fingerprint="fp",
            start_date="2024-12-31",
            end_date="2020-01-01",
            tested_conditions=10,
        )
    with pytest.raises(ValueError, match="检验条件数不能为负数"):
        store.save_research_trial(
            "trial-1",
            data_fingerprint="fp",
            start_date="2020-01-01",
            end_date="2024-12-31",
            tested_conditions=-1,
        )


def test_research_trial_reset_returns_removed_count(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    for index in range(3):
        store.save_research_trial(
            f"trial-{index}",
            data_fingerprint="fp",
            start_date="2020-01-01",
            end_date="2024-12-31",
            tested_conditions=100,
        )

    assert store.reset_research_trials() == 3
    assert store.list_research_trials() == []
    # 再清一次是 0 而不是报错：重置按钮可能被连点。
    assert store.reset_research_trials() == 0


def test_latest_trade_date_reports_the_newest_completed_bar(tmp_path: Path) -> None:
    """行情是手动同步的，最新交易日几乎从不等于今天。

    任何按"从某天到现在"取窗口的调用方都得先问它：窗口终点一律取 ``today`` 的话，
    入组晚于最新行情时整个区间为空，而空区间会被误读成"库里没有行情"，再把原因
    错怪到标的上（说它们在起点没有收盘价）。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    # 空库要给 None，不能给今天——否则调用方会以为库里已经有数据了。
    assert store.latest_trade_date() is None

    store.upsert_local_bars(
        _bars(
            ("SH", "600000", "2024-01-02", 10.0, "local"),
            ("SH", "600000", "2024-01-05", 11.0, "local"),
        ),
        price_basis="hfq",
    )
    assert store.latest_trade_date() == date(2024, 1, 5)

    # 后写的一天更早，不该把最大值拉回去。
    store.upsert_local_bars(
        _bars(("SH", "600000", "2024-01-03", 9.0, "local")), price_basis="hfq"
    )
    assert store.latest_trade_date() == date(2024, 1, 5)


def test_market_trading_calendar_persists_sessions_and_exposes_sync_check_state(
    tmp_path: Path,
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    sessions = [date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 9)]

    assert (
        store.save_market_trading_calendar(
            sessions,
            source="eastmoney_sse_composite",
        )
        == 3
    )
    assert store.latest_market_trading_date(date(2026, 10, 2)) == date(2026, 9, 30)
    assert store.latest_market_trading_date(date(2026, 10, 9)) == date(2026, 10, 9)

    store.set_meta("last_auto_sync_check_date", "2026-10-02")
    store.set_meta("last_auto_sync_check_session", "2026-09-30")
    status = store.status()

    assert status.market_calendar_latest_date == "2026-10-09"
    assert status.market_calendar_source == "eastmoney_sse_composite"
    assert status.market_calendar_updated_at is not None
    assert status.last_auto_sync_check_date == "2026-10-02"
    assert status.last_auto_sync_check_session == "2026-09-30"
