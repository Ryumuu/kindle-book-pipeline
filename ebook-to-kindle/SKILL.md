---
name: ebook-to-kindle
description: "制作 Paperwhite 2 电子书或按用户要求发送到 Kindle 时使用；不用于下载或去除 DRM。"
---

<!-- Detailed scope moved from catalog metadata. -->
Use when: user asks to convert a local PDF, DOCX, HTML, TXT, Markdown, or EPUB file into Paperwhite 2-targeted AZW3 or MOBI6, reuse an explicit private single-book workspace, explicitly localize EPUB footnotes with GPT-5.3-Codex-Spark, or wirelessly push converted ebooks to a registered Kindle device via Amazon Send to Kindle; off-device gates run automatically and changed bytes require a device check. Not for: searching/downloading books, whole-book translation, or DRM removal.



# Ebook to Kindle

遇到“自动检查通过但仍不好读”、扫描书净化或批量交付时，先读 [真实阅读验收](references/actual-reading-acceptance.md)，用代表样本核对阅读结果，再批量处理；现有设备批准条件保持不变。

把用户已获授权的本地文档离线转换为适合 Kindle Paperwhite 2 的可重排电子书。默认输出 AZW3；MOBI 必须使用旧式 MOBI6。成品默认保存到 `~/Documents/KindleBooks`，可用环境变量 `EBOOK_TO_KINDLE_OUTPUT_DIR` 或 `--output-dir` 改。

## 工作流

1. 首次使用或运行时变更后先运行：

   ```bash
   python3 scripts/ebook_to_kindle.py doctor --deep
   ```

2. 若返回 `DEPENDENCY_MISSING`，先运行 `bootstrap_runtime.py --print-plan`。安装会联网下载约 2GB 并持久写入本机，必须先按当前环境的确认规则取得批准，再运行 `--accept-download`。详见 [runtime.md](references/runtime.md)。

3. 转换一个本地文件。成品默认存入 `$EBOOK_TO_KINDLE_OUTPUT_DIR`（未设置时为 `~/Documents/KindleBooks`）；会反复迭代的书应显式给一个不与输入、输出、runtime 或 Skill 重叠的私有工作区：

   ```bash
   # 默认输出到 $EBOOK_TO_KINDLE_OUTPUT_DIR 或 ~/Documents/KindleBooks
   python3 scripts/ebook_to_kindle.py convert INPUT

   # 显式指定输出目录
   python3 scripts/ebook_to_kindle.py convert INPUT --output-dir OUTPUT_DIR

   python3 scripts/ebook_to_kindle.py convert INPUT \
     --output-dir OUTPUT_DIR \
     --workspace PRIVATE_BOOK_WORKSPACE \
     --reuse auto

   # 需要准确的 Kindle 书库语言、书名或作者时显式指定；元数据会进入缓存键。
   python3 scripts/ebook_to_kindle.py convert INPUT \
     --title "BOOK TITLE" --authors "AUTHOR" --language zh \
     --output-dir OUTPUT_DIR
   ```

   同时输出 PW2 的两种格式及主 EPUB：

   ```bash
   python3 scripts/ebook_to_kindle.py convert INPUT \
     --formats azw3,mobi6,epub \
     --output-dir OUTPUT_DIR
   ```

4. 只在自动 PDF 判型错误时使用 `--pdf-mode digital` 或 `--pdf-mode scan`。不要把扫描件静默降级为 Calibre 直转。

5. `--reuse auto` 依次尝试 exact final、master EPUB、PDF semantic package，未命中才 cold rebuild；`--reuse refresh` 显式忽略旧节点并重建。命中、各阶段耗时和跳过的工具写入报告，私密原文与绝对路径不进入报告。详见 [workspace.md](references/workspace.md)。

6. 仅在用户明确同意替换时使用 `--overwrite`。成功报告仍标记 `paperwhite2_manual_check_required: true`；真机抽查目录、翻页、字体、图片和脚注后才能发布。只有 exact 命中且成品 SHA/bytes 与已真机批准版本相同，才能继承既有 `PW2_PASS`。

7. 成功转换后读取报告的 `performance_review`，指出主瓶颈；需要优化时再做安全实验。旧报告可用 `python3 scripts/ebook_to_kindle.py review REPORT` 复盘。只有总耗时降低至少 10% 或主阶段减少至少 15 秒，且全部质量门无回归，才保留 Skill 优化；否则回滚实验。

8. 注释翻译是独立的显式流程，不属于普通 `convert`。这一步是可选功能，需要本机已装 OpenAI `codex` CLI 且有对应模型权限，默认模型是 `gpt-5.3-codex-spark`（可用 `--model` 换）；真实书只有在用户同意把注释文本发给云模型后才可运行 `--allow-cloud-translation`。提取、模型复核、用户批准、按 ID 写回和再转换命令见 [note-localization.md](references/note-localization.md)。普通转换仍受系统网络沙箱约束并完全离线。

