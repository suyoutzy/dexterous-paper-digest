# 灵巧手日报：配置与维护

**每天北京时间 08:00 主触发，08:17 备用检查。** 2026-09-27 已完成[真实来源与模型的云端预览验证](https://github.com/suyoutzy/dexterous-paper-digest/actions/runs/36323738971)：五篇入选论文的来源标签、额外期刊名额和末尾来源说明均通过检查；验证记录见 `.daily/validation.json`。本次验证没有发送飞书消息，也没有测试飞书实际投递。2026-09-28 08:00 首轮主触发没有产生运行记录；备用触发自配置提交后的下一次时刻起生效，实际投递结果仍待确认。

GitHub 在云端运行，无需个人电脑开机或开启代理。08:00 和 08:17 都是计划触发时间，调度可能排队、延迟或丢弃。两次触发共用运行互斥设置；当天已确认发送时，后一次会在调用模型前退出。未确认的投递不会自动重发。

## 当前设置

| 项目 | 设置 |
| --- | --- |
| 运行时间 | 北京时间每天 08:00 主触发、08:17 备用检查；UTC cron `0 0 * * *`、`17 0 * * *` |
| 模型 | 官方 DeepSeek `deepseek-flash`，评分与总结均为思考强度 `low` |
| 接口 | `https://api.deepseek.com`，Chat Completions |
| 候选与门槛 | 常规最多 30 篇，每批最多 5 篇；期刊补查时另评最多 10 篇尚未评过的期刊候选；低于 65 分不推荐 |
| 推荐数量 | 最多 4 篇综合精选，加 1 篇额外期刊论文；不足时少发 |
| 日期范围 | 最近 7 天优先，常规范围为最近 30 天；额外期刊名额不足时，才单独扩展到最近 90 天 |
| 阅读范围 | 标题、摘要、日期与来源元数据；不下载 PDF 或保存正文 |

## 每日筛选流程

1. **检索与去重**：查询 arXiv 和期刊公开元数据，保存相关论文，不只保存当天入选名单。按 DOI、arXiv ID 和能识别的版本关系去重，跳过已确认发送的论文。
2. **分批评分**：最近 30 天内最多 30 篇候选按每批最多 5 篇评分。作者背景只能辅助判断，不能凭姓名推测机构、引用数或学术权威。
3. **确定前四篇**：从所有达到 65 分门槛的候选中，按评分选择最多四篇，来源可以是 arXiv 或期刊。
4. **增加期刊论文**：从未入选的合格期刊候选（Crossref 监测清单）中，再选择评分最高的一篇。前四篇已有期刊论文时，仍额外选择一篇不同论文；不要求来自不同期刊。近 30 天没有额外合格期刊时，保持前四篇不变，只为期刊名额扩大到近 90 天补查；只评分尚未评过的最多 10 篇期刊候选，再选评分不低于 65 分且未发送的一篇。近 90 天仍没有合格论文时保留缺额并明确说明，不用 arXiv 论文补充这个名额。常规运行不总是扩大到 90 天。
5. **中文总结**：只对入选论文生成核心方法、推荐理由和一个可积累的学习或实验步骤，总结不改变评分和名单。缺摘要时标为“仅标题”，说明信息不足，不编写方法、实验结果或性能结论。
6. **投递与记录**：先保存待发送批次，再推送飞书；只有全部卡片收到成功确认后，才更新已发送记录和日报归档。

摘要中的实验结果是作者报告，本任务没有核对正文、图表或实际复现。模型分数表示个人阅读价值，不能作为学术质量认证。不下载 PDF、不保存正文、不查询 Hugging Face 额外元数据，也不假设 Aero Hand 自带触觉或力矩传感器。

## 论文来源

arXiv API 暂不可用时尝试官方 RSS 与近期缓存。期刊通过 Crossref 按 ISSN 监测：

| 期刊 | ISSN |
| --- | --- |
| IEEE Transactions on Robotics（T-RO） | `1552-3098` |
| IEEE Robotics and Automation Letters（RA-L） | `2377-3766` |
| The International Journal of Robotics Research（IJRR） | `0278-3649`、`1741-3176` |
| Science Robotics | `2470-9476` |
| Soft Robotics | `2169-5172`、`2169-5180` |

Crossref 缺摘要时，有限尝试 OpenAlex 补充；没有摘要仍保留缺失标记。期刊登记依赖出版方提交元数据，可能延迟或缺失，不保证即时、全量覆盖。

出版日期保留实际的年、月或日精度，在线发表与索引更新分别说明，索引今日更新不代表今日发表。每篇卡片显示 `arXiv` 或期刊全名与缩写，例如 IEEE Robotics and Automation Letters（RA-L）；整体来源与补查说明位于最后一张卡片末尾。来源提供的作者、机构和发表信息未经独立核实。

## 凭据与权限

以下三项 Secrets 已配置，无需重复添加。需要更新时，在[仓库](https://github.com/suyoutzy/dexterous-paper-digest)打开 **Settings → Secrets and variables → Actions → Secrets**，编辑对应条目。

| 名称 | 填写内容 |
| --- | --- |
| `LLM_API_KEY` | 现用的 DeepSeek 官方 API Key |
| `FEISHU_WEBHOOK` | 现用飞书群机器人 Webhook |
| `FEISHU_SIGN_SECRET` | 同一个机器人的签名校验密钥 |

密钥只填到 GitHub 设置页，不写入代码、日报、数据库、缓存或日志。普通模型、筛选关键词和研究偏好放在 `digest_config.yaml`。公开的是代码和论文元数据，访客不会获得上述 Secrets；只给可信的人仓库写入权限。

四个任务分别获得最少所需权限：模型步骤读取模型 Key，投递步骤读取飞书凭据，提交公开数据的步骤使用 `contents: write`。手动运行仅允许仓库所有者在 `main` 发起，不需要开启创建或批准 PR 的权限。

## 运行与查看结果

在 [Actions 页面](https://github.com/suyoutzy/dexterous-paper-digest/actions/workflows/dexterous_digest.yml)选择 **Dexterous Hand Daily Digest**，点击 **Run workflow**，分支保留 `main`：

- `preview`：只生成预览和临时产物，不发送、不更新已发送记录。
- `send`：实际推送飞书，并提交确认结果。
- `retry_uncertain`：平时关闭；核对群聊并确认需要重发未确认卡片时才打开，可能产生重复。

Actions 的 **Summary** 显示推荐内容与 token 用量；红色步骤中查看脱敏错误。已确认日报保存在 `docs/digests/`，运行产物中不包含密钥或 PDF 正文。

**今天已经成功发送时，普通预览也会跳过。** 这不能证明新 API Key 可用。可在下一天验证，或使用仅写临时目录的状态副本进行只预览验证；不能为了测试清空真实发送记录。

## 故障处理与维护

- **模型失败**：先查看错误。`401` 检查 Key，`402` 检查余额，限流或服务暂不可用时等待后再试，不连续点击重跑。
- **检索受限**：尝试后备来源并说明覆盖限制；所有来源不可用且没有近期候选缓存时停止，不发送误导性的空日报。
- **飞书明确拒绝**：检查签名密钥、机器人关键词等设置；新运行恢复原批次，跳过已确认卡片。
- **投递超时或结果未写回**：先核对飞书群，再决定是否使用 `retry_uncertain`。已经收到时不要重发，应核对并修复发送记录。

运行入口是 `python -m daily_digest`。`docs/papers_db.json` 持续保存相关元数据，`.daily/` 保存发送状态与待发送批次，`docs/digests/` 保存已确认日报。保留已发送论文的标题、作者、DOI 等去重信息，不要清空真实状态来测试；完成的批次不会自动重放。

卡片归档链接从当前仓库地址生成，记录提交后可打开。模型、研究偏好、质量门槛与期刊清单可在 `digest_config.yaml` 调整，修改后应重新验证。定时调用保留闲时检查；手动运行按执行时段计费，以服务提供方当前价格和账单为准。

参考：[设置 Secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)、[Actions 设置](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository)、[DeepSeek 官方定价](https://api-docs.deepseek.com/quick_start/pricing/)。
