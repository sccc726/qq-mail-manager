# 离线开发与测试

本仓库的测试和 CI 不使用真实邮箱、授权码或网络。`tests/support.py` 的 `BlockNetwork` 会阻断未替换的 socket、DNS、IMAP 和 SMTP 调用；业务测试只使用 `FakeIMAP` 与 `FakeSMTP`。

在具备 Python 3.11、3.12 或 3.13 的环境中运行：

```bash
python -m compileall -q scripts tests
python scripts/list_folders.py --help
python scripts/search_emails.py --help
python scripts/get_email.py --help
python scripts/download_attachment.py --help
python scripts/mark_email.py --help
python scripts/move_email.py --help
python scripts/send_email.py --help
python -m unittest discover -s tests -v
```

入口脚本只保留兼容导出和 CLI 启动；可测试业务逻辑集中在 `scripts/qqmail_core/`：读取在 `readers.py`/`details.py`，附件在 `attachments.py`，UID 写操作在 `marking.py`/`mutations.py`，发送在 `sending.py`。连接只由 `connections.py` 创建，环境凭据只由 `config.py` 读取。

任何手动运行若缺少 `QQ_EMAIL` 或 `QQ_EMAIL_AUTH_CODE`，都会在连接前以单个 JSON 错误退出。不要把真实凭据写进测试、CI、日志或提交。
