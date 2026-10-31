---
name: book-search
description: "多平台检索、对比并下载电子书（内置 Project Gutenberg 与 Z-Library，可自行添加平台），并做后续研读处理（提取大纲、转 Markdown、推送 Kindle）时使用。用户第一次用、说「开始用 kindle-book-pipeline」「改设置」「体检一下」时，也用本 Skill 走首次使用向导。"
---

# 多平台找书与研读助手 (book-search)

## 首次使用

每次调用先在本 Skill 目录运行 `python3 scripts/pipeline_setup.py status --json`，按[首次使用向导](references/onboarding.md)处理：已有设置就一句话提醒后继续；没有设置先按用途问卷，用户明确跳过可用内置默认。「改设置」「体检一下」也走该向导。


从 **检索 → 多版本对比甄别 → 下载 → 后处理（大纲、Markdown、推送阅读设备）** 的完整闭环。

找书平台是可插拔的「平台插件」(provider)：

| 平台 | 名字 | 需要登录 | 说明 |
| :--- | :--- | :--- | :--- |
| Project Gutenberg | `gutenberg` | 否 | 公版书，只有 EPUB，经 Gutendex 接口 |
| Z-Library | `zlibrary` | 是 | 用你自己的账号与每日下载额度 |
| 你自己加的 | 任意 | 自定 | 见下文「添加平台」 |

所有命令都在本 Skill 目录下执行：`python3 scripts/book_search.py ...`（只用 Python 标准库）。

> 只下载你有权获取的内容；各平台的使用条款与所在地法律由使用者自行遵守。

---

## 🚨 Agent 行为硬性门禁

1. **严禁盲目下载第一项**：第 1 条结果往往不是最佳版本（老版、扫描件、残卷）。搜出结果后不得立刻下载。
2. **额度保护**：有下载额度的平台（如 Z-Library 免费账号每天 10 本）每次下载不可逆，必须用在综合质量最高的版本上。下载前先 `info` 看剩余额度。
3. **四阶段 SOP**：
   * **Phase 1 检索**：拿到 3~8 个候选的完整元数据（可跨平台）。
   * **Phase 2 对比**：按下方维度对比，向用户列出对比表并说明推荐理由。
   * **Phase 3 下载**：对比后（或用户指定编号）才下载。
   * **Phase 4 后处理**：大纲、导读、转 Markdown、全书翻译 (ebook-translate) 或推送 Kindle。

---

## 核心命令

### 1. 查看可用平台 / 额度
```bash
python3 scripts/book_search.py providers          # 列出平台、是否可用、插件文件位置
python3 scripts/book_search.py info               # 各平台账号与额度（如 Z-Library 剩余下载数）
```

### 2. 检索
```bash
# 默认搜索所有可用平台（未登录的平台自动跳过并提示）
python3 scripts/book_search.py search "<书名/作者/ISBN>"

# 指定平台、语言、格式、每个平台条数
python3 scripts/book_search.py search "<书名>" --provider zlibrary,gutenberg --lang chinese --ext epub --limit 5

# 原始 JSON（便于 Agent 解析）：{"results": [...], "errors": {平台: 原因}}
python3 scripts/book_search.py search "<书名>" --json
```
每条结果都带一个 `ref`，形如 `gutenberg:1342` 或 `zlibrary:<id>/<hash>`，下载时原样传入。

### 3. 多版本对比标准
1. **格式**：EPUB 最优（可重排、体积小、易提取文本）> 文字版 PDF（图表公式最保真）> 扫描版 PDF（体积大、不可搜索，非必要不选）。
2. **版次与年份**：优先最新修订版。
3. **体积合理性**：< 500KB 多为样章或书评，剔除；> 200MB 的 PDF 多为未压缩扫描件，谨慎。
4. **语言与出版社**：优先正规出版社。
5. **平台**：同一本书公版平台（Gutenberg）有就优先用，不消耗额度。

输出给用户的对比表：
```markdown
| 候选 | 平台 | 版次/年份 | 格式 | 大小 | 出版社 | 评级 | 推荐理由与缺陷 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| #1 | zlibrary | 2023 (第3版) | EPUB | 8.2 MB | 某出版社 | ★★★★★ 首选 | 最新修订，可重排，体积小 |
| #2 | zlibrary | 2016 (第2版) | PDF | 344 MB | 某出版社 | ★★☆☆☆ | 老版本且为扫描件 |
```

### 4. 下载
```bash
python3 scripts/book_search.py download <ref> [--out-dir <目录>]
```
* 默认保存到 `$BOOK_SEARCH_DOWNLOAD_DIR`，未设置时为 `~/Downloads/Books/`。
* 不会覆盖已有文件，同名自动加 `(2)`。
* 成功时最后一行输出 `SAVED_PATH=/path/to/file.ext`。

### 5. 后处理
1. **大纲与导读**：EPUB 可读 NCX/nav 提取目录树。
2. **转 Markdown**：如有 Markdown 转换工具可用，转重点章节做笔记。
3. **外文整书翻译**：若下载为外文书籍且需中文精排，联动本仓库的 `ebook-translate` 进行分章高保真断点续译：
   ```bash
   python3 ebook-translate/scripts/ebook_translate.py unpack "<下载文件路径>"
   ```
4. **推送 Kindle**：联动本仓库的 `ebook-to-kindle` 技能转 AZW3 或推送（成品默认存到 `~/Documents/KindleBooks`）。

---

## Z-Library 登录

凭证读取顺序：环境变量 `ZLIB_REMIX_USERID` / `ZLIB_REMIX_USERKEY`（可选 `ZLIB_BASE_URL`）→ `~/.config/zlibrary/config.json`。

macOS + Chrome 用户可一键同步：先在 Chrome 打开已登录的 Z-Library 页面，并开启 Chrome 菜单「显示 → 开发者 → 允许 Apple 事件中的 JavaScript」，然后：
```bash
python3 scripts/book_search.py zlib-sync-cookie
```
凭证以 600 权限写入配置文件。遇到 401/403 或登录失效时重跑一次。

---

## 添加平台

1. 复制 `examples/my_provider.py` 到 `~/.config/kindle-book-pipeline/providers/<名字>.py`（或放进 `$BOOK_SEARCH_PROVIDER_PATH` 列出的任意目录，多个目录用 `:` 分隔）。
2. 改 `name`，实现 `search()` 返回 `Book` 列表、`download()` 返回保存路径；需要登录的平台在 `is_ready()` 里返回 `(False, "原因")`。
3. `python3 scripts/book_search.py providers` 确认已加载。

* ⚠️ 插件就是普通 Python 代码，以你的用户权限运行：能读写本机文件、读环境变量、联网，而且只要运行 `book_search.py`（哪怕只是 `providers`）就会执行。只放你自己写的或读过、信得过的插件文件。
* 和内置平台同名会替换内置平台（可借此改接口地址或镜像）。
* 某个插件文件报错只会警告并跳过，不影响其他平台。
* 以 `_` 开头的文件不加载，可用来暂时停用。
