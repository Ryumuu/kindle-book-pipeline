# Markdown Kindle Fixture

## 第一章：结构

这是一份用于验证 Markdown 输入的本地样本。正文同时包含中文与 English tokens，确保内容回读门不会把空壳电子书当作成功。

## 第二章：边界

- 只读取本地文件。
- 不调用云端 OCR 或大模型。
- 不自动连接 Kindle 设备或账号。

结尾标记：MARKDOWN-END-20260811。

## 第三章：常见语法

[引用文字][规则链接]与[快捷引用]都应只保留可见标签，不应把定义 URL 当正文。

[规则链接]: https://example.invalid/rule
[快捷引用]: https://example.invalid/shortcut

| 项目 | 结果 |
| --- | --- |
| 引用定义 | 不进入正文 |
| 原始 HTML | 保留单元格文字 |

<table><tr><td>原始HTML单元格</td><td>可见内容</td></tr></table>
