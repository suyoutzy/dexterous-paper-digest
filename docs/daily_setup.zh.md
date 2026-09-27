# 灵巧手日报：独立仓库部署与维护

**当前已部署并启用每天北京时间 08:00 的定时任务。** [2026-09-27 云端验证](https://github.com/suyoutzy/dexterous-paper-digest/actions/runs/36321628979)完成真实检索、三十篇候选评分、五篇中文总结及验证记录写回；131 项测试通过，DeepSeek 共 7 次成功调用。三个 Secrets 已配置，飞书 Webhook 格式检查通过。本次没有发送飞书消息，飞书实际端点未重复测试；原投递结果与已发送记录已迁入。

旧仓库 `suyoutzy/robotics_paper_daily` 的定时触发已移除，发送入口被阻止，历史资料保留。首次新仓库定时运行计划为 2026-09-28 08:00；GitHub 调度可能延迟。

## 以后更换仓库时的部署设置

以下步骤供后续迁移参考；当前新仓库已经完成设置，无需重复添加密钥。

1. 在自己的 GitHub 账号下创建空的新仓库。建议名称 `dexterous-paper-digest`；公开或私有均可。创建时不初始化 README、`.gitignore` 或 License，工程已经包含这些文件。
2. 将新仓库网址交给维护者上传工程。上传前，维护者必须把 `.github/workflows/dexterous_digest.yml` 中的仓库限制替换成新仓库的真实 `owner/repo`；当前为 `suyoutzy/dexterous-paper-digest`。保留默认分支 `main`。
3. 新仓库打开 **Settings → Secrets and variables → Actions → Secrets → New repository secret**，分别添加下表三项。可以继续使用原来的有效凭据，旧仓库的 Secrets 不随代码复制。

| 名称 | 填写内容 |
| --- | --- |
| `LLM_API_KEY` | 现用的 DeepSeek 官方 API Key |
| `FEISHU_WEBHOOK` | 现用飞书群机器人 Webhook |
| `FEISHU_SIGN_SECRET` | 同一个机器人的签名校验密钥 |

密钥只填到 GitHub 设置页。普通模型、筛选关键词和研究偏好放在 `digest_config.yaml`。公开的是代码和论文元数据，访客不会获得上述 Secrets。

## 迁移验证与切换经验

上传后由维护者检查 Actions 是否允许 GitHub 官方工作流组件，再执行 owner 在 `main` 上发起的预览。四个任务分别获得最少所需权限，只有提交公开数据的任务要求 `contents: write`；不需要开启创建或批准 PR 的权限。

**今天已经成功发送时，普通预览也会跳过。** 这不能证明新 API Key 可用。可在下一天验证，或使用仅写临时目录的状态副本进行只预览验证；不能为了测试清空真实发送记录。

新仓库验证通过后，确认旧仓库没有正在投递或排队的日报，再停用旧工作流。切换前重新读取旧仓库的最新发送状态和对应论文元数据，避免本地准备期间发生新投递。最后在新工作流开启 `schedule`，使用 UTC `0 0 * * *`（北京时间 08:00）；首次定时投递后可在 Actions 与飞书群核对结果。此次按上述顺序完成切换。

新旧仓库的并发限制互不共享，所以必须按上述顺序切换。旧仓库先保留作回查和恢复用途。

## 数据与日常维护

运行入口是 `python -m daily_digest`。程序可以从不存在的数据库和发送状态开始运行，自动建立目录；迁移保留已有发送记录以避免重发，并保留对应标题、作者、DOI 来支持版本去重。

本次初始数据库共 42 条：5 条已确认推送论文和 37 条本系统首次采集的近期相关记录。没有搬入其余 994 条继承档案。未来检索会继续积累元数据，不保存 PDF 或正文。

手动 `preview` 只生成预览和临时产物，`send` 会推送并提交记录；`retry_uncertain` 平时保持关闭。飞书投递结果不确定时，先核对群聊再决定是否重发。完成的批次不会自动重放。

飞书卡片的归档链接从 GitHub 当前仓库地址自动生成，记录提交后可打开。历史日报中的旧验收链接保留作为历史来源。

arXiv 与期刊检索失败时尝试降级来源；所有来源不可用且没有近期候选缓存时停止，不发送误导性的空日报。模型错误或投递失败可在 Actions 红色步骤查看脱敏信息。

维护模型与计费设置时以服务提供方当前文档为准。定时任务保留闲时调用检查；手动执行可能产生当前时段的费用。08:00 是计划启动时间，GitHub 可能排队。

参考：[新建仓库](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository)、[设置 Secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)、[Actions 设置](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository)。
