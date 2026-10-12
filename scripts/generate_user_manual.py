"""把页面说明数据源渲染成 Markdown 用户手册。

用法（在仓库任意位置执行均可）：

    python3 scripts/generate_user_manual.py

数据源是 ``web-ui/src/data/pageGuides.json`` —— 页面上的「这个功能怎么用」区块读的是
同一份文件。所以手册和界面永远不会各说各话：改口径只需要改那一个 JSON，再跑一次这个
脚本。反过来，**不要直接编辑 docs/user-manual.md**，下次生成会被覆盖。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "web-ui" / "src" / "data" / "pageGuides.json"
TARGET = REPO_ROOT / "docs" / "user-manual.md"

# 章节出现的顺序就是导航里的顺序：先是日常选股，再看验证，最后是翻旧账。
SECTION_ORDER = (
    "formula-screen",
    "condition-research",
    "portfolio-backtest",
    "strategy-fitness",
    "strategy-lab",
    "strategy-tracking",
    "swing-research",
    "individual-research",
    "strategy-pool",
    "factor-test",
    "factor-attribution",
    "candidate-library",
    "instrument-lab",
    "history-compare",
    "market-data",
    # 手册页自己是索引，放最后：先读功能，再读「这些说明是从哪来的」。
    "user-manual",
)

# 每个 Section 顶部的「这一段归哪一类」，帮第一次读的人建立地图。
SECTION_GROUP = {
    "formula-screen": "筛选与挖掘",
    "condition-research": "筛选与挖掘",
    "portfolio-backtest": "回测与验证",
    "strategy-fitness": "回测与验证",
    "strategy-lab": "回测与验证",
    "strategy-tracking": "回测与验证",
    "swing-research": "规律研究",
    "individual-research": "规律研究",
    "strategy-pool": "回测与验证",
    "factor-test": "规律研究",
    "factor-attribution": "规律研究",
    "candidate-library": "沉淀与复用",
    "instrument-lab": "沉淀与复用",
    "history-compare": "沉淀与复用",
    "market-data": "数据",
    "user-manual": "附录",
}

HEADINGS = (
    ("useCases", "什么时候用它"),
    ("steps", "操作步骤"),
    ("answers", "它能回答什么"),
    ("limits", "它不能回答什么"),
    ("caliber", "口径提醒"),
)


def bullet_lines(items: Iterable[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def ordered_lines(items: Iterable[str]) -> str:
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1))


def render() -> str:
    payload = json.loads(SOURCE.read_text(encoding="utf-8"))
    guides = {item["key"]: item for item in payload["guides"]}

    missing = [key for key in SECTION_ORDER if key not in guides]
    extra = [key for key in guides if key not in SECTION_ORDER]
    if missing or extra:
        raise SystemExit(
            f"数据源与手册章节不一致：缺 {missing}，多 {extra}。请把 SECTION_ORDER 和 "
            f"pageGuides.json 对齐，避免有的页面没被写进手册。"
        )

    parts: list[str] = [
        "# 用户手册",
        "",
        "> 本文件由 `scripts/generate_user_manual.py` 自动生成，**不要直接编辑**。",
        "> 想改内容请改 `web-ui/src/data/pageGuides.json`（页面上的「这个功能怎么用」区块",
        "> 读的是同一份文件），然后执行：",
        ">",
        "> ```bash",
        "> python3 scripts/generate_user_manual.py",
        "> ```",
        "",
        "A股策略研究是一个**日线级别的研究工具**：它帮你在 A 股日线上确认一个想法有没有证据，",
        "它不连接券商、不产生下单。这一点决定了下面所有功能的边界——每个页面都明确写出了",
        "「它能回答什么」和「它不能回答什么」，读的时候请特别留意后者。",
        "",
        "## 这些页面怎么串起来",
        "",
        "| 阶段 | 页面 | 在这一步做的事 |",
        "| :--- | :--- | :--- |",
        "| 有想法 | 公式选股 | 看这个想法今天能筛出多少票 |",
        "| 验证想法 | 买卖条件研究 / 波段研究 / 因子检验 | 用置换检验判断它是不是运气 |",
        "| 配成策略 | 组合回测 / 策略适配性 | 把规则变成可执行的组合，算扣费后的收益 |",
        "| 放手不管 | 策略实验室 | 让系统搜索参数组合（记得榜单自带挑选偏差） |",
        "| 往前观察 | 策略追踪 | 规则冻结后，看它在没见过的数据上还灵不灵 |",
        "| 事后复盘 | 因子归因 / 历史对比 | 解释为什么这次赚了、为什么这版比那版好 |",
        "| 沉淀 | 候选库 | 把带完整证据的条件留下来，之后还能找回去 |",
        "",
        "## 全站共通的三条口径",
        "",
        # 下面这几条每条必须输出成单独一行：Markdown 的软换行会被渲染成空格，
        # 中文之间被塞进空格很难看。用括号内的隐式拼接绕开源码行宽限制。
        "1. **只做 A 股日线**。主板、科创板、创业板，不含 ETF 与基金、指数、债券、B 股；"
        "也没有分钟数据，所以盘中级别的策略没法在这里验证。",
        "2. **信号收盘确认、次日成交**。回测里的收益不等于「今天看到就买到」的收益，"
        "这是所有回测页面的默认假设。",
        "3. **凡是看上去不错的发现，都要先问它能不能被随机解释**。各处用到的置换检验、"
        "错误发现率校正，以及「这到底是第几次试出来的」这类追问，意义都在这一条上，"
        "页面不会替你省略它们。",
        "",
        "---",
        "",
    ]

    for index, key in enumerate(SECTION_ORDER, start=1):
        guide = guides[key]
        parts.append(f"## {index}. {guide['title']}（{guide['path']}）")
        parts.append("")
        parts.append(f"*{SECTION_GROUP[key]}*")
        parts.append("")
        parts.append(guide["summary"])
        parts.append("")
        for field, heading in HEADINGS:
            items = guide.get(field, [])
            if not items:
                continue
            parts.append(f"### {heading}")
            parts.append("")
            if field == "steps":
                parts.append(ordered_lines(items))
            else:
                parts.append(bullet_lines(items))
            parts.append("")
        next_steps = guide.get("next", [])
        if next_steps:
            parts.append("### 下一步")
            parts.append("")
            for item in next_steps:
                parts.append(f"- [{item['label']}]({item['path']})")
            parts.append("")
        parts.append("---")
        parts.append("")

    parts.append("## 术语")
    parts.append("")
    parts.append(
        "界面上的中文术语后面如果带一个小问号，鼠标悬停会给出英文原名和白话解释。这些解释"
        "集中放在 `web-ui/src/utils/terms.ts` 里，和这里的手册是两回事——那里是给正在操作的人"
        "看的即时提示，这里是给愿意完整读一遍的人看的。"
    )
    parts.append("")

    return "\n".join(parts)


def main() -> None:
    TARGET.write_text(render(), encoding="utf-8")
    print(f"已生成 {TARGET.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
