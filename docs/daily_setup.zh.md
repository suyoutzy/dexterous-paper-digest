# 灵巧手日报：配置与维护

**每两天推送一次；每天北京时间 18:17 主检查，22:17 备用检查。** 2026-11-04 将原来的 09:07/08:17 检查改到晚间，利用项目现有峰时保护允许的连续窗口 18:00–次日 09:00，为调度延迟和全文生成留出余量。11 月 2–4 日的六次原计划检查实际在 14:18–15:47 启动，全部被下午峰时保护跳过；按两次运行的先后对应原计划估算，延迟约六小时，不能把这个延迟当成新时段的固定规律。[最近一次跳过记录](https://github.com/suyoutzy/dexterous-paper-digest/actions/runs/41792212210)

GitHub 在云端运行，无需个人电脑开机或开启代理。18:17 和 22:17 都是计划检查时间，调度可能排队、延迟或丢弃，实际启动和送达可能在次日凌晨。更换时段后的延迟需重新观察；生成步骤会记录本次触发的 UTC cron 和实际开始时间，便于区分主检查与备用检查。两次触发共用运行互斥设置；距离上次成功确认少于两个北京时间日期时，在检索与模型调用前退出。失败不推进间隔，后续检查可补发；飞书投递结果不确定时仍需要人工核对。恢复旧批次后，从实际收到全部成功确认的日期计算下一次间隔，日报归档保留原批次日期。

## 当前设置

| 项目 | 设置 |
| --- | --- |
| 运行时间 | 北京时间每天 18:17 主检查、22:17 备用检查；UTC cron `17 10 * * *`、`17 14 * * *` |
| 推送间隔 | `delivery_interval_days: 2`；按北京时间日期计算，例如 10 月 9 日成功后，10 月 10 日跳过，10 月 11 日可发送；不保证严格相隔 48 小时 |
| 模型 | 官方 DeepSeek `deepseek-flash`，评分与总结均为思考强度 `low` |
| 接口 | `https://api.deepseek.com`，Chat Completions |
| 候选与门槛 | 常规最多 30 篇，每批最多 5 篇；期刊补查时另评最多 10 篇尚未评过的期刊候选；低于 65 分不推荐 |
| 推荐数量 | 最多 4 篇综合精选，加 1 篇额外期刊论文；不足时少发 |
| 日期范围 | 最近 7 天优先，常规范围为最近 30 天；额外期刊名额不足时，才单独扩展到最近 90 天 |
| 阅读范围 | 筛选仍依据摘要与元数据；入选后优先读取公开全文，失败则逐篇标注回退范围 |

## 到期后的筛选流程

1. **检索与去重**：查询 arXiv、期刊和会议公开元数据，保存相关论文，不只保存当天入选名单。按 DOI、arXiv ID 和能识别的版本关系去重，跳过已确认发送的论文；移出监测清单的期刊和会议不再从缓存进入推荐。
2. **分批评分**：最近 30 天内最多 30 篇候选按每批最多 5 篇评分。作者背景只能辅助判断，不能凭姓名推测机构、引用数或学术权威。
3. **确定前四篇**：从所有达到 65 分门槛的候选中，按评分选择最多四篇，来源可以是 arXiv、期刊或会议。
4. **增加期刊论文**：从未入选的合格期刊候选中，再选择评分最高的一篇。前四篇已有期刊论文时，仍额外选择一篇不同论文；不要求来自不同期刊。近 30 天没有额外合格期刊时，保持前四篇不变，只为期刊名额扩大到近 90 天补查；只评分尚未评过的最多 10 篇期刊候选，再选评分不低于 65 分且未发送的一篇。近 90 天仍没有合格论文时保留缺额并明确说明，不用 arXiv 或会议论文补充这个名额。常规运行不总是扩大到 90 天。
5. **V4 科研速览**：只对入选论文读取公开 HTML/PDF，按研究问题与方法、实验与局限、关键术语、科研启发与阅读建议四部分生成内容，不改变评分和名单。全文不可得时用摘要及来源页面，明确阅读范围；缺摘要时不编造方法、结果或性能。模块可多选，只显示名称。[完整规则与字段兼容](digest_content_v4.zh.md)
6. **投递与记录**：先保存待发送批次，再推送飞书；只有全部卡片收到成功确认后，才更新已发送记录和日报归档。

实验结果仍是作者报告，不代表本任务完成独立复现。正文优先读取可提取文本；图像、公式和补充材料不自动视为已核验。全文过长或部分页面无法提取时标为节选。全文访问仅限公开入口，不绕过访问限制；PDF 和提取正文仅用于本次生成，不保存进公开数据库或投递产物。评分预算不变，全文分析逐篇请求，输入上限 100,000 字符，输入量及生成耗时会高于旧摘要版。

## 论文来源

arXiv API 暂不可用时尝试官方 RSS 与近期缓存。期刊名单如下；前六本使用 Crossref ISSN 查询，JMLR 读取官网 `jmlr.xml`（其 Crossref 入口未提供记录，已查到的 OpenAlex 期刊来源停留在旧年份）：

| 期刊 | ISSN |
| --- | --- |
| IEEE Transactions on Robotics（T-RO） | `1552-3098` |
| IEEE Robotics and Automation Letters（RA-L） | `2377-3766` |
| The International Journal of Robotics Research（IJRR） | `0278-3649`、`1741-3176` |
| Science Robotics | `2470-9476` |
| Nature Machine Intelligence | `2522-5839` |
| Journal of Field Robotics（JFR） | `1556-4959`、`1556-4967` |
| Journal of Machine Learning Research（JMLR） | `1533-7928`、`1532-4435` |

| 会议 | 检索来源 |
| --- | --- |
| Robotics: Science and Systems（RSS） | OpenAlex 精确会议来源 |
| Conference on Robot Learning（CoRL） | PMLR 正式论文集 RSS 与作者目录 |
| IEEE International Conference on Robotics and Automation（ICRA） | OpenAlex 精确会议来源 |
| IEEE/RSJ International Conference on Intelligent Robots and Systems（IROS） | OpenAlex 精确会议来源 |
| Conference on Neural Information Processing Systems（NeurIPS） | OpenAlex 精确会议来源 |
| International Conference on Machine Learning（ICML） | PMLR 正式论文集 RSS 与作者目录 |
| International Conference on Learning Representations（ICLR） | OpenReview 已录用主会场 `venueid` |

所有来源继续按灵巧手设计与控制方向筛选。会议来源保留独立身份，无 DOI 的论文也可进入前四篇综合排名。PMLR 只读正式主会论文集，排除同名 Workshop；OpenReview 不收投稿中、拒稿或撤稿记录。OpenReview 的 `pdate` 表示来源登记录用日期，卡片与归档按此标注，不混同首次公开日期；来源可设置该字段，未独立核验录用时间。缺少可用日期的条目不假造日期。JMLR 官网仅提供年份时保留年份，不用首次发现日期代替发表日期，因此无法确认近期的记录只入库、不自动推荐。论文集按年集中发布，最近30天没有条目并不表示没有监测。

OpenAlex 来源 ID 和 OpenReview 会场配置在 `conferences.watchlist`，默认每个来源最多两页、每页100条，OpenAlex 先按相关关键词缩小范围；PMLR 每个会议最多读取最近两届正式论文集的公开元数据。不新增模型评分名额。索引不保证全量或即时，匿名 API 也可能限流；拒绝访问、分页上限和部分论文集失败会在归档中说明。现有三项 Secrets 无需更改。

Crossref 缺摘要时，有限尝试 OpenAlex 补充；没有摘要仍保留缺失标记。期刊登记依赖出版方提交元数据，可能延迟或缺失，不保证即时、全量覆盖。

出版日期保留实际的年、月或日精度，在线发表与索引更新分别说明，索引今日更新不代表今日发表。每篇卡片显示 `arXiv`、期刊或会议全名与缩写；整体来源与补查说明位于最后一张卡片末尾。来源提供的作者、机构和发表信息未经独立核实。

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
- `force_send`：仅用于明确要求的手动额外批次，临时忽略日期间隔，仍排除已发送论文；单独归档为日期加运行编号，不覆盖当天原日报。定时任务不会启用此选项。

手动额外批次中，某篇内容在重试一次后仍不合格时，只发送其余通过校验的论文，并说明未通过数量；不合格论文不计为已发送。全部不合格时不发送。定时任务仍要求整批通过校验。

Actions 的 **Summary** 显示推荐内容与 token 用量，或本次正常跳过的原因。未到发送日期、定时生成进入峰时都按成功退出，后续投递作业跳过；真正错误仍显示失败。已确认日报保存在 `docs/digests/`，运行产物中不包含密钥或 PDF 正文。

**发送间隔未到时，普通预览也会跳过。** 这不能证明新 API Key 可用。可在下次到期日期验证，或使用仅写临时目录的状态副本进行只预览验证；不能为了测试清空真实发送记录。

## 故障处理与维护

- **模型失败**：先查看错误。`401` 检查 Key，`402` 检查余额，限流或服务暂不可用时等待后再试，不连续点击重跑。
- **模型输出校验失败**：JSON、论文数量、ID 或必填内容不合格时，当前评分/总结批次最多重试一次，已通过的批次不重做。第二次仍不合格则终止，不创建发送意图；鉴权、网络或峰时异常不触发这种内容重试，底层限流/网络重试维持原有上限。
- **峰时正常跳过**：工作日北京时间 09:00–12:00、14:00–18:00 不开始定时模型调用；先在检索前检查，再在每次请求前复查。中国法定节假日仍保守地按工作日处理。已有待投递批次可以恢复，不需要重新调用模型。
- **检索受限**：尝试后备来源并说明覆盖限制；所有来源不可用且没有近期候选缓存时停止，不发送误导性的空日报。
- **飞书明确拒绝**：检查签名密钥、机器人关键词等设置；新运行恢复原批次，跳过已确认卡片。
- **投递超时或结果未写回**：先核对飞书群，再决定是否使用 `retry_uncertain`。已经收到时不要重发，应核对并修复发送记录。

运行入口是 `python -m daily_digest`。`docs/papers_db.json` 持续保存相关元数据，`.daily/` 保存发送状态与待发送批次，`docs/digests/` 保存已确认日报。保留已发送论文的标题、作者、DOI 等去重信息，不要清空真实状态来测试；完成的批次不会自动重放。

卡片归档链接从当前仓库地址生成，记录提交后可打开。模型、研究偏好、质量门槛、推送间隔与期刊清单可在 `digest_config.yaml` 调整，修改后应重新验证。手动运行遵守发送间隔，但不限制模型时段，按执行时段计费，以服务提供方当前价格和账单为准。

本仓库未设置调度时区，cron 按 UTC 计算：UTC 10:17/14:17 对应北京时间 18:17/22:17。[官方调度说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)确认整点属于高负载时刻，建议避开整点；未给出全球按小时划分的最忙/最闲排名。晚间计划为模型生成留出更长余量，更换小时仍不能保证准时创建任务。若延迟接近最近约六小时的估算，主检查与备用检查可能分别在次日零点和四点附近启动；这是基于旧时段的推算，需用新运行验证。

参考：[设置 Secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)、[Actions 设置](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository)、[DeepSeek 官方定价](https://api-docs.deepseek.com/quick_start/pricing/)。
