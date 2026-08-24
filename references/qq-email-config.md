# QQ邮箱配置指南

## 目录
- [开启IMAP/SMTP服务](#开启imapsmtp服务)
- [获取授权码](#获取授权码)
- [本机 Codex 凭据储存](#本机-codex-凭据储存)
- [服务器配置信息](#服务器配置信息)
- [常见问题](#常见问题)
- [离线开发安全](#离线开发安全)

## 开启IMAP/SMTP服务

### 步骤1:登录QQ邮箱
访问 [https://mail.qq.com](https://mail.qq.com)，使用QQ账号登录邮箱。

### 步骤2:进入设置
1. 点击右上角「设置」
2. 选择「账户」选项卡
3. 向下滚动找到「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务」

### 步骤3:开启服务
根据需要开启以下服务：
- **IMAP/SMTP服务**:用于收取和管理邮件（必开）
- **SMTP服务**:用于发送邮件（必开）

点击对应的「开启」按钮，按照提示验证手机号码即可。

## 获取授权码

### 为什么需要授权码
QQ邮箱不支持直接使用QQ密码登录第三方邮件客户端，需要使用专门的「授权码」进行身份验证。

### 获取方法
1. 在「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务」页面
2. 点击「生成授权码」按钮
3. 通过手机短信验证
4. 系统会生成一个16位的授权码，格式类似：`abcdabcdabcdabcd`
5. 按[本机 Codex 凭据储存](#本机-codex-凭据储存)一节保存授权码；不要把真实值写入仓库、脚本参数或普通配置文件

### 注意事项
- 授权码可以随时在设置中重新生成
- 每个邮箱账户可以生成多个授权码
- 建议定期更换授权码以保护账户安全

## 本机 Codex 凭据储存

### 当前推荐

在本项目当前的使用边界——仅邮箱所有者、仅本机 Windows、仅通过 Codex 交互使用——推荐把以下两项持久保存为 **Windows 当前用户（`User`）范围的环境变量**：

- `QQ_EMAIL`：完整邮箱地址
- `QQ_EMAIL_AUTH_CODE`：QQ 邮箱 IMAP/SMTP 授权码，不是 QQ 登录密码

项目的 `scripts/qqmail_core/config.py` 只从当前进程环境读取这两个变量，不会自行把凭据写入文件、数据库或日志。[Codex 官方文档](https://learn.chatgpt.com/docs/config-file/environment-variables)也将环境变量用于 shell 范围覆盖和自动化凭据。选择当前用户范围可以让 Codex 重启后继续使用，同时避免把授权码写入项目仓库或 `~/.codex/config.toml`。

不要把授权码配置在 `config.toml` 的 `shell_environment_policy.set` 中；该字段适合显式注入普通环境值，但会让授权码以明文出现在常规配置文件中。不要在项目中创建 `.env` 保存真实凭据：当前代码不会加载它，而且它仍是明文副本。也不要使用 Windows `Machine` 范围，因为这会扩大到本机其他用户和服务。

> **安全边界：** Windows 通常把 `User` 环境变量持久保存在当前用户注册表 `HKEY_CURRENT_USER\Environment`；它不是加密凭据库。同一 Windows 用户身份下运行的其他进程可以读取这些值，管理员也可能访问。该方案只适用于本项目当前的单用户、本机边界。若改为多用户、共享主机、远程服务、高权限进程或高敏感邮箱，应改用操作系统或托管凭据库，并在启动邮箱脚本时按进程注入；当前项目尚未实现这种凭据提供器。

### Windows 配置

在 PowerShell 7 中执行。授权码使用遮罩输入，避免直接出现在命令历史中：

```powershell
[Environment]::SetEnvironmentVariable(
    "QQ_EMAIL",
    "your-address@qq.com",
    [System.EnvironmentVariableTarget]::User
)

$qqAuthCode = Read-Host "请输入 QQ 邮箱授权码" -MaskInput
try {
    [Environment]::SetEnvironmentVariable(
        "QQ_EMAIL_AUTH_CODE",
        $qqAuthCode,
        [System.EnvironmentVariableTarget]::User
    )
} finally {
    Remove-Variable qqAuthCode -ErrorAction SilentlyContinue
}
```

设置后完全退出并重新启动 Codex，使新进程继承变量。不要把真实邮箱或授权码替换进文档、脚本、测试、CI、截图或提交记录。

### 无泄露验证

以下命令只显示邮箱地址及授权码是否存在，不输出授权码本身：

```powershell
[pscustomobject]@{
    QQ_EMAIL = [Environment]::GetEnvironmentVariable(
        "QQ_EMAIL", [System.EnvironmentVariableTarget]::User
    )
    QQ_EMAIL_AUTH_CODE_CONFIGURED = -not [string]::IsNullOrWhiteSpace(
        [Environment]::GetEnvironmentVariable(
            "QQ_EMAIL_AUTH_CODE", [System.EnvironmentVariableTarget]::User
        )
    )
}
```

### 轮换与删除

轮换时先在 QQ 邮箱生成新授权码，用上面的遮罩输入命令覆盖 `QQ_EMAIL_AUTH_CODE`，重启 Codex 并验证认证成功后，再在 QQ 邮箱设置中撤销旧授权码。

不再使用技能时，删除当前用户范围的两项配置并重启 Codex：

```powershell
[Environment]::SetEnvironmentVariable(
    "QQ_EMAIL", $null, [System.EnvironmentVariableTarget]::User
)
[Environment]::SetEnvironmentVariable(
    "QQ_EMAIL_AUTH_CODE", $null, [System.EnvironmentVariableTarget]::User
)
```

## 服务器配置信息

### IMAP服务器（收邮件）
```
服务器地址: imap.qq.com
端口: 993 (SSL)
用户名: 你的完整QQ邮箱地址 (如 123456@qq.com)
密码/授权码: QQ邮箱授权码
```

### SMTP服务器（发邮件）
```
服务器地址: smtp.qq.com
端口: 587 (STARTTLS)
用户名: 你的完整QQ邮箱地址
密码/授权码: QQ邮箱授权码
```

## 常见问题

### Q: 授权码和登录密码有什么区别？
A: 授权码是专门用于第三方客户端的访问凭证，即使他人获取了授权码也无法登录你的QQ账号，比直接使用QQ密码更安全。

### Q: 收件箱显示正常，但发送邮件失败？
A: 请确认：
1. 已开启SMTP服务
2. 使用的是授权码而非QQ密码
3. 网络连接正常

### Q: 显示"Authentication failed"错误？
A: 常见原因：
1. 授权码输入错误
2. 授权码已过期或被重置
3. 邮箱地址格式不正确（需要完整的@qq.com地址）

### Q: 如何查看邮件夹名称？
A: QQ邮箱常见的邮件夹名称：
- `INBOX` - 收件箱
- `INBOX.Sent` - 已发送
- `INBOX.Drafts` - 草稿箱
- `INBOX.Trash` - 垃圾箱
- `INBOX.Star` - 星级邮件

### Q: 授权码忘记了怎么办？
A: 登录QQ邮箱网页版 → 设置 → 账户 → POP3/IMAP/SMTP服务 → 点击「更改授权码」→ 重新获取新的授权码。

## 离线开发安全

自动测试、CI、示例和代码审计均不得设置真实邮箱或授权码，也不得连接 QQ 邮箱。请使用仓库的 `FakeIMAP`、`FakeSMTP` 和网络阻断测试；开发命令见 [docs/development.md](../docs/development.md)。
