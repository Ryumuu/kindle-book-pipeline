---
name: ebook-translate
description: "外语电子书（EPUB）全书高保真翻译与精排制作助手。支持解包分章、断点续译、默认中文精排及可选中英对照，并自动编译为 EPUB/AZW3 归档或推送到 Kindle。用户第一次用或说「开始用 kindle-book-pipeline」时先走首次使用向导。"
---

# 电子书高保真全书翻译助手 (ebook-translate)

## 首次使用

每次调用先在本 Skill 目录运行 `python3 scripts/pipeline_setup.py status --json`，按[首次使用向导](references/onboarding.md)处理：已有设置就一句话提醒后继续；没有设置先按用途问卷，用户明确跳过可用内置默认。「改设置」「体检一下」也走该向导。


将外语原版 EPUB 电子书全自动化或半自动化翻译为高质量中文精排版或中英对照版，并生成适配墨水屏与移动设备的 EPUB 与 AZW3 成品。

所有转换后的成品电子书默认统一保存至：`~/Documents/KindleBooks`。

---

## 核心设计与执行原则

1. **【100% 完整忠实翻译】**：
   - 严禁擅自删节、概括、缩写或总结；
   - 必须逐段完整忠实翻译，保留原文的所有段落、标题层级、引用块、列表与插图；
   - 关键经济学/社会学/技术专业术语统一标准译法，专有名词可采用“中文译名（Original Name）”首次标注。

2. **【断点续译与状态管理 (Zero Waste)】**：
   - 全书按真实目录结构拆分为独立的章节切片（`manifest.json`）；
   - 翻译每完成一章立刻持久化落盘至 `chapters/<slug>.md` 并更新状态；
   - 任务中断、模型超时或会话重启时，自动识别已完成章节并无缝续跑，绝对不重复翻译已完成章节。

3. **【灵活输出模式】**：
   - **纯中文精排版**（默认）：流畅自然的高品质中文全译本，适合沉浸式阅读；
   - **中英对照版**（`pack --bilingual`）：由 Agent 按“英文原文一段、紧跟中文一段”写好，脚本识别原文标记并排版，不自动翻译或按段对齐。

4. **【全自动下游联动】**：
   - 编译产物自动归档至 `~/Documents/KindleBooks/`；
   - 可一键联动 `ebook-to-kindle` 无线推送到用户的 Kindle 墨水屏设备。

---

## 核心工作流与命令

以下命令在 kindle-book-pipeline 仓库根目录执行。底层 CLI 脚本位置：`ebook-translate/scripts/ebook_translate.py`

### 1. 解构书籍与建立工作区 (Unpack)
```bash
# 解包 EPUB 到默认工作区 (~/.local/share/ebook-translate-workspaces/<book_slug>)
python3 ebook-translate/scripts/ebook_translate.py unpack "/path/to/book.epub"

# 或指定专属工作区
python3 ebook-translate/scripts/ebook_translate.py unpack "/path/to/book.epub" --workspace "/path/to/workspace"
```
* 执行后按 EPUB 阅读顺序拆分，优先使用 NCX 目录名，否则尝试正文标题；配图按 ZIP 内规范化完整路径提取为 `images/<ZIP内路径>`（如 `images/OPS/art/plate.svg`），保留目录层级与原始图片内容，原书各章节分离至 `source/chXX.txt`，并生成 `manifest.json`。
* `manifest.json` 的 `spine_results` 逐项记录阅读页的保留或空白跳过结果。独立 SVG 阅读页及其他非 HTML 类型暂不支持，会列出文件并报错退出，不能当作完整书继续；正文内嵌 SVG 仍保留。
* `manifest.json` 的 `image_map` 记录 ZIP 内完整图片路径到工作区图片路径的映射；`source/` 只改真实标签的 `src`、`href`、`xlink:href`、`srcset` 与 `style` 属性的 `url(...)` 中能匹配映射的图片地址，正文文字、注释、CDATA 和其他链接保持原样。译稿请复制映射中的实际路径（URL 中的空格等需百分号编码），不要只取文件名；重跑提取若目标内容不同或路径大小写冲突会报错，不会覆盖。同目录仅大小写不同的图片属于非法 EPUB（OPF-060），会拒绝提取。
* SVG 内部指向已提取图片的相对链接依靠保留的目录解析，提取时不改 SVG 原始内容。打包同样保留资源目录层级，包含仅被 SVG 引用的图片；Calibre 可能调整资源文件名，并同步更新成品内部引用。不处理 SVG 依赖的字体或样式表。外部 CSS 文件中的背景图不做引用改写；仅通过 CSS 背景显示的封面/页面可能被判为空白，不保证保留。Markdown 编译不自动还原原书的响应式图片或 CSS 布局，Agent 需按映射显式保留所需图片。


旧版 `images/image-<散列>.*` 工作区不会自动迁移图片引用。升级后请新建工作区，核对章节对应关系，再按新的 `image_map` 更新并复制译稿；不要只在旧工作区重跑 unpack 后直接编译。

