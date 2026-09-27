# 灵巧手论文日报

面向灵巧末端设计与控制学习，每天检索 arXiv 和五本期刊的公开元数据，用 DeepSeek 根据标题、摘要和日期筛选最多五篇，推送到飞书群。

**迁移状态：本地工程已整理，新仓库尚未部署，定时触发暂未开启。**

[配置与维护说明](docs/daily_setup.zh.md) · [筛选设置](digest_config.yaml) · [日报归档](docs/digests)

## 研究偏好

针对具备嵌入式基础、已经组装 Aero Hand 的直博新生，优先推荐多指手机构与腱绳传动、驱动与传感、标定、控制、触觉、抓取及手内操作。重视能够积累基础知识和复现实验经验的工作。

最多三十篇候选分批评分，统一选出最多五篇；低于推荐门槛时少发。入选论文提供中文方法摘要、适合你的理由、一个学习或实验步骤，以及论文和公开代码链接。作者背景只能作有依据的辅助信息。

不下载 PDF 或保存正文。摘要中的实验是作者报告，未进行全文核验。模型评分表示个人阅读价值，不代表学术质量认证。

## 来源与运行

来源为 arXiv API（官方 RSS 和近期缓存备用），以及 Crossref 按 ISSN 监测的 T-RO、RA-L、IJRR、Science Robotics、Soft Robotics；缺摘要时有限尝试 OpenAlex 补充。期刊元数据可能延迟或缺失，不能保证全量覆盖。

摘要评分与总结沿用 `digest_config.yaml` 中的官方 DeepSeek 配置。目标运行时间为北京时间每天 08:00，GitHub 云端执行，无需电脑开机。调度可能延迟；定时模型调用保留闲时检查。

三个凭据仅存放在 GitHub Repository Secrets：`LLM_API_KEY`、`FEISHU_WEBHOOK`、`FEISHU_SIGN_SECRET`。手动入口默认 `preview`，不发送；`send` 才实际投递。

## 数据与来源说明

`.daily/` 保存已确认的发送记录，`docs/digests/` 保存日报，`docs/papers_db.json` 保存元数据。迁移初始数据保留五篇已发送论文的去重信息及三十七篇本系统采集的近期相关记录；不搬入其余继承档案、旧会议脚本或原论文网页。

新检索会继续积累相关元数据。系统不读取或同步原作者后来生成的日报。

本工程由 [cold-young/robotics_paper_daily](https://github.com/cold-young/robotics_paper_daily) 的 Fork 项目整理而来；保留 [Apache-2.0 许可证](LICENSE) 和[来源说明](NOTICE)。新增 `daily_digest/` 是本项目的运行入口。
