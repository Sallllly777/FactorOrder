# 上传到 GitHub 的 FactorOrder 仓库

此文件夹是独立仓库。原工作目录中的行情、服务任务、依赖缓存与历史输出没有迁入。`example_factors/` 中是用户要求发布的派生研究报告和账本。

1. 在 GitHub 创建名为 **FactorOrder** 的空仓库；可以先设为 Private。不要在远端先生成 README 或其他文件。
2. 在本文件夹打开终端，检查 `git status` 与 `git log -1 --oneline`。交付时已初始化 `main` 并创建本地提交。
3. 将下面 URL 的 `YOUR_GITHUB_USER` 换成自己的 GitHub 用户名：

```bash
git remote add origin https://github.com/YOUR_GITHUB_USER/FactorOrder.git
git push -u origin main
```

使用 GitHub 的正常登录/凭据管理，不把密码或 Token 写入命令 URL、配置样例或提交。若已有 origin，用 `git remote -v` 确认目标后再设置正确 URL。这里的交付未添加远端、创建 GitHub 仓库或执行推送。

也可以使用 GitHub Desktop 的 Add local repository 选择本目录，再发布到目标账户。旁边的 `FactorOrder-github.zip` 不含 `.git`；用于源码备份或解压上传，保留提交历史请使用本地仓库推送。

`.gitignore` 排除行情 ZIP/Feather/Parquet、`data/`、`outputs/`、`service-data/`、虚拟环境、缓存和凭据。`examples/service.json` 是公开的无密钥配置，应纳入版本管理。不要用 `git add -f` 将原始行情或密钥加入仓库。

GitHub 可直接查看各 Markdown 报告；HTML 需下载后打开，或日后自行配置静态托管。网页工作台还需要 Python 服务和数据，不能仅靠 GitHub Pages 托管运行。尚未选定 LICENSE，仓库的公开/私有设置与代码授权是两个独立事项。
