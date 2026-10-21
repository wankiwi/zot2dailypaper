# zot2dailypaper

根据 Zotero 文库和语义关键词推荐每日新论文，按期刊、预印本和随机推荐分组，通过 SMTP 发送邮件。支持出版社 RSS/Crossref、arXiv、bioRxiv、medRxiv、Research Square 和 OpenReview；可生成受每日预算约束的中文一句话摘要。

基于 [TideDra/zotero-arxiv-daily](https://github.com/TideDra/zotero-arxiv-daily)，感谢原作者及贡献者。保留原 fork 关系及 [GNU AGPL v3](LICENSE) 许可证。

## 安装与测试

需要 Python ≥3.13 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/wankiwi/zot2dailypaper.git
cd zot2dailypaper
uv sync --frozen
uv run --frozen pytest                         # 隔离测试，无真实邮件或付费模型调用
uv run --frozen zot2dailypaper --help           # 只显示配置帮助
uv run --frozen python scripts/preview_email.py # 生成合成邮件预览
```

Python 模块入口为 `python -m zot2dailypaper.main`。从 wheel 安装时，配置文件由部署方提供，通过 `zot2dailypaper --config-path /absolute/path/to/config --help` 检查；仓库运行直接使用根目录的 `config/`。

## GitHub Actions 配置

在 **Settings → Secrets and variables → Actions** 设置以下字段，凭据只通过 Secrets 或环境变量引用。

| 类型 | 字段 |
|---|---|
| Secrets | `ZOTERO_ID`、`ZOTERO_KEY`、`SENDER`、`SENDER_PASSWORD`、`RECEIVER` |
| 可选 Secrets | LLM 的 `OPENAI_API_KEY`；OpenReview 的 `OPENREVIEW_USERNAME` 和 `OPENREVIEW_PASSWORD`；加密缓存的 `EMBEDDING_CACHE_KEY` |
| Variables | `CUSTOM_CONFIG`、`PAPER_CONFIG`、`SMTP_SERVER`、`SMTP_PORT`、`LLM_MODEL`、`OPENAI_API_BASE`（也可由同名 Secret 提供） |

`CUSTOM_CONFIG` 是无 `defaults` 的 YAML mapping。配置优先级为 **preset → CUSTOM_CONFIG → 显式手动参数 → 仓库投递策略**；缺省字段继承 preset。不要用示例覆盖整个现有配置。

```yaml
executor:
  quotas: {journals: 25, preprints: 20, random: 5}
llm:
  budget: {enabled: true, daily_cny: 0.30, timezone: Asia/Singapore}
```

当前配额为 25/20/5，总量 50；预算为每日 ¥0.30，均以现有 `CUSTOM_CONFIG` 为准。配额非空时以其合计为总量，不足不跨组补齐。定时策略使用已保存的 interests 过滤、来源和关键词，保持中文摘要、预算守卫和仅邮件投递，RSS/Pages 输出关闭。

每日运行时间为 **19:17 UTC（次日 03:17 Asia/Singapore）**，GitHub 调度可能延迟。定时任务的 arXiv 检索窗口至少为 7 天，以覆盖公开延迟；旧 `CUSTOM_CONFIG` 的 1 天覆盖会被提升至 7 天，更长窗口及其他来源设置保留。`Send emails daily` 手动运行可设置 `sources`、`preprint_profile`、`llm_mode`、`window_days` 和 `recipient`；手动运行仍尊重显式窗口。启动完整应用会执行投递，例如以下命令会发送邮件：

```bash
uv run --frozen zot2dailypaper --config-name=interests llm.enabled=false
```

完整字段、脱敏配置、兴趣权重、来源过滤、摘要恢复、预算/重试、OpenReview 登录、加密缓存和可选离线工具见 [配置与维护参考](docs/CONFIGURATION.md)。

## 状态与改名兼容性

- `paper-state` 分支保存投递历史、待投递记录、期刊目录及 `llm_budget.json` 预算账本。沿用原分支、文件和 schema，不清空、不重新初始化。独立的 `arxiv_health.json` 只保存每日抓取/过滤计数及零结果天数，连续三个 UTC 日零候选时告警，不将合法空结果当作来源失败。
- `embedding-private-v1-` / `embedding-validation-v1-` 缓存 key、模型命名空间和 AES-GCM v1 认证字符串保留原身份；认证字符串中的 `wankiwi/zotero-arxiv-daily` 是兼容标识。可信运行检查使用新仓库名，仍仅允许 main 的定时/手动运行。
- Zotero 保存服务的 Site、登录身份和已加密凭据沿用既有部署。仓库改名不启用保存入口；其摘要来源 URL 的后续更新及过渡说明见 [改名审计](docs/RENAME.md)。

分数范围 0–100，表示相关性而非概率或准确率；来源过滤和文库/历史去重先于配额选择。未成功生成 AI 摘要时保留原摘要，缺失时明确标注。SMTP 接受不保证收件箱到账。排查缺额或失败时检查来源、日期窗口、过滤、阈值和结构化状态，不清历史或补预算。

arXiv API 的官方 HTTP 论文链接仅在验证域名、路径和论文 ID 后升级为 HTTPS；新抓取和历史恢复使用同一规则，投递身份不变，其他 HTTP 链接仍被同步校验拒绝。已保存批次的有边界补发入口、SMTP 不确定结果处理及本次授权范围见[恢复投递说明](docs/CONFIGURATION.md#恢复已保存的待发批次)。
