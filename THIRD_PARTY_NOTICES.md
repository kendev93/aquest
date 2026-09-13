# Third-party notices

## TDX 协议和 `.day` 解析参考

本节中的上游项目名称仅用于许可证归属和技术参考，不代表指标实验室与任何上游项目存在品牌或官方关联。

本项目运行时不依赖 `easy-tdx`。`selector_app/tdx_protocol/` 和
`selector_app/market_data/day_format.py` 的协议字段、`.day` 记录格式、价格/成交量系数和部分容错行为，参考并改写自 MIT 许可的
`easy-tdx`/`pytdx`/`xmtdx` 相关实现与测试；项目没有把上游包作为依赖，也没有在运行时导入其模块。

上游项目及其相关声明：

- [`easy_tdx`](https://github.com/handsomejustin/easy_tdx)
- [`pytdx`](https://github.com/rainx/pytdx)
- [`xmtdx`](https://github.com/rainx/xmtdx)

```text
MIT License

Copyright (c) 2025-present easy-tdx contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

easy-tdx also documents inspiration/attribution for `pytdx` and `xmtdx`; their MIT notices remain applicable to the portions derived from those references. The corresponding upstream license and notice files should be consulted for complete attribution text.

## DuckDB

本项目使用 DuckDB Python 包作为嵌入式本地数据库。DuckDB 以 MIT License 发布；其完整许可证文本随安装包和官方仓库分发。本项目没有修改 DuckDB 源码。

## AKShare

本项目使用 [AKShare](https://github.com/akfamily/akshare) 作为默认股票在线行情 provider，仅调用其公开 Python 接口并将结果转换为项目自己的行情模型；没有修改 AKShare 源码。AKShare 以 MIT License 发布，完整许可证文本随安装包和官方仓库分发。

## 本项目许可证

本项目自身代码为陈垦版权所有的商业专有软件，具体授权条件见根目录 [LICENSE](LICENSE)。本项目许可证不改变任何第三方依赖原有的许可证义务。
