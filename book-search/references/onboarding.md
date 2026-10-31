# 首次使用：先问清用途，再拿到第一本书

这份流程供 AI 执行。三个模块各带同一份脚本和说明，可单独安装，共享一份非敏感偏好。

## 1. 每次调用先看设置

先找到**当前 Skill 的实际目录**，在该目录执行（不要假定用户在仓库根目录）：

```bash
python3 scripts/pipeline_setup.py status --json
```

- `has_profile: true`：用一句话提醒「正在用你上次的设置（返回的 path），说『改设置』可以改」，然后正常做事。字段可以缺省；当前任务用得到而缺少的才补问，用户说跳过就用内置默认。
- 没有可用 profile（含损坏）：先走下面的问卷，再做事；用户明确说「跳过」时允许跳过，不偷偷保存代选答案。
- 用户说「改设置」：保留旧答案，只问想改的项，使用 `save --set` 合并；说「体检一下」：运行 `doctor`，未选路线就先问用途。
- 用户说「重新开始」：说明 `reset` 只删偏好、不删账号配置和书；先运行 `reset` 看说明，得到用户对重置的明确同意后再用 `reset --yes`。不用卸载软件或删除凭证来重走问卷。

设置默认在 `~/.config/kindle-book-pipeline/profile.json`；环境变量 `KINDLE_BOOK_PIPELINE_PROFILE` 可改位置。文件只接受下面的非敏感字段，含 `version: 1`；保存自动更新 `updated_at`，权限为 600。不要索要、回显或写入账号、邮箱密码、SMTP 授权码、cookie、token。凭证仍走原有配置文件、环境变量或登录流程。

## 2. 按用途问，别让用户填整张表

优先使用宿主的提问工具（AskUserQuestion / request_user_input / 同类工具），支持多题就合并；不支持多选时允许用户用文字补充。没有工具就用编号文字题。每题都带推荐选项和一句说明，推荐不等于已经得到答案。

**第一题（可多选）**：「你想用它做什么？①找书（推荐先试，免登录拿到一本公版书） ②把手上的书转成 Kindle 格式 ③发到 Kindle ④把外文书翻成中文。这样只准备你需要的东西。」保存为 `goals`，分别对应 `find / convert / send / translate`。

只追问选中的路线；同一个问题只问一次：

| 路线 | 问题、推荐项与为什么问 | 保存字段 |
| :--- | :--- | :--- |
| 找书 | 先说明「古腾堡公版书免登录可直接用；Z-Library 需要你自己的账号，需要它时再引导，不必为了试用先注册」。不用索要账号。 | 无额外必答项 |
| 转书 | 「你的 Kindle 是哪款？Paperwhite 2（这款排版已调过）/其他 Paperwhite/其他 Kindle/没有 Kindle/不清楚。为了如实说明阅读效果。」没有设备、不清楚都可选；其他型号须说明「也能看，但没在该型号上实测」，不新增型号版式。 | `kindle_model`: `paperwhite2 / paperwhite_other / other_kindle / no_kindle / unknown` |
| 转书 | 「成品放哪？推荐 `~/Documents/KindleBooks`，也可以给自己的文件夹。这样下次能找到书。」翻译用户若主动指定目录也可记录，不把未选转书的人拦在目录题上。 | `output_dir` |
| 发送 | 先运行 `doctor --route send --json`。推荐邮件（大多数用户没有 ego-browser）；**只有 `ego_browser_exists: true` 才把浏览器列为选项**，同时保留「暂不发送」。解释「这决定要准备邮箱还是已有的亚马逊网页登录」。 | `send_method`: `email / browser / none` |
| 发送 | 「你平时在哪个亚马逊站点管理 Kindle？推荐选自己的实际站点：美国 amazon.com / 日本 amazon.co.jp / 英国 amazon.co.uk / 其他（填写域名）；不知道就先去自己账号核对，不猜。」暂不发送可跳过。浏览器方式才按需问设备名，可暂不填，不能随便挑一台。 | `amazon_domain`；可选 `kindle_device_name` |
| 翻译 | 「想要纯中文（推荐，适合连贯阅读）还是中英对照（适合对照学习）？」说明目前只支持翻成中文，输入是 EPUB；整书由宿主 AI 翻译，会花模型额度和时间，先说明范围与成本，不承诺几分钟译完。 | `translate_mode`: `zh / bilingual` |

