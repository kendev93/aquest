"""榜单模拟盘的状态表，拆库时和策略状态一起迁移。"""

ARCHIVE_DDL = """
CREATE TABLE IF NOT EXISTS pool_simulation_signals (
    trade_date DATE, objective VARCHAR, source_job VARCHAR NOT NULL,
    entries_json VARCHAR NOT NULL, archived_at TIMESTAMP NOT NULL,
    PRIMARY KEY (trade_date, objective)
)
"""

SIMULATION_DDL = """
CREATE TABLE IF NOT EXISTS pool_simulations (
    simulation_id VARCHAR PRIMARY KEY, config_json VARCHAR NOT NULL,
    state_json VARCHAR NOT NULL
)
"""
