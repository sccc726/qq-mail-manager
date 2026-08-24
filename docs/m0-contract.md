# M0 CLI、JSON 与操作契约

本文是 `qq-mail-manager` 的兼容性契约。它定义 M0 建立的目标边界；M1 和 M2 的实现必须遵守它，不能通过为单个脚本另造字段来绕过它。M2/U7 已原子切换为 UID 语义，`mail_id` 不再表示 sequence number。

## 邮件引用

邮件的稳定引用是 `MailRef(folder, uidvalidity, uid)`：

```json
{"folder":"INBOX","uidvalidity":"12345","mail_id":"42"}
```

- `folder` 是服务器返回、可再次选择的文件夹名称。
- `uidvalidity` 是所选文件夹的十进制 UIDVALIDITY 字符串。
- `mail_id` 保留这个公开名称以兼容调用方；M2/U7 后它只能是十进制 UID 字符串，且等于 `uid`。
- 每个返回的邮件摘要、详情、附件结果、预览和批量项都必须包含这三个字段。任何定位邮件的命令都必须同时接收它们；UIDVALIDITY 不匹配时必须在 FETCH、STORE、COPY 或 MOVE 前停止。

所有生产 CLI 均采用本节的 MailRef 接口，防止重新混入两种 ID 语义。

## 通用命令和输出

- 入口保持为 `python scripts/{list_folders,search_emails,get_email,download_attachment,mark_email,move_email,send_email}.py`；现有参数名在 U7 前不移除。
- 所有正常、预览和业务错误路径的 stdout 必须以 UTF-8 编码恰好输出一个 JSON 文档；诊断只能写 stderr。七个真实 CLI 入口会在进程边界统一配置 UTF-8，以避免 Windows 重定向流继承非 UTF-8 代码页。
- 顶层 `status` 只能是 `success`、`preview`、`partial` 或 `error`。`success`/`preview` 退出码为 0；`partial`、`error` 和参数错误为非零。
- 返回的错误必须是结构化 JSON，至少包含 `status: "error"` 和非秘密的 `message`。不得把授权码、完整 MIME 或附件内容放入诊断。
- 读取凭据仅使用 `QQ_EMAIL` 和 `QQ_EMAIL_AUTH_CODE`；缺失时在任何网络连接前返回该结构化错误。

## 搜索与批量结果

搜索结果包含 `emails`、`total_matched`、`total_displayable`、`total` 和 `has_more`；有下一页时还包含 `next_offset` 和可选的人类提示 `tip`。`total_matched` 是 UID SEARCH 的命中数，`total_displayable` 是成功取得 metadata、通过精确 recent 过滤并进入可分页候选集的数量；为避免全量邮件头 FETCH，这个候选集在页内 header/preview FETCH 前确定。`total` 始终等于实际返回的 `emails` 数量；页内 FETCH 失败保留在 `failed` 中，并使有成功项的结果为 `partial`、整页无成功项的结果为 `error`，不会伪装为空成功。`limit` 是本次展示范围，`offset` 是零基页偏移；单页最多 15 项。按字段或关键词的过滤条件会原样回显在顶层。

批量读取、下载、标记和移动的返回包含目标集合、成功集合/条目和失败集合/条目。只要一个目标失败且另一个成功，状态为 `partial`；全部失败为 `error`。失败不得伪装为空结果或成功。

## 预览与确认

- 移动/删除默认返回 `preview`，不改变邮箱状态。预览清单至少绑定操作、源/目标文件夹和每个 MailRef；确认请求必须携带由该清单得到的 `confirmation` 摘要。
- 发送默认返回 `preview`，包括规范化的 To/CC/BCC、主题、正文摘要、附件清单和 `confirmation` 摘要。只有同时给出 `--confirm --confirmation <摘要>`，且重建后的清单仍匹配时，才可建立一个 SMTP 连接并调用一次 `sendmail`；这不是跨进程 exactly-once 承诺。
- 发送清单绑定账号、规范化邮件头/信封收件人、主题、正文内容、HTML、附件的顺序/规范路径/类型/大小/mtime/内容哈希、主题和正文文件元数据，以及回复 MailRef、原邮件内容和线程头。任一预览字段或文件内容变化都会使旧摘要失效。
- Bcc 只存在于预览清单和 SMTP 信封，绝不写入 MIME 头。SMTP 拒收映射为 `success`、`partial` 或 `error`；传输在 `sendmail` 后异常时会标记 `delivery_indeterminate`，不会声称邮件未发送。
- `send_email.py --test` 只连接、执行 TLS 和认证后关闭；它返回 `sent:false`，不构建 MIME、不读取附件/正文文件、不读取 IMAP，且不能和任何发送、回复或确认参数组合。

- `get_email.py` 的 `body` 保留原字段名但有 64KiB 安全上限；同时返回 `body_truncated` 和 `body_bytes_fetched`，不得把截断正文表述为完整正文。

## M4 本机目录与资源策略

本仓库的默认使用者是当前电脑上的单一交互式所有者。因此发送附件路径和下载目录可以自由选择；实现会规范化绝对路径，但不设可信根，也不默认拒绝链接。这一选择依赖发送文件哈希和预览确认、下载不覆盖提交、Windows 安全单组件文件名以及单附件 50 MiB/单次 100 MiB 配额。支持硬链接时附件发布是原子的；FAT/SMB 等不支持硬链接时会以 `O_EXCL` 独占预留目标再复制，复制期间目标可见且非原子，但仍不会覆盖既有路径。

严格目录沙箱不是当前默认值。只要运行模式变为多用户、服务/API、远程入口、不可信路径输入、共享目录或高权限账户，就必须配置发送只读根、下载写根并拒绝符号链接、junction 与 reparse point，同时采用更严格的资源配额。静态无效下载路径在读取凭据或建立 IMAP/SMTP 连接前返回单个 JSON 错误。
- 发送地址使用 ASCII dot-atom 和合法域标签；Unicode 仅可用于显示名（尚未协商 SMTPUTF8）。去重保留 local-part 大小写，仅将域名按大小写无关比较。

移动确认摘要与 UID 实现已在 C4/C5/U6 完成；M3 已将未确认发送的原预期失败回归转为正常通过测试。

## M5 实现归属与最终审计

七个 `scripts/*.py` 入口只负责兼容导出和启动公共 CLI；它们不直接创建 IMAP/SMTP 连接、读取环境、处理 MIME、下载附件或执行 UID 操作。`connections.py` 是唯一连接所有者，`config.py` 是唯一环境读取点，`mime.py` 是 Header 解码与 BODYSTRUCTURE 解析的权威层；详情、搜索、附件、UID 变更和发送分别位于对应的 `qqmail_core` 模块。

移动预览只请求上限为 16 KiB 的 Subject/From/Date Header，不拉取完整邮件。`BODY.PEEK[]` 仅保留在 `sending.py` 的回复源读取路径：它需要对用于确认绑定的完整原始源计算摘要；列表和移动预览均不得使用它。