邮件方式要向用户解释三样东西，但**不在聊天中收原值**：

1. Kindle 收件邮箱：在自己的亚马逊「管理内容和设备 → 首选项 → 个人文档设置 / Send to Kindle 邮箱」查看。
2. 已认可发件人：在同一页把用于发书的发件邮箱加到「已认可的个人文档电子邮件列表」；否则亚马逊可能拒收。
3. SMTP 授权码：去发件邮箱服务商的账号安全或客户端/SMTP 设置获取；例如 Gmail 是应用专用密码，QQ/163 是开启 SMTP 后取得的授权码，不是让 AI 代收登录密码。

AI 只帮生成**空模板**：在 ebook-to-kindle 模块执行 `python3 scripts/send_to_kindle_email.py --init-config`，让用户自己在本机编辑 `~/.config/kindle/email_config.json`，或自行设置 `KINDLE_TO_EMAIL / KINDLE_FROM_EMAIL / KINDLE_SMTP_PASS` 等原有变量。不要读取、展示文件内容来帮用户填密码，不把邮箱地址或密码混入 profile。

## 3. 保存后体检：告诉他还差哪一步

把用户确认的答案保存，不把未回答的题填成事实。命令参数作为普通参数传给工具，正确引用用户的目录/设备名，不把回答拼成可执行的 shell 代码。例如在当前模块目录：

```bash
python3 scripts/pipeline_setup.py save --set goals=find,convert kindle_model=paperwhite2 'output_dir=~/Documents/KindleBooks'
python3 scripts/pipeline_setup.py doctor --route find --route convert
```

`--set` 可重复，也可一次放多项；`goals` 用逗号列表（或 JSON 列表），`save` 合并而非整份覆盖。字段全可缺省，`status` 会列出已选路线待补的偏好。若字段非法则不保存；损坏的 profile 会提示中文并按无设置处理，合法答案可重新保存。

体检可重复 `--route`，不写则检查 profile 的 `goals`；`--json` 可放子命令前或后。结果逐项为「可用 / 缺少 / 这条路线不需要」，缺少时附具体下一步；可选的 Z-Library、邮件用户的 ego-browser 缺少不阻塞古腾堡或邮件路线。退出码 3 表示所需本地条件未齐或尚未选择路线，2 表示设置/文件操作错误。

体检只复用内置 provider 的本地就绪检查、普通转书 doctor、Calibre 查找，检查邮件字段和 ego-browser 命令是否存在。不加载第三方 provider，不登录、联网或发邮件；凭证字段齐全也不代表密码正确、发件人已被认可或书已收到。若只装了一个模块，缺少其他模块会如实提示，不假定完整仓库存在。

**装软件、联网下载或持久写入运行环境之前必须先征得用户同意**。首次转书运行环境约 3GB，安装检查要求至少 7GiB 空闲空间；先展示 `bootstrap_runtime.py --component core --print-plan`，获准再运行 `--accept-download`。普通 doctor 只看文件就绪，准备完成后还需原有 `doctor --deep`，不能把普通检查当成一次真实转换。若宿主 AI 自己的沙箱不允许转换脚本再启动 macOS 沙箱（`doctor --deep` 或 convert 报沙箱/锁相关错误），先按宿主规则申请在沙箱外运行这一条命令；宿主不支持时，给用户一条能直接粘进「终端」运行的完整命令，跑完再由 AI 核对产物。转 PDF 另需 uv 和 PDF 组件，不为 EPUB 示例安装整套 OCR。

读取默认值优先级为：**命令行 > 环境变量 > profile > 内置默认**。具体接线只包括：

