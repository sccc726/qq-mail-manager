# M0 CLI、JSON 与操作契约

本文是 `qq-mail-manager` 的兼容性契约。它定义 M0 建立的目标边界；M1 和 M2 的实现必须遵守它，不能通过为单个脚本另造字段来绕过它。当前 `main` 的 sequence-number 实现是迁移基线，**不是** UID 契约的例外：在 U7 原子切换前，不得将中间 ID 语义用于真实写操作。

## 邮件引用

邮件的稳定引用是 `MailRef(folder, uidvalidity, uid)`：

```json
{"folder":"INBOX","uidvalidity":"12345","mail_id":"42"}
```

- `folder` 是服务器返回、可再次选择的文件夹名称。
- `uidvalidity` 是所选文件夹的十进制 UIDVALIDITY 字符串。
- `mail_id` 保留这个公开名称以兼容调用方；M2/U7 后它只能是十进制 UID 字符串，且等于 `uid`。
- 每个返回的邮件摘要、详情、附件结果、预览和批量项都必须包含这三个字段。任何定位邮件的命令都必须同时接收它们；UIDVALIDITY 不匹配时必须在 FETCH、STORE、COPY 或 MOVE 前停止。

M0 不改变现有脚本的 sequence-number 实现；本节是 U7 前各阶段必须共同采用的目标接口，防止迁移期间混用两种 ID 语义。

## 通用命令和输出

- 入口保持为 `python scripts/{list_folders,search_emails,get_email,download_attachment,mark_email,move_email,send_email}.py`；现有参数名在 U7 前不移除。
- 所有正常、预览和业务错误路径的 stdout 必须恰好输出一个 JSON 文档；诊断只能写 stderr。
- 顶层 `status` 只能是 `success`、`preview`、`partial` 或 `error`。`success`/`preview` 退出码为 0；`partial`、`error` 和参数错误为非零。
- 返回的错误必须是结构化 JSON，至少包含 `status: "error"` 和非秘密的 `message`。不得把授权码、完整 MIME 或附件内容放入诊断。
- 读取凭据仅使用 `QQ_EMAIL` 和 `QQ_EMAIL_AUTH_CODE`；缺失时在任何网络连接前返回该结构化错误。

## 搜索与批量结果

搜索结果包含 `emails`、`total_matched`、`total` 和 `has_more`；有下一页时还包含 `next_offset` 和可选的人类提示 `tip`。`limit` 是本次搜索的最大范围，`offset` 是零基页偏移；单页最多 15 项。按字段或关键词的过滤条件会原样回显在顶层。

批量读取、下载、标记和移动的返回包含目标集合、成功集合/条目和失败集合/条目。只要一个目标失败且另一个成功，状态为 `partial`；全部失败为 `error`。失败不得伪装为空结果或成功。

## 预览与确认

- 移动/删除默认返回 `preview`，不改变邮箱状态。预览清单至少绑定操作、源/目标文件夹和每个 MailRef；确认请求必须携带由该清单得到的 `confirmation` 摘要。
- 发送默认返回 `preview`，包括规范化的 To/CC/BCC、主题、正文摘要、附件清单和 `confirmation` 摘要。只有显式确认且摘要仍匹配时才可建立 SMTP 连接并发送一次。
- 发送和移动的预览字段或文件内容任一变化，都必须使旧确认摘要失效。

这些确认摘要和 UID 实现属于后续 C4/C5/U6/S2 的工作；M0 测试将未确认写操作作为预期失败的回归目标，而不把当前缺陷固定为正确行为。