9. 无线推送到 Kindle (Send to Kindle 双模支持)：
   转换/精排完成后的电子书（EPUB、PDF、DOCX 等），支持两种免插线无线推送通道：

   * **模式 A（本地浏览器直推 - Browser Mode，适合 Mac 本机）**：
     基于 Ego 浏览器的 `ego-browser` 命令行复用当前亚马逊网页登录态（没装 Ego 时自动改走模式 B），通过 CDP 协议自动化完成文件上传与「管理我的内容和设备」即时投递指令：
     ```bash
     python3 scripts/send_to_kindle.py INPUT_FILE [--domain amazon.com] [--device 设备名] [--title 书库里显示的书名]
     ```
     - Send to Kindle 不收 AZW3/MOBI：传 AZW3 时自动改传同目录同名 EPUB，没有就报 `UNSUPPORTED_FORMAT`（转换时加 `--formats azw3,epub`）。
     - 上传成功即进入 Kindle 书库，设备下次同步会自动收到。
     - 之后只对**刚上传的那一本**发起「传送至设备」：上传前先记下书库（按时间倒序）已有的文档和最新那一本，连续两次读到的列表完全一致才算数；上传后只在排在原最新那本**之上**的新文档里按书名找，且必须唯一，否则跳过。书库读不到或为空时不推送，只上传。已知局限：脚本拿不到绑定本次上传的唯一编号，靠书库前后变化加书名匹配（全等或包含）识别；同一账号另一笔相同或相近书名的上传若在这段时间出现在书库里，可能被认错，上传成功也可能误判。别在其他入口同时传相近书名，需要确认时去「管理内容和设备」人工核对。
     - 设备：账号只有一台时直接推；多台时须用 `--device`，先按完整设备名匹配，再按名字片段匹配，片段命中多台时不推并列出候选。确认前会取消其他设备的勾选，只留目标这一台。
     - 结果里 `deliveryStatus` 为 `CLOUD_UPLOADED`（只上传）、`DELIVERY_REQUESTED`（已向亚马逊发出传送请求，设备联网后收到）或 `DELIVERY_UNCONFIRMED`（传送请求可能已提交但没能确认，请去「管理内容和设备」看一眼再重试）。上传之后的步骤出错时仍报 `SUCCESS` 并保留以上状态；`UPLOAD_UNCONFIRMED`（点了发送但没确认上传）和 `BROWSER_PUSH_TIMEOUT` 表示结果未知，**先查书库再重试，别直接重发**。
     - `--no-deliver` 只上传不推。`--domain` 按你的亚马逊站点填（amazon.com / amazon.co.jp / amazon.co.uk 等），默认取环境变量 `KINDLE_AMAZON_DOMAIN`，未设置时为 `amazon.co.jp`。

   * **模式 B（官方邮箱推送 - Email Mode，完美适配云端 Agent / Linux VPS / 无头环境）**：
     通过 Python 标准库 SMTP 协议，直接将电子书作为附件发送至您的专属 Kindle 邮箱（`YOUR_KINDLE_EMAIL@kindle.com`），亚马逊云端接收后自动排版并下发给您的 Kindle 设备：
     ```bash
     # 专用脚本直推
     python3 scripts/send_to_kindle_email.py INPUT_FILE

     # 或通过主入口自动感知环境（在无 ego-browser 环境下自动切换为邮件模式）
     python3 scripts/send_to_kindle.py INPUT_FILE --method auto
     ```
     - 配置文件路径：`~/.config/kindle/email_config.json`（初次使用可运行 `--init-config` 生成模版）
     - 默认接收邮箱：配置在 `email_config.json` 中的 `kindle_email`
     - 授权发件白名单：配置在 `email_config.json` 中的 `sender_email`（必须已在亚马逊后台添加到已认可的发件人列表）
     - 支持通过配置文件（600 权限）或环境变量 `KINDLE_SMTP_PASS` 传入发件邮箱密码/授权码，不支持命令行明文传密码。
     - 亚马逊邮件通道不收 AZW3/MOBI：传 AZW3 时若同目录有同名 EPUB 会自动改发 EPUB，否则提示先转出 EPUB（`--formats azw3,epub`）。邮件模式发到的是该邮箱对应的那台设备，`--device` 不起作用。

转换不是“能打开就算成功”：所有路线都会把作者边距归零，过滤源书的固定字体、字号与行高，再用 PW2 可调的相对字号重建正文和标题层级。主 EPUB 还会合并有明确证据的中文纸页硬换行，去掉中文字符间的版面空格；目录、landmark、脚注、表格、诗歌/预格式文本、带锚点块和带目标链接的列表项不参与块合并。同时生成可点击的两级目录并在 Kindle 输出回读中验证 NCX。已有 EPUB/DOCX/HTML 注释由 Calibre 保留并接受双向链接门；数字 PDF 的正文方括号引用按编号与源 PDF 逐项对账，连续尾注补上点击与返回链接。Docling 漏掉但 PDF 文本可恢复的定义使用原文恢复；源 PDF 本身缺少定义时只生成明确的缺失说明，不伪造注释内容。