- convert / pack 的成品目录：`--output-dir` > `EBOOK_TO_KINDLE_OUTPUT_DIR` > `output_dir` > `~/Documents/KindleBooks`。
- send_to_kindle 的站点：`--domain` > `KINDLE_AMAZON_DOMAIN` > `amazon_domain` > `amazon.co.jp`。
- 设备：`--device` > `KINDLE_DEVICE_NAME` > `kindle_device_name` > 不指定设备。

其余偏好由 AI 执行：`send_method=email` 时显式传 `--method email`，`none` 时不发送；`translate_mode=bilingual` 时按原 Skill 写好 `[EN]` 原文与中文段落，pack 显式带 `--bilingual`，纯中文不带。不能只保存了双语偏好就用普通译稿编译。`kindle_model` 仅记录与说明，不偷偷更换排版。找书下载目录仍按原有 `--out-dir / BOOK_SEARCH_DOWNLOAD_DIR`，不受成品目录偏好影响。

## 4. 先跑通一次免登录示例

在正常使用中，准备好 book-search 后，用古腾堡公版英文原书《傲慢与偏见》（`gutenberg:1342`，不是已译好的中文版）做首次示例。先说明需要联网下载；遵循宿主授权规则。用户选择别的书或明确跳过示例时尊重其目标。这个固定 ref 是已指定的示例，不做无依据的“搜索第一项就下载”，也不查 Z-Library 额度。

以下示例**在完整仓库根目录执行**；若模块单独安装，AI 先定位所需模块，改用它的实际路径；不存在就给出安装下一步，不运行错误的相对路径：

```bash
python3 book-search/scripts/book_search.py download gutenberg:1342
# 从 SAVED_PATH 取得实际下载路径，再引用该路径，不猜文件名：
python3 ebook-to-kindle/scripts/ebook_to_kindle.py convert '实际下载路径.epub' --formats azw3,epub
```

- **只选找书**：下载后读取实际文件，核对它是可解开的 EPUB（ZIP 内有正确 mimetype、container.xml 指向的 OPF，书名/作者与示例相符、阅读顺序存在）；报告实际保存路径。只有请求发出或文件非空不算成功。告诉用户可用 Calibre 等 EPUB 阅读器打开；未安装阅读器就说明需要什么，不自动安装。
- **选了转书且运行环境就绪**：在下载验收后继续转换，核对命令退出码、报告的成功状态、质量检查及实际 AZW3/EPUB 产物。告诉用户「AZW3 用 USB 拷到 Kindle 的 documents 文件夹后在书库打开；EPUB 可在电脑阅读器打开或另行发送」。离机检查通过也不宣称真机阅读已通过，保留原有设备检查要求。
- **环境没装或独立模块不齐**：先交代已经拿到的 EPUB 在哪，明确还差哪个组件和下一步，等待安装授权后再继续。不要把未转换的 EPUB 叫作已做好的 AZW3。
- **选翻译**：说明拿到的是英文原书，遵循翻译 Skill 的 EPUB 解包、逐章翻译、校对、编译流程和用户授权范围；不因首次示例就默认花额度翻完整书。部分预览须明确标记，不能报全书已译完。
- **选发送**：按用户的具体发送要求与原 Skill 执行，发送请求、亚马逊受理、设备实际收到分别说明；向导检查通过不等于已经发出，更不等于已经收到。

本轮开发自测有离线限制时，**不实跑上述下载或发送**，交由后续真实验收。

## 5. 收尾让用户能接着用

首次示例结束时的收尾**三样都要有，缺一样就不算完**：

1. 实际成品或已下载文件在哪（给真实路径）。
2. 用什么打开、下一步做什么（如 USB 拷进 Kindle 的 `documents`），以及还有什么没完成。
3. 下次可以直接照说的两三句话：固定包含「改设置」「体检一下」，再加一句贴合用户所选路线的，如「帮我找《XX》转成 Kindle 格式」。

没有产物就直说没有，不拿“设置已保存”充当第一本书成功。
