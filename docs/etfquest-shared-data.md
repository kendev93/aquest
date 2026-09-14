# ETFQuest 共享行情模式

ETFQuest 是行情目录、行情下载和 `market_data.db` 的唯一写入方。Indicator
Lab 只读 ETFQuest 的 `market_data`、`trade_calendar`、`asset_info` 和授权设置，
自己的 DuckDB 仍保存策略历史、指标缓存与任务数据。

## Docker 启动

先在 ETFQuest 中完成机器授权和“同步全市场日线”，再以只读方式挂载其
`database` 目录：

```bash
export ETFQUEST_MARKET_DATABASE_DIR="$HOME/Library/Application Support/ETFQuest/database"
docker compose \
  -f docker-compose.yml \
  -f docker-compose.protected.yml \
  -f docker-compose.etfquest.yml \
  up -d
```

Windows 下将变量设置为 ETFQuest 的实际目录，例如
`C:/ProgramData/ETFQuest/database`。不要把 `market_data.db` 以可写方式挂载给
Indicator Lab，也不要让 Indicator Lab 对该文件执行迁移、压缩或备份。

如果 ETFQuest 数据库不存在、尚未保存机器码，或会员状态无法通过
ETFQuest-server 验证，Indicator Lab 只保留健康检查、授权状态和升级提示。