## 路由

- DOCX / HTML / TXT / Markdown：提取两级标题与已有注释预期后，由 Calibre 生成语义主 EPUB；无标题时至少保留一个正文导航入口。
- EPUB：先读取原 nav/NCX、注释和资源合同，再经 Calibre 归一化样式；已有目录优先保留，缺失目录才从标题重建。
- 文字 PDF：Docling 关闭 OCR，恢复阅读顺序；迁移原 PDF outline，删除被误识别为表格的印刷目录，重建连续尾注，并将源 PDF 正文编号与 Kindle noteref 按编号和次数精确对账。
- 扫描 PDF / 图片扫描书：Docling + macOS Vision OcrMac 全页中英 OCR。文字净化、印刷目录对齐与错题复盘详见 [scan-book-purification-lessons.md](references/scan-book-purification-lessons.md)。
- 最终 AZW3：`kindle_pw` + KF8/MOBI v8。
- 最终 MOBI：`kindle_pw` + `--mobi-file-type old`，必须为 MOBI v6。
- 工作区 exact hit：只重验输入/对象/证据哈希和容器，随后走原子发布；不启动 Docling、Calibre、Java 或 Spark。
- 工作区 master hit：复用已验 master EPUB、源参考文本和 baseline；重跑 EPUB/Kindle 实物门，不启动 Docling/OCR。
- 工作区 semantic hit：复用完整校验过的 Docling 导出包，重跑正规化、master 和最终门，不启动 Docling/OCR。

## 必须遵守

- 转换核心过程严格只收本地路径并离线运行；无线推送仅在用户明确要求时通过 `scripts/send_to_kindle.py` 调用本地 `ego-browser` 交互亚马逊官方 Send to Kindle 接口，不向任何第三方泄露凭证或书籍内容。
- 不修改用户真实 HOME 内的数据；转换子进程的 `HOME`、`CFFIXED_USER_HOME`、Calibre/Docling/HF/Torch 与 XDG 状态全部重定向到随机 0700 任务目录，安装子进程则重定向运行时内的可丢弃缓存，并在结束时严格清理。
- 输出目录默认为 `$EBOOK_TO_KINDLE_OUTPUT_DIR` 或 `~/Documents/KindleBooks`；输出目录、成品和报告不得位于私有 runtime 树中；发布全程锦定同一目录 inode，拒绝符号链接、硬链接或中途换目录。
- 同一私有 runtime 一次只运行一项转换、核验、安装或卸载；内核文件锁被占用时以 `RUNTIME_BUSY` 停止，发现残留任务目录时以 `ISOLATION_FAILED` 停止。
- 任一道内容、目录、资源、EPUBCheck、OCR 或 Kindle 回读门失败，都返回结构化错误并不发布成品。详见 [quality-gates.md](references/quality-gates.md)。
- 目录条目必须逐个解析到正文锚点；AZW3/MOBI 回读必须保留不少于主 EPUB 的有效 NCX 条目。注释引用、注释目标和返回链接三者计数必须一致；数字 PDF 还必须通过逐编号源引用对账，不能只比较总数。
- 主 EPUB 中中文字符间空格、可识别的句内块断裂、正文 `<br>` 硬换行、固定/异常字号、源字体族、行内字号和异常行高必须全部为 0。Kindle 回读 EPUB 只作文本流和结构证据；Calibre 反向转换会合成自己的 CSS，不得把它当成最终字号真值。
- 不把临时绝对路径、用户名、凭证或原文写入最终 JSON 报告。
- 单书工作区根目录为 0700、文件为 0600、上限 2 GiB；对象每次命中都重算 SHA-256。发现损坏、未知条目、硬链接、符号链接或边界重叠时以 `WORKSPACE_INVALID` 停止，不把损坏静默当成 miss。
- 已批准的注释译文按 ID 冻结；重复英文原文不得覆盖上下文不同的已批准译文。只有全组都处于未批准状态时才允许先翻译唯一原文再 fan-out。
- 第三方版本、许可证和排除项见 [third-party.md](references/third-party.md)。

## 验证与样式

确定性单测和触发边界用例位于 `evals/`；PW2 重排样式来自 `assets/pw2.css`。修改任一路由后至少重跑相关单测、一个真实冷转换和对应命中层；高耦合提速还必须用下方合成验收集完整重建，并在真机上抽查一本。

关键 DOCX、文字 PDF、扫描 PDF 均有无隐私合成源和固定 SHA-256。完整重建验收集：

```bash
python3 evals/run_acceptance.py --output-dir /private/tmp/ebook-to-kindle-acceptance
```

若复用非空验收目录，必须像正式转换一样显式追加 `--overwrite`。
