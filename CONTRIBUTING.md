# 贡献指南

感谢关注 Indicator Lab！本文说明如何搭建开发环境、遵循哪些约定，以及提交贡献时你需要知道的事项。

## 开发环境

- Python 3.10–3.13（发布锁定 3.12），Node 22.22+（CI 使用 Node 24）；
- 后端依赖（含哈希校验的运行时清单 + 开发/目录工具）：

  ```bash
  python3 -m pip install --require-hashes -r requirements.txt
  python3 -m pip install -e ".[dev,catalog]"
  ```

- 前端依赖：`cd web-ui && npm ci`；
- 直接依赖的版本范围维护在 `requirements.in`，用 Python 3.12 环境的 pip-tools 重新生成锁文件并一起提交（详见 [docs/packaging.md](docs/packaging.md)）。

## 本地运行

```bash
# 后端（热重载）
uvicorn selector_app.web.app:app --reload --host 127.0.0.1 --port 8123
# 或
indicator-lab --reload

# 前端（Vite 开发服务器）
cd web-ui && npm run dev
```

完整容器化运行见 [独立分发部署](docs/standalone-deployment.md)。

## 目录结构

- `selector_app/`：Python 后端，按领域分包；`selector_app/web/` 为 FastAPI 装配、路由与 schema；
- `tests/unit/` 与 `tests/integration/`：后端测试；
- `web-ui/src/`：Vue 3 前端，组件测试与页面同目录，Playwright E2E 在 `web-ui/e2e/`；
- `scripts/`：构建、验证、打包与维护脚本；`docs/`：产品与架构文档。

## 代码风格

- Python：4 空格缩进、`snake_case` 模块与函数、Ruff 行宽 100；Ruff 检查 `E`、`F`、`I`、`UP`；mypy 以 strict 模式运行；
- 测试命名为 `test_*.py`，放到对应的 unit 或 integration 区域；Python 覆盖率保持 ≥ 80%；
- 前端组件使用 PascalCase `.vue` 命名；Vitest 规格文件用 `.spec.ts`。

## 验证

提交前按改动范围跑对应检查（依赖需提前安装）：

```bash
sh scripts/verify_ci.sh python      # pytest + 覆盖率、Ruff format/check、mypy
sh scripts/verify_ci.sh frontend    # Vitest、类型检查、构建、Playwright E2E
sh scripts/verify_ci.sh docker      # Compose 配置与镜像构建
sh scripts/verify_ci.sh             # 全部
```

## 提交规范

遵循仓库历史的短前缀风格：`feat:`、`fix:`、`refactor:`、`test:`、`docs:`、`chore:`，摘要用简洁的中文一句话说清用户可见的变化。

## Pull Request

新建 PR 时会看到模板，请填写：

- **用户可见的行为变化**：这个 PR 让使用者的哪一步体验发生了什么改变；
- **验证方式与结果**：跑过哪些命令、结果如何；
- **schema / 数据迁移影响**：涉及表结构、数据迁移或备份策略时必须说明，并附回归测试；
- UI 变更请附截图。

## 配置与数据安全

- `.env.example` 是配置项的唯一参考，新增配置必须同步更新它；
- **不要提交**：密钥与 token、本地 DuckDB 数据文件、导入的行情数据、缓存、构建产物（`data/vipdoc/` 下的本地行情文件一律不入库）；
- 涉及 schema 的改动需要说明迁移与备份影响，并补充回归覆盖。

## 许可与贡献授权

本项目以 [GNU AGPL-3.0](LICENSE) 发布。欢迎提交 Issue 与 Pull Request。**向本项目提交 PR 即视为你同意：该贡献以 AGPL-3.0 授权项目使用，同时授权项目维护者将该贡献随项目以其他许可证（包括未来的商业许可）发布。** 你保留对自身贡献的完整版权，本条款不改变你对自己原始作品的权利。

## 行为准则

保持专业与友善：讨论针对问题本身，尊重不同的技术观点，不给任何人贴标签。
