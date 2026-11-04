# 灵巧手论文日报

面向灵巧末端设计与控制学习，每两天检索 arXiv、七本期刊和七个会议的公开元数据，用 DeepSeek 根据标题、摘要和日期筛选论文，推送到飞书群。

**每两天推送一次；每天北京时间 18:17 主检查，22:17 备用检查。** 两次计划均落在项目现有峰时保护允许的连续窗口 18:00–次日 09:00 内，为调度延迟和全文生成留出余量。GitHub 调度可能延迟或丢弃，实际启动和送达可能在次日凌晨；更换时段后的延迟需观察，不能保证准点。从上次成功确认的北京时间日期起，至少间隔两个日期才生成下一批；未到日期时直接跳过，不检索或调用模型。失败后保留补发机会，不采用每月重置的隔日 cron。峰时仍在检索前和每次模型请求前检查。

[配置与维护说明](docs/daily_setup.zh.md) · [筛选设置](digest_config.yaml) · [日报归档](docs/digests)

## 研究偏好

针对具备嵌入式基础、已经组装 Aero Hand 的直博新生，优先推荐多指手机构与腱绳传动、驱动与传感、标定、控制、触觉、抓取及手内操作。重视能够积累基础知识和复现实验经验的工作。

常规从近三十天论文中建立最多三十篇候选，按每批最多五篇评分，推荐门槛为 65 分。每次最多推荐五篇：

1. 从全部合格候选中按评分选择前四篇，来源可以是 arXiv、期刊或会议。
2. 再从剩余合格期刊候选中选择评分最高的一篇，与前四篇不重复；会议不占这个期刊名额。

前四篇已经有期刊论文时，仍额外选择另一篇期刊论文；两篇可以来自同一本期刊。近三十天没有额外合格期刊时，只为这个名额扩大到近九十天补查，再评分最多十篇尚未评过的期刊候选，保持前四篇不变。补查后仍没有合格且未发送的期刊论文时少发，并明确说明缺额，不用 arXiv 论文替代期刊名额。

只对入选论文优先读取公开全文，生成 V4 结构化科研速览；分析不改变评分和名单。每篇基本信息后固定四部分：研究问题与核心方法、实验结果与局限、关键术语、科研启发与阅读建议。正文建议约 1000 个汉字，通常 900–1100，硬性上限为 1500；技术方法为重点，尽量结合论文真实任务解释输入、中间处理和输出，不强制关联某个机器人平台。[内容规则与字段兼容说明](docs/digest_content_v4.zh.md)

每篇精选论文按主要研究问题和贡献标注工作模块，可多个，在中英文标题下显示“工作模块：名称”，飞书与归档保持一致：

1. 机器人本体与硬件
2. 建模、辨识与仿真
3. 运动规划与控制
4. 感知与状态估计
5. 灵巧操作与策略学习
6. 高层任务与技能规划

分类在内容生成请求中完成，不增加单独的分类调用；缺失或无效分类会触发当前内容批次重试一次，实际发送时仍无效则暂缓该篇并继续其他论文，普通手动预览严格报错。全文分析默认逐篇生成，单次输入最多 100,000 字符；摘要评分仍按原有 24,000 字符、最多五篇一批执行。

飞书卡片以当天精选数量开头，每篇显示 `arXiv` 或期刊全名与缩写；整体来源、筛选范围与补查说明放在最后。

公开 HTML 全文优先，必要时临时读取 PDF；不将全文或 PDF 写入论文数据库、待发送批次或归档。全文不可得时回退摘要和来源页面，并逐篇标明阅读依据；缺摘要时不编造方法或结果。全文文本提取不等于图表、公式核验或实验复现，过长或提取不完整时明确标为全文节选。模型评分表示个人阅读价值，不代表学术质量认证。

## 来源与运行

来源清单如下；全部仍按灵巧手设计、感知与控制方向筛选，不因会议或期刊知名度推荐无关论文。

| 类别 | 监测名单 |
| --- | --- |
| 期刊 | Science Robotics、Nature Machine Intelligence、IEEE Transactions on Robotics（T-RO）、The International Journal of Robotics Research（IJRR）、IEEE Robotics and Automation Letters（RA-L）、Journal of Field Robotics（JFR）、Journal of Machine Learning Research（JMLR） |
| 机器人会议 | Robotics: Science and Systems（RSS）、Conference on Robot Learning（CoRL）、IEEE International Conference on Robotics and Automation（ICRA）、IEEE/RSJ International Conference on Intelligent Robots and Systems（IROS） |
| 机器学习会议 | Conference on Neural Information Processing Systems（NeurIPS）、International Conference on Machine Learning（ICML）、International Conference on Learning Representations（ICLR） |

arXiv 使用官方 API、RSS 和近期缓存。六本期刊使用 Crossref ISSN 清单，JMLR 读取官网 RSS；缺摘要时有限尝试 OpenAlex 补充。RSS、ICRA、IROS、NeurIPS 使用 OpenAlex 精确会议来源，CoRL、ICML 读取 PMLR 正式论文集，ICLR 读取 OpenReview 已录用主会场记录。会议记录独立检索，不能把 arXiv 自报会议信息当作已核验录用。

会议论文集中发布，最近30天可能没有新条目。来源索引可能延迟、缺失或限制访问，不能保证全量覆盖；来源失败、分页上限和资料日期依据会在归档中说明。OpenReview 使用来源登记录用日期，未独立核验；JMLR 仅提供发表年份的记录会保留元数据，不能确认近期时不自动推荐。不会使用索引更新或 RSS 构建时间把旧论文变成新论文。

摘要评分与总结均使用官方 DeepSeek `deepseek-flash`，思考强度 `low`，配置见 `digest_config.yaml`。GitHub 云端执行，无需电脑开机或开启代理；定时任务在检索前检查峰时，进入峰时记为正常跳过，每次模型请求前仍复查。模型 JSON、论文数量、ID 或必填内容不合格时，仅重试当前批次一次；评分失败时停止，定时或手动实际发送的摘要失败时暂缓相关论文，全部没有合格内容时不发送。普通手动预览严格报告失败。

三个凭据仅存放在 GitHub Repository Secrets：`LLM_API_KEY`、`FEISHU_WEBHOOK`、`FEISHU_SIGN_SECRET`。手动入口默认 `preview`，不发送；`send` 才实际投递。

## 数据与维护

`.daily/` 保存已确认的发送记录与待发送批次，`docs/digests/` 保存日报，`docs/papers_db.json` 保存相关论文元数据。数据库不只保存当天入选的论文；日期保留实际精度，在线发表与索引更新分开记录，已确认推送的论文会跳过。

遇到飞书投递结果不确定时，先核对群聊，再按[维护说明](docs/daily_setup.zh.md)处理。不要清空发送状态来测试，以免重复推送。

运行入口为 `python -m daily_digest`。项目采用 [Apache-2.0 许可证](LICENSE)，项目信息见 [NOTICE](NOTICE)。