工作区绑定源 EPUB 的 SHA-256（内容散列）。换书或修改原书必须换一个 `--workspace`，
否则会报错并在覆盖文件前退出；也可由用户手动清理旧工作区后重新开始。
相同源文件再次 unpack 会保留已有译稿。pack 前会重新核对原书，因此 `source_path`
指向的原 EPUB 必须仍然可读且内容不变。没有散列的旧工作区拒绝直接复用/编译：
请先新建工作区，逐章核对原文和章节对应关系后再复制旧译稿，不要直接给旧清单补散列。

### 2. 检查翻译状态与进度 (Status)
```bash
python3 ebook-translate/scripts/ebook_translate.py status --workspace "/path/to/workspace"
```
* 输出清晰的章节表格、各章字数、已完成/待翻译状态及整体百分比。

### 3. Agent 执行逐章高保真翻译
Agent 调度翻译时：
1. 读取 `manifest.json` 中处于 `pending` 状态的章节源文件（`source/chXX.txt`）；
2. 翻译为 Markdown 格式并保留插图标记（例如 `![描述](images/OPS/art/plate.png)`）；
3. 将翻译结果写入对应的 `chapters/chXX.md`；
4. 批量或全书翻译完成后即可进行打包。

### 4. 编译精排成品与推送到 Kindle (Pack)
```bash
# 编译全书并输出 EPUB 与 AZW3 至 ~/Documents/KindleBooks
python3 ebook-translate/scripts/ebook_translate.py pack --workspace "/path/to/workspace"

# 编译并直接推送到 Kindle 墨水屏
python3 ebook-translate/scripts/ebook_translate.py pack --workspace "/path/to/workspace" --push-kindle

# 部分章节预览（允许未全部翻译完成时编译预览版）
python3 ebook-translate/scripts/ebook_translate.py pack --workspace "/path/to/workspace" --allow-partial
```

---

### 中英对照模式：Agent 写法与编译

纯中文版保持默认：`chapters/<slug>.md` 只写中文，原有 pack 命令不加选项即可。
中英对照版请另建工作区（例如 `workspace-bilingual`），不要覆盖纯中文版的章节文件：

```bash
python3 ebook-translate/scripts/ebook_translate.py unpack "/path/to/book.epub" --workspace "/path/to/workspace-bilingual"
```

Agent 按源文顺序逐段写入该工作区的 `chapters/<slug>.md`。每个英文段落用
`[EN] ` 开头（含一个空格），紧接下一段中文。每个原文段、译文段各占一个物理行，
段间可留空行；长段落不要手动折行。标题用原有 Markdown 标题，图片仍用 `![描述](images/OPS/art/plate.png)`。
引用、诗歌或列表要对照时，同样按段写为 `[EN] 原文` 加普通中文段，不把 `[EN]` 嵌入 `>` 引用块。

```markdown
## 第一章　掉进兔子洞

[EN] In another moment down went Alice after it, never once considering how in the world she was to get out again.

转眼间，爱丽丝也跟着钻了进去，压根儿没想过自己以后究竟该怎么出来。
```

```bash
python3 ebook-translate/scripts/ebook_translate.py status --workspace "/path/to/workspace-bilingual"
python3 ebook-translate/scripts/ebook_translate.py pack --workspace "/path/to/workspace-bilingual" --bilingual
# 仅预览已翻译章节时，在上一条命令后加 --allow-partial
```

原文输出为 `p.bilingual-original`（0.9em、深灰），中文为 `p.bilingual-translation`
（正常字号和正文颜色），方便墨水屏分辨。书名及文件名后缀为「（中英对照版）」，
不会覆盖原有「 (中文精排版)」EPUB/AZW3；中间 HTML 也单独保存为 `master_book-bilingual.html`。
两种模式均支持 `--formats epub`、`--formats azw3` 或默认两者。

脚本不检查译文是否准确或段落是否配对；Agent 必须核对原文段与中文段一一对应、无遗漏、顺序一致。
`status` 仍按章节文件非空判断完成，不代表译文已验收。
模式只由 pack 的 `--bilingual` 指定，manifest 不记录模式；不带此选项会按普通文本显示 `[EN]`。
请始终把纯中文和对照译稿放在不同工作区，并在每次编译对照版时带上此选项。

---

## 组合与联动规范 (与找书 Skill 搭配)

可接在本仓库 `book-search` 后面用：
1. `book-search` 负责检索、对比和下载，默认保存于 `~/Downloads/Books/`。
2. 用户需要中文阅读时，将下载的 EPUB 交给本 Skill：
   ```bash
   python3 ebook-translate/scripts/ebook_translate.py unpack "$SAVED_PATH"
   ```
3. Agent 完整翻译后编译，默认保存至 `~/Documents/KindleBooks/`。只有用户要求时才使用 `--push-kindle`。

编译先查找 `EBOOK_TO_KINDLE_RUNTIME`（默认 `~/.local/share/ebook-to-kindle`）下的
`core/Calibre.app/Contents/MacOS/ebook-convert`，再查 PATH 中的 `ebook-convert`。
找不到会明确报错；本工具不会自动安装。推送脚本取同仓库的
`ebook-to-kindle/scripts/send_to_kindle.py`，不依赖全局 Skill 安装位置。
