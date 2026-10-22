# 部署与运行手册

本版本可以运行本地 Python/CLI 研究，也提供**单团队、共享权限的私有研究 API**。它没有实盘交易功能、用户权限分级或资金托管；不适合作为匿名公网多租户 SaaS。实际服务器、域名、证书和券商终端未在本次工作中部署或联调。

v0.3 按当前需求保留未来接入能力，暂不执行真实部署。管理员数据源的 `data` 路径现在也可指向完整 DatasetBundle 快照，ST、规则和日历会一起加载；配置示例及离线验收见 [供应商接口](provider_contract.md)。

## 启动

```powershell
python -m pip install -e '.[service,test]'
# 生成随机令牌，不把它写进项目或发到日志：
$env:PKUQUANT_API_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(48))"
python run.py serve --service-config examples/service.json
```

默认只监听 127.0.0.1:8000；配置文件内路径相对配置文件。管理员登记可访问的数据路径；HTTP 用户只能用 `original` 等 source ID，不能指定服务器路径、上传文件或提交 Python 回调。示例指向原始 feathers.zip，仍适用其研究数据限制。

令牌需至少 32 字符；**所有持有令牌的人共享任务和结果权限**。实际部署将固定密钥放在服务管理器/Secret 中，重启时重新注入，不使用示例随机生成方式使每次重启改变密钥。轮换密钥需重启。API 不在 URL 或返回体中传播密钥；不要在代理日志记录 Authorization。

另一 PowerShell 窗口需要通过安全方式取得同一个令牌：

```powershell
$headers = @{ Authorization = "Bearer $env:PKUQUANT_API_TOKEN"; 'Idempotency-Key' = 'lowvol-dev-001' }
$body = @{
  source = 'original'
  alpha = '-rank(ts_std(returns, 60))'
  settings = @{ start = '2023-01-03'; end = '2023-12-29' }
} | ConvertTo-Json -Depth 4
$job = Invoke-RestMethod http://127.0.0.1:8000/jobs -Method Post -Headers $headers -ContentType application/json -Body $body
Invoke-RestMethod "http://127.0.0.1:8000/jobs/$($job.id)" -Headers $headers
# status 为 succeeded 后下载（API 返回附件）：
Invoke-WebRequest "http://127.0.0.1:8000/jobs/$($job.id)/artifacts/report.html" -Headers $headers -OutFile report.html
```

| 路径 | 方法 | 用途 |
|---|---|---|
| /health | GET | 最小健康状态；检查 worker 与 SQLite；免鉴权 |
| /sources | GET | 管理员登记的数据源 ID |
| /jobs | POST | 提交表达式任务，202 返回任务 ID |
| /jobs | GET | 最近 100 个任务 |
| /jobs/{id} | GET | 状态与安全错误摘要 |
| /jobs/{id}/cancel | POST | 取消排队或运行中的任务 |
| /jobs/{id}/artifacts/{name} | GET | 下载成功任务的白名单报告文件 |

所有业务接口支持 Bearer Token，也支持 v0.4 网页会话 Cookie；网页启动与会话安全约定见 [网页工作台](web_ui.md)。根路径 `/` 提供 UI，`/ui/catalog` 提供公开算子目录，`/ui/config` 提供鉴权后的配置。`POST /session` 用 Bearer 换取会话，`DELETE /session` 注销；`/jobs/{id}/strategy` 与 `/jobs/{id}/result` 返回鉴权后的策略与结果视图。

无公网 API 文档页，健康接口不暴露路径或凭据。提交上限 16 KiB；表达式长度/节点/数值及设置有上限。违法表达式会在执行白名单校验时失败。相同 Idempotency-Key 与相同请求返回同一任务，内容改变返回 422。积压/总任务数超过配额返回 429。取消是终态操作，不自动重跑；重试应使用新的 key。

## 进程、任务与数据

SQLite WAL 存储任务；一个工作线程串行领取，每项研究在独立 Python 子进程中执行。默认最长 1800 秒，超时终止；本地异常详细信息位于 service-data/{id}/worker.log，API 仅返回摘要。默认最多 8 个排队/运行任务和 1000 个累计任务。硬崩溃后遗留 running 任务标记 interrupted，queued 任务继续；不隐式重复执行已开始任务。优雅关闭会终止当前子进程并标记 cancelled。

**只运行一个 ASGI worker/一个副本**；目录级进程锁拒绝第二个实例。SQLite 放本地磁盘，不放共享网络文件系统。扩容需要独立队列、共享对象存储和权限体系，不要简单把 `workers` 改大。

源快照需只读、版本化。源在提交到执行之间若被管理员替换，worker 会读取执行时版本；manifest 记录执行时 SHA-256。要求固定版本时，登记不可变快照，不能登记持续改写的行情目录。原始供应商授权范围也需由部署方落实。

报告先写隐藏 staging 目录，再原子改名为目标目录；已有结果不覆盖。`checksums.json` 校验各输出文件，`source_code.zip` 保存引擎快照。保存失败会留下 `.pending-*` 供管理员排查；成功状态只在完整输出出现后写入。未成功任务的部分文件不通过 API 提供。源文件和历史状态文件需独立备份，哈希不能替代数据本身。

## 私有服务器验收

1. 在干净虚拟环境安装依赖并运行全部测试；保存该环境锁定的依赖版本。当前工作区验证于 Python 3.13；不同平台和版本须重新验证。
2. 先运行 `python run.py doctor --data <快照> --states <规则> --security-history <状态>`。退出码 2 表示严格数据检查未通过，不代表基础研究接口不可运行。原始 ZIP 按预期会返回 2。
3. 由反向代理提供 HTTPS、访问范围限制、请求速率/连接数/超时限制；不将裸 Uvicorn 暴露公网。服务账号仅能读取登记的数据、写入任务目录。生产进程不应持有实盘下单凭据。
4. 用容器/系统服务设置内存、CPU、磁盘配额及进程树清理。表达式白名单不是操作系统资源沙箱；大矩阵复杂表达式仍可耗尽内存。Windows 硬杀父进程可能遗留子进程，服务管理器应清理整个进程树；容器应一起终止所有进程。
5. 监控 /health、失败/超时任务比例、磁盘余量、任务积压和源数据更新时间。配置服务启动/崩溃重启；只自动重启服务，不盲目重跑中断任务。
6. 停服后备份整个任务目录（含数据库、WAL/SHM 如存在）和源快照，执行恢复演练。累计任务达到配额时先归档并创建新的任务目录；不要自动删除研究记录。当前没有在线删除/自动保留策略。
7. 使用自己的完整历史数据做小样本人工逐笔核对、跨系统对账、容量/滑点/费用压力测试，再开放给团队。检验未来数据隔离，并冻结未参与开发的样本外区间。

部署设计参考 [FastAPI 官方部署概念](https://fastapi.tiangolo.com/deployment/concepts/) 的 HTTPS、启动、恢复、进程和内存要求。这里的单进程队列是本项目针对研究任务的实现选择，不是 FastAPI 自带的持久化任务队列。

## 尚未完成的外部验收

没有实际服务器上的 TLS、负载、灾难恢复演练；没有真实 QMT/券商登录的端到端验证；没有交易所全量状态/官方规则/完整退市样本的校验。这些不能由合成测试替代。实盘还需要真实公司行动账本、交易网关、风控、订单回报与对账等独立建设；当前经济单位账户不会被描述成实盘交割账本。
