# 配置与维护参考

返回 [zot2dailypaper README](../README.md)。配置字段的默认值见 [config/base.yaml](../config/base.yaml)，运行时以现有 `CUSTOM_CONFIG` 为准。以下示例不包含凭据或真实 collection 路径。

## 当前配置的脱敏示例

以下为已启用配置与每日投递策略的脱敏示例。已替换 collection 名称、用户标识与所有凭据；`archive/**` **仅为示例，不是用户真实排除路径**。主分支预算守卫及持久账本已生效，在线配置为中文摘要、DeepSeek-V4-Flash、每日¥0.30估算记账额度、以 CUSTOM_CONFIG 为准的25/20/5配额、OpenReview开启和RSS关闭。OpenReview官方登录及公开稿件检索已验证；LLM是否成功以每次运行的生成状态及预算账本为准。GitHub不能读回已有secret，所有凭据继续由用户管理。

```yaml
zotero:
  user_id: ${oc.env:ZOTERO_ID}
  api_key: ${oc.env:ZOTERO_KEY}
  include_path: null
  ignore_path: ["archive/**"]  # 脱敏占位，按自己的 collection 修改
email:
  affiliation_max_chars: 180
  sender: ${oc.env:SENDER}
  receiver: ${oc.env:RECEIVER}
  smtp_server: mail.cstnet.cn
  smtp_port: 994
  sender_password: ${oc.env:SENDER_PASSWORD}
llm:
  enabled: true
  budget: {enabled: true, daily_cny: 0.30}
  language: Chinese
  input_mode: abstract
  api:
    key: ${oc.env:OPENAI_API_KEY}
    base_url: https://api.siliconflow.cn/v1
  generation_kwargs:
    model: deepseek-ai/DeepSeek-V4-Flash  # 已通过受预算约束的单篇中文验证
executor:
  source: [journals, arxiv, biorxiv, researchsquare, openreview]
  max_paper_num: 50 # quotas 非空时以配额之和为准；此字段只用于旧模式
  min_score: -10
  min_score_scale: legacy
  quotas: {journals: 25, preprints: 20, random: 5}
  debug: false
  send_empty: true
source:
  arxiv: {window_days: 1}
  biorxiv: {window_days: 1}
  researchsquare: {window_days: 1}
abstracts:
  enabled: true
  max_papers: 50
  publisher_fallback: true
  publisher_max_papers: 10
  aps_metadata_max_papers: 10
output:
  email: {enabled: true}
  rss: {enabled: false}
preprint_interests:
  arxiv:
    categories: [physics.chem-ph, physics.comp-ph, cond-mat.mtrl-sci, cond-mat.soft, cs.LG, cs.AI]
  biorxiv:
    categories: [biophysics, biochemistry]
  medrxiv: {enabled: false}
  researchsquare:
    backend: openalex
    source_ids: [S4306525896]
    type: [preprint]
    subfield: [Physical and Theoretical Chemistry, Materials Chemistry, General Materials Science, Condensed Matter Physics, Artificial Intelligence, Biophysics, Biochemistry]
  openreview:
    enabled: true
    venues: [ICLR, NeurIPS, ICML, TMLR, CoRL]
    subject_areas: [AI for Science, Machine Learning, Scientific Machine Learning]
    keywords: [atomistic, molecular, chemistry, materials, molecular dynamics]
```

## 全参数参考

完整默认值见 [config/base.yaml](../config/base.yaml)。`???` 必须由 preset、CUSTOM_CONFIG 或环境补齐；`${oc.env:NAME,default}` 为环境引用。CLI 使用 `section.key=value` 覆盖，列表例子：`'zotero.ignore_path=["archive/**"]'`。

### 文库、排序、执行

| 参数 | 默认/意义与例子 |
|---|---|
| `zotero.user_id`, `api_key` | Zotero 用户 ID / 只读 API key，均必填 |
| `zotero.include_path`, `ignore_path` | null 或 glob 字符串列表；如 `["2026/reading/**"]`。排除优先，空列表不筛选。`name` 与 `name/**` 的路径含义不同，按实际 collection 层级选择 |
| `executor.source` | 启用来源列表；journals/arxiv/biorxiv/medrxiv/researchsquare/openreview |
| `executor.reranker` | `local`（默认）或 `api` |
| `executor.quotas` | base=null；interests 默认 `{journals:25, preprints:15, random:5}`，CUSTOM_CONFIG 覆盖（当前25/20/5）。键必须齐全，值为非负整数且总和正数；缺省字段由 preset 补齐。 |
| `executor.max_paper_num` | 仅 quotas=null 时使用的旧全局上限；分组配额非空时以配额之和为准。 |
| `executor.min_score` | 默认 -10；由 min_score_scale 声明量纲。低于阈值者不参与任何组，包括随机。旧 -10 等价新 0，旧 6.5 等价新 82.5。 |
| `executor.min_score_scale` | `legacy`（默认，旧 [-10,10]）或字符串 `"0_100"`。不猜测重叠区间；使用新制阈值时必须显式指定。 |
| `executor.exclude_existing` | true；排除已在整个 Zotero 文库中的论文，不只兴趣子集 |
| `executor.send_empty` | false；允许无新论文时发空邮件，存在源错误时不会伪装成正常空结果 |
| `executor.debug` | false；保留兼容的旧调试选项，当前SMTP不输出会话内容 |
| `executor.fetch_full_text` | 保留旧配置兼容；摘要输入现由llm.input_mode控制，abstract模式不下载全文，full_text模式只对选中论文、可调用模型时获取支持的全文 |
| `executor.enrichment_workers` | base=1，journal 系列=4；1–8，仅 llm.input_mode=abstract 时并发；全文提取不进入线程池 |
| `reranker.local.model` | `jinaai/jina-embeddings-v5-text-nano-retrieval` |
| `reranker.local.revision` | null；可锁定模型 commit/revision |
| `reranker.local.cpu_dtype` | auto；允许auto/native/float32；auto在不支持原生BF16时转float32，native保留模型dtype |
| `reranker.local.cpu_threads` | null；尊重 CPU affinity/cgroup，可显式设线程数 |
| `reranker.local.cache_dir` | null；可选本地 embedding 缓存，含私有向量，不上传 |
| `reranker.local.encode_kwargs` | 传给模型 encode，默认 `{task: retrieval, prompt_name: document}` |
| `reranker.api.key`, `base_url`, `model`, `batch_size` | API embedding 配置；如环境key、OpenAI兼容URL、模型名、64；只在api模式使用 |

### 来源与兴趣过滤

`preprint_interests: null` 保留旧 source 分类；否则为平台 mapping。`enabled: false` 禁用平台；未列入 executor.source 的平台不会仅因 enabled=true 自动运行。分类内 OR，关键词内 OR，不同过滤维度 AND。关键词为大小写无关的字面词/短语，按词边界匹配 title 或 abstract，不跨两个字段拼接，不支持通配符。`categories: null` 继承 source.category；空数组无效。

| 参数 | 默认/意义与例子 |
|---|---|
| `preprint_interests.<platform>.enabled` | true；支持所有五个预印本平台 |
| `preprint_interests.<platform>.keywords` | 可省略；非空短语列表，如 `[molecular dynamics, materials]` |
| `preprint_interests.<arxiv/biorxiv/medrxiv>.categories` | 各平台原生分类。arXiv如 `[cs.LG, physics.chem-ph]`；bioRxiv如 `[biophysics]`；`['*']` 全分类 |
| `source.<arxiv/biorxiv/medrxiv>.category` | base=null；legacy分类配置，被对应 interests.categories 覆盖 |
| `source.arxiv.include_cross_list` | false；RSS及API日期窗口模式是否保留交叉分类记录；false仅保留订阅分类为主分类的论文 |
| `source.arxiv.window_days` | base=null 使用最新RSS announcement；all=7，以 submittedDate 日期窗口检索 API。定时任务在合并 CUSTOM_CONFIG 后至少保留7天，旧1天覆盖提升至7天，更长值保留；手动/本地运行仍尊重显式窗口。其他来源窗口不受此策略影响 |
| `source.<biorxiv/medrxiv>.window_days` | base=null 使用原始模式；all=1，按日期窗口分页 |
| `source.<biorxiv/medrxiv>.max_pages` | 100；分页安全上限，达到未完成时报告错误 |
| `source.<platform>.conversion_delay` | 可选0秒；BaseRetriever 转换间隔（OpenReview自行转换，不使用此项） |
| `source.researchsquare.window_days`, `max_pages`, `mailto` | 1、100、null；日期窗口、页上限、Crossref polite pool 联系邮箱 |
| `preprint_interests.researchsquare.backend` | `openalex` 使用公开OpenAlex；未设时旧Crossref backend |
| `preprint_interests.researchsquare.source_ids` | `[S4306525896]`；实时核对确为Research Square，不能猜ID |
| `preprint_interests.researchsquare.type` | 仅 `[preprint]` |
| `preprint_interests.researchsquare.subfield` | 非空OpenAlex正式子领域名称列表；实时按精确名称解析，重名取所有精确匹配 |
| `source.openreview.enabled` | true；可以关闭来源；interests中的 enabled 优先 |
| `source.openreview.venues` | ICLR、NeurIPS、ICML、TMLR、CoRL；interests.openreview.venues 可覆盖 |
| `source.openreview.subject_areas` | AI for Science、Machine Learning、Scientific Machine Learning；interests 同名参数优先，不支持的值报错 |
| `source.openreview.keywords` | atomistic、molecular、chemistry、materials、molecular dynamics；interests 同名参数优先 |
| `source.openreview.window_days`, `max_pages` | 7、100；1–90天，正整数页上限；每页最多1000条 |
| `source.journals.presets` | jacs,jctc,prl,nature_family,science,science_advances,jcp,jpcl,pnas,acs_catalysis,npjcompumats,angew,chemical_science,mlst |
| `source.journals.custom` | []；自定义 `{id: example, title: Example Journal, issns: ['1234-5679'], rss: 'https://publisher.example/feed'}`，必须提供可核验ISSN或正式名称；示例ISSN只是格式演示 |
| `source.journals.window_days` | 7；从UTC零点向前回溯到现在，以出版日期而非索引日期筛选 |
| `source.journals.workers` | 4；1–16并发期刊；Crossref请求全局限速 |
| `source.journals.max_pages` | 100；每ISSN Crossref cursor分页上限 |
| `source.journals.use_rss` | true；合并出版社RSS与Crossref，不只依赖RSS覆盖历史窗口 |
| `source.journals.discover_nature` | true；刷新Nature官方目录，缓存7天 |
| `source.journals.require_live_catalog` | base=false，journals及派生preset=true；刷新失败时显式报错而非声称完整覆盖 |
| `source.journals.catalog_cache` | data/journal_catalog.json；目录及ISSN缓存 |
| `source.journals.mailto` | null；Crossref请求联系邮箱，勿将私人邮箱写入公共示例 |
| `abstracts.enabled`, `max_papers`, `mailto` | base=false、50、null；interests/定时启用。排名与配额选择后，最多对50篇已选中、缺摘要且有DOI的论文查询两个元数据源；缺摘要候选仍按标题排名，补回摘要不重新排名；401/403/429后跳过该provider，保留警告 |
| `abstracts.publisher_fallback`, `publisher_max_papers` | true、10；Crossref/OpenAlex均未返回摘要时，最多为10篇已选论文尝试Nature或PRL公开摘要页（范围0–50，且受max_papers限制）。每篇最多4次GET（含重定向）、页面最多2MB；只访问HTTPS白名单出版社及Nature的匿名authorize/transit重定向，验证最终文章页面DOI，仅抽取摘要区或明确的citation_abstract。遇401/403/429或挑战页立即停止该站点，不绕过付费墙、不取全文或宣传描述代替摘要 |
| `abstracts.aps_metadata_max_papers` | 10；范围0–50，0关闭。对仍缺摘要的APS DOI，最多处理10篇已选论文，先查Semantic Scholar精确DOI元数据，再用arXiv标题定位最多3个候选并强制验证DOI、标题与明确版本号。每站每篇最多一次GET，2MB上限，不跟随重定向；401/403/429停止该站点。arXiv单连接、请求间隔至少3秒。索引摘要明确标注版本未核实，arXiv标注具体版本及“DOI-linked manuscript”，不覆盖出版社日期或冒称出版社原摘要 |
| `email.zotero_action_origin` | `null` (disabled). Future authenticated confirmation service HTTPS origin, e.g. `https://papers.example.org`; no path/query/credentials. Adds a login-and-confirm navigation link only. Requires a separately approved/deployed service; never enables writes on email GET. See [design and setup requirements](RESEARCHSQUARE_ZOTERO.md). |
| `email.affiliation_max_chars` | 180；HTML/纯文本中作者单位总显示字符上限，包含末尾省略号 `…`，范围20–1000。例：`email: {affiliation_max_chars: 120}`；不修改内部完整单位信息 |

### OpenReview 的真实范围与限制

仅主会议，不含workshops。API v2 `/groups` 核验组，再读取其 `submission_id` 或 `submission_name`，按 submission invitation 拉取公开 notes，避免仅按 accepted venueid 丢失审稿中论文。年度组覆盖当前/上一年，ICLR另含下一年；TMLR无年度。修改时间扫描窗口内实际更新记录，未发表记录日期优先 `odate`（首次公开），缺失时退至 cdate/tcdate（创建时间）；经venue ID和pdate核验的已发表记录使用pdate，以包含新发表的旧投稿。无持久增量水位，重叠窗口+delivery history去重，因此失败不会推进水位；窗口外首次公开但元数据日期错误的记录可能无法恢复，不能声称完整历史覆盖。

官方content字段按 `.value`解包，subject标量/数组都支持。NeurIPS `ai_4_physical_sciences` / `machine_learning_for_sciences`、ICML `applications->chemistry_physics_and_earth_sciences`、ICLR physical sciences 标签映射到科学方向。TMLR和部分CoRL年份无subject；请求包含宽泛的 **Machine Learning** 时，核验的这五个ML主venue作为明确记录的venue-scope fallback，仍必须通过领域关键词过滤。仅配置科学方向且无subject时，用title/abstract/author关键词中的明确科学ML短语匹配；否则排除。每篇保存 `subject_match_reason`，日志报告fallback数。

关键词仅匹配标题、摘要或作者keywords，包括CoRL `primary_keyword`、`secondary_keyword`、`free_keyword_1/2`；**绝不使用subject标签充当关键词**。venue/subject/keyword组间AND，组内OR。不声称OpenReview有统一subject taxonomy。不绕过403 challenge；403、限流、分页不完整或schema缺失均为可见来源失败。API v1-only旧年份不伪装成功。

参考：[官方检索说明](https://docs.openreview.net/how-to-guides/data-retrieval-and-modification/how-to-get-all-notes-for-submissions-reviews-rebuttals-etc)、[Note日期字段](https://docs.openreview.net/reference/api-v2/entities/note/fields)、[API v2规范](https://api2.openreview.net/docs/api.yml)。

### LLM、邮件、输出与历史

| 参数 | 默认/意义与例子 |
|---|---|
| `llm.enabled` | base=true；私有配置控制启用，定时不再强制关闭。预算守卫未验证时仍不调用 |
| `llm.budget.enabled`, `daily_cny` | 守卫必须启用；金额以 CUSTOM_CONFIG 为准（当前 0.30），未覆盖时继承 preset 默认 0.30。没有另一个固定金额覆盖值或上限；缺失、非数值、非有限或非正金额禁止调用。本地将守卫设为 false 也只会禁止模型调用，不会开放无限额调用 |
| `llm.input_mode` | `abstract`（默认）或 `full_text`。abstract不为摘要下载全文；full_text尝试来源支持的合法全文，发送完整提取文本，若不可得/超上下文/超预算输入界限则明确回退abstract，邮件和日志注明原因 |
| `llm.language` | Chinese；prompt明确要求一句话，失败不将英文原摘要冒称中文摘要 |
| `llm.api.key`, `base_url` | OpenAI兼容服务key/URL；key保留环境引用，当前URL显式为 `https://api.siliconflow.cn/v1`，须与预算验证记录一致 |
| `llm.generation_kwargs` | 保留model选择；其他历史参数不再原样透传。受控请求固定max_tokens=96、n=1、enable_thinking=false，不允许额外参数绕过预约。历史max_tokens=16384不会生效 |
| `email.sender`, `receiver`, `sender_password` | 发件人、单收件人、SMTP授权码/密码；工作流从secrets解析 |
| `email.smtp_server`, `smtp_port` | 如smtp.qq.com/465，当前配置为mail.cstnet.cn/994；465/994使用SMTP_SSL，其他端口要求STARTTLS |
| `output.email.enabled` | true；工作流强制true |
| `output.rss.enabled` | false；仅显式本地调用可启用，工作流强制false |
| `output.rss.path`, `title`, `site_url` | public/feed.xml、Daily Paper Recommendations、空；RSS文件、频道名、站点地址 |
| `output.rss.retention_days`, `max_items` | 30、300；feed保留时间/条数 |
| `state.enabled` | base=false，journals及派生preset=true；RSS也强制启用本地历史 |
| `state.path`, `retention_days` | data/recommendations.json、90；原子写入JSON。超保留期的历史不再去重 |

Preset：`base`仅默认，`custom`环境和旧arXiv示例，`legacy`组合二者；`journals`配置通用凭据和期刊；`preprints`四个既有预印本平台；`all`期刊及全部预印本含OpenReview；`interests`在all上应用当前兴趣和25/15/5；`default`等同all；`arxiv/biorxiv/medrxiv/researchsquare`为单平台。历史preset仍保留兼容，不是重复废代码。

## 维护与排错

`Executor`组织取文库→源检索→去重→排序/分组→已选论文摘要恢复→摘要生成→状态/投递。`retriever/`注册来源，`reranker/`注册embedding实现，`selection.py`统一分组和待投递配额，`abstracts.py`处理合法元数据恢复，`protocol.py`记录摘要来源/分组，`construct_email.py`生成单列HTML及纯文本，`state.py`保存投递，`scripts/prepare_workflow.py`处理有效策略，`scripts/workflow_state.py`持久化Git状态。

无结果先查源错误、窗口、关键词、ignore_path、阈值、文库及历史去重；不要通过清历史强行再发。模型失败看结构化摘要状态，不打印API响应体或secret。OpenReview403是访问限制，不是没有论文。Crossref未索引但出版社RSS正常时会标注覆盖限制；临时网络错误仍失败。测试工作流不载入真实投递凭据；只有手工运行发送工作流才发送真实邮件。

## CUSTOM_CONFIG/新加坡日预算与激活条件

仅约束本仓库受控请求，不是账户级扣费上限；其他客户端或旧版本不在保护范围内。所有公开摘要生成入口均要求BudgetRequests预约；金额来自合成配置中的 `llm.budget.daily_cny`，CUSTOM_CONFIG 优先于 preset。守卫不另行补入固定金额；缺失或无效金额禁止调用，false禁止调用而不是无限额。付费单位提取已移除，保留出版社单位元数据。

保留用户选择 `deepseek-ai/DeepSeek-V4-Flash`。已核对[公开API约定](https://api-docs.siliconflow.cn/docs/api/chat-completions-post)及[官方价格](https://www.siliconflow.cn/pricing)，使用 `https://api.siliconflow.cn/v1`（仅默认443端口）、`enable_thinking=false`、n=1、max_tokens=96，不传reasoning_effort/thinking_budget。SDK 自动重试关闭，仅按下文的有界策略重试临时错误，不自动补写被截断的输出。即使在低价时段也按输入3元/百万、输出9元/百万的峰值计费。价格记录核验于2026-10-01；建议于2026-10-08 UTC结束前复核。2026-10-09 UTC起若未复核，继续按最后复核费率运行并执行配置的每日估算记账额度，同时在邮件（HTML及纯文本）和日志醒目提醒；供应商涨价时实际费用可能超过估算及配置额度，不承诺实际费用的绝对硬上限。已知新价格时应更新费率并按预算减少请求。登录保护的用户控制台未被读取，不声称已验证其中的账户设置。

用户输入最多768 UTF-8字节，系统输入最多256字节，再保留128 framing tokens，共按1152输入token上界预约。原摘要完整保留在内部数据；只有没有有效AI总结时才在邮件中显示原摘要，仅模型上下文缩短。每次最坏预约 `(1152×3 + 96×9)/1,000,000 = ¥0.00432`，50篇合计¥0.216，低于当前 CUSTOM_CONFIG 的 ¥0.30。预算是基于已发布接口约定的工程上限，不是对供应商未来涨价或违规计费的绝对保证。

任何调用前须在共享paper-state分支的llm_budget.json原子提交当天完整额度。`llm.budget.timezone` 固定为已批准的 `Asia/Singapore`，金额仍来自 CUSTOM_CONFIG。普通fast-forward push实现并发比较交换；每次预约有独立 UUID，避免相同时间及运行元数据生成同一 Git 提交而使两个进程同时获批。不强推、不改投递记录。失败、账本损坏/丢失、当天已预约均不调用；崩溃/超时不返还额度，当天后续运行回退原摘要。运行中串行预扣及实际请求，在等待后及实际付费边界检查原预约的 UTC 起止时间，跨新加坡零点停止；UTC 零点不会换额度。模型ID、usage缺失/无效、token计数越界、reasoning_content/reasoning_tokens异常会终止所有排队调用。实际付费请求边界强制client.with_options(max_retries=0)，直接调用库函数也不能保留SDK默认重试。

首次读取有效 v1 账本时，在同一分支通过比较交换保存 v2 切换标记、来源提交与摘要，以及完整不变的旧 UTC 预约。每个旧预约的整个 UTC 日窗口都会阻止所有重叠的新加坡日期；旧 ¥0.20 预约也不补额度。旧写入先成功时重新读取并纳入；新标记先成功时旧写入被 Git 拒绝，旧代码重新读取 v2 后停止。已启动的旧守卫仍受保留的 UTC 窗口覆盖。不同日期命名空间不构成独立额度池。错误 schema、时间戳或完整性摘要均停止付费调用。回滚代码须保留 v2 兼容或停止付费，不得降级、清空账本或仅改配置回 UTC。

**现有仓库已有预算账本，不运行初始化命令。** 只有确认 `paper-state` 从未存在账本的新部署，才可在明确授权后初始化（会写账本，不调用模型）：

```bash
uv run --frozen python scripts/bootstrap_budget.py --initialize-new-ledger
```

初始化必须使用非浅克隆的完整历史，并核对远端账本。只允许该分支从未存在账本时初始化；若账本曾存在后丢失，必须恢复旧账本而非重建空额度。已有账本不运行初始化命令，由正常预算路径保留并迁移。新部署应先发布守卫代码并初始化，再在 CUSTOM_CONFIG 设置 llm.enabled=true；现有部署沿用原账本。endpoint/model/密钥值仍由现有secret和配置解析，不打印或改写凭据。付费验证必须占用同一新加坡日预约，不能另开不计费的“测试”路径。

### 选择摘要输入

```yaml
llm:
  input_mode: abstract # 默认：只用原摘要，不下载全文
# 若确需原文：改为 full_text
```

`full_text`当前复用arXiv合法HTML/PDF/TeX提取（HTML含表格），不承诺所有出版社全文可获取，不绕过付费墙。输入是完整的已提取文本，仍可能受文档解析质量影响。其他来源未提供全文时回退abstract；不会把截断前缀标为全文。全文超过当前768字节用户输入界限时回退，该小额度通常只能支持摘要。回退不额外调用模型，也不隐瞒原因：`summary_input_source`及`summary_input_fallback`保存在状态，并显示在邮件元信息。无原摘要可回退时不声称成功生成。无论输入选哪一种，有效AI总结都不会在邮件中重复附上原摘要；内部原摘要不删除。

## OpenReview 官方登录与访问限制

公开匿名请求可能收到403挑战或429限流；这不是“没有论文”。官方网页[挑战提示](https://github.com/openreview/openreview-web/blob/master/app/challenge/route.js)提供登录路径，本实现遵循[官方API v2客户端](https://github.com/openreview/openreview-py/blob/master/openreview/api/client.py)的 `/login` 与 Bearer 会话流程。

在仓库 **Settings → Secrets and variables → Actions → New repository secret** 分别设置 `OPENREVIEW_USERNAME`（账户登录邮箱/用户名）及 `OPENREVIEW_PASSWORD`（账户密码）；不要发到聊天、提交到代码或放入 CUSTOM_CONFIG。两者都缺省时继续匿名公开API；只填一个时明确报错。工作流仅向检索步骤注入这两个环境变量。登录失败只记录脱敏错误，令牌仅留在内存，不输出响应正文、不复制浏览器cookie、不绕过挑战、不换IP。启用MFA的账户可能无法无人值守登录，此时明确停止OpenReview检索，请通过官方支持解决账户认证，程序不会自动发送验证码或降低账户安全设置。

即使登录成功也只接收 `readers` 明确包含 `everyone` 且 `nonreaders` 缺省或为空列表的公开稿件；非空或格式异常的排除列表会拒绝该稿件，字段级排除列表同样使对应字段不可用，私有审稿内容不进入摘要/邮件。403应核查账号访问权限；429应等待下一次运行并遵守限流，登录不保证解除所有服务端限制。其他来源仍可投递并保存历史，随后任务以源不完整失败；不得将错误伪装成空结果。


凭据设置后可在 Actions 的 **Test** 工作流选择 `mode=openreview`，在待验证分支运行只读验证：仅注入上述OpenReview secrets，不注入邮件/LLM凭据，不访问投递历史、不调用模型。先验证登录和TMLR公开样本，通过后检查其余四个会场；总请求不超过22、每秒最多一次、不重试或跟随重定向。日志仅含阶段、HTTP状态及计数，不保存稿件或认证响应。每会场最多10条近期修改样本；匹配数为该有限样本通过原有日期/主题/关键词条件的结果，不代表当天完整配额。


`mode=openreview_full` 执行完整的有界覆盖诊断：所有已发现的当前/前一年会场及下一年ICLR，按投稿邀请分页，每页1000条、最多160次请求，逐阶段输出公开ACL、日期、关键词和主题计数。使用官方Notes分页参数；不以创建日期截断，避免漏掉早投稿而新公开的论文。生产检索也完整分页后应用原日期窗口，达到页数上限会明确报错。历史正例诊断只在内存中评估相同主题条件，不改变生产日期窗口，不输出论文或账号内容。



### 单篇 LLM 验证（不发送邮件）

手动运行 **Test → mode=llm**：先验证预算记录中的服务地址与模型，再通过只读模型列表检查凭据与模型可用性，然后为新加坡当日预留整个配置额度，只对一篇已投递、包含公开原始摘要的论文生成一句中文总结。使用生产摘要函数、96-token 输出限制、关闭思考和零自动重试；此显式验证最多一次付费尝试。日志仅保留状态与 token 用量，不输出论文内容、账户信息或密钥。此模式没有 SMTP 凭据，不写入投递历史，不发送或重发邮件。即使验证失败或仅用掉一小部分预算，当天额度也不会返还；同日后续任务保留原始摘要。模型列表返回401/403时在预留预算前停止，需要通过 GitHub Secrets 安全更新 `OPENAI_API_KEY`。

所有来源在排名和配额计算前排除明确封面标签（Inside/Outside/Front/Back/Supplementary Cover、Cover Image/Picture/Feature/Profile/Art、Frontispiece，标签后须为冒号、括号、分隔破折号或标题结束）；待投递与随机池同样排除，不删除历史。研究标题中正常提到cover或surface不会被排除。恢复后的摘要记录来源URL与状态，出版社拒绝普通HTTP访问时保留缺失状态和警告，不用标题相似的预印本或其他版本替代。

APS使用[官方公布的RSS输入](https://journals.aps.org/feeds)，与已禁用的RSS输出无关。Feed中的截断片段不冒充完整摘要。出版社网页受限时可以使用上述明确标注来源的DOI匹配索引或稿件摘要；只有标题相同、DOI缺失或版本不明的arXiv结果一律拒绝。官方[Harvest API](https://harvest.aps.org/docs/harvest-api)对部分内容要求APS授权；本程序不自动申请授权、不绕过401，也不使用账户/代理替换重试。

Research Square abstract recovery preserves the cited DOI version and verifies both DOI/version and title; the versionless identifier is used only for recommendation deduplication. See [verification and Zotero action proposal](RESEARCHSQUARE_ZOTERO.md).


## 可编辑的语义关键词兴趣

可在 Actions variable `CUSTOM_CONFIG` 的现有 YAML **合并**以下顶层块，不要用它覆盖整个配置。它只影响已通过来源筛选、去重和投递历史过滤的候选排序，不会扩大 arXiv 类别、OpenReview subject/keyword 条件或其他来源范围。以下示例使用代码默认权重 0.6/0.4；当前 CUSTOM_CONFIG 的实际权重为 0.4/0.6，切换策略时应保留现有权重：

```yaml
reranker:
  strategy: multi_interest_profile
interest_profile:
  keywords:
    - machine learning force field
    - interfacial water
    - electric double layer
    - droplet
    - proton transfer
    - enhanced sampling
  keyword_weight: 0.6
  zotero_weight: 0.4
```

也可在本地显式选择 `--config-name keyword_interests`；此 preset 继承 `interests`。定时工作流仍使用 `interests`，因此定时运行需要通过 CUSTOM_CONFIG 合并此块。启动完整应用会执行配置中的输出；验证配置时不要直接启动生产投递。

| 参数 | 默认值与规则 |
| --- | --- |
| `interest_profile.keywords` | `[]`，保持原来的 Zotero 排序；最多 100 个字符串，每个最多 250 字符。空白项忽略，Unicode NFKC、大小写、空白及连字符形式归一后去重。 |
| `interest_profile.keyword_weight` | `0.6`，关键词分数系数。必须为有限非负数字。 |
| `interest_profile.zotero_weight` | `0.4`，Zotero 分数系数。必须为有限非负数字；两项之和必须有限且大于零。6/4 与 0.6/0.4 等效。 |

关键词和候选摘要使用同一本地 embedding 模型；摘要缺失时使用标题并在邮件标记。模型可匹配近义表达，但不会生成或反复加权同义词列表，也不保证覆盖每个专业同义词。不同意思相近但写法不同的手工词仍是独立兴趣，建议避免重复罗列同一主题。

默认 `reranker.strategy=multi_interest_profile` 保留各兴趣方向：每篇参考按余弦相似度归入最匹配的一个关键词方向，达到 0.30 才参与画像；画像内按匹配程度选最多 24 篇，平分时优先较新的参考。画像内用原时间权重 `1/(1+log10(添加时间名次+1))` 重新归一。画像只按规范化 DOI（含既有 Research Square/ChemRxiv 版本身份规则）去重，保留最新条目；没有可靠 DOI 的参考保持独立。全库均值仍包含全部参考，与原算法一致。

有效支持 `n_eff=1/sum(画像内归一权重²)`；少于 5 篇或 `n_eff<3` 时，该方向回退全库均值。支持充足时，用 `r=n_eff/(n_eff+5)` 收缩：`方向质心 = r×方向加权质心 + (1-r)×全库加权质心`。这些质心使用单位参考向量的加权平均，**不再归一化质心**；因此低支持或分散方向不会仅因向量被放大而取得高分。

候选在每个方向计算 `w_keyword×关键词余弦 + w_zotero×方向画像余弦`，取最大的**同一方向联合分数**。它不是两项分别取最大后相加，也不是先按全库截断候选。方向数量随去重后的配置关键词变化，不限于示例中的六个。最多 24 篇的支持上限减少大主题凭条目数支配画像，但全库收缩和缺失方向仍可能保留主题偏差；这只是排序策略，不强制主题配额，也不进行多样性重排。

显式 `reranker.strategy=legacy_mean` 可回退：关键词取等权余弦均值，Zotero 取原添加时间加权均值。两种策略均先计算内部旧制 `s=10×联合余弦`，在该有符号域施加原缺摘要惩罚，再通过 `5×s+50` 映射到 0–100。有效权重归一为 1，不按当天候选做 min-max/百分位归一化；0/50/100 分对应原 -10/0/10，不代表概率或准确率。

| `reranker.multi_interest_profile` 参数 | 默认值与规则 |
| --- | --- |
| `assignment_min_cosine` | `0.30`；有限数字，范围 [-1, 1]。参考必须达到此归属阈值。 |
| `max_references_per_direction` | `24`；正整数，每个方向的参考上限。 |
| `min_references_per_direction` | `5`；正整数，不能超过上限；不足时回退全库均值。 |
| `min_effective_support` | `3.0`；有限正数字；有效支持不足时回退全库均值。 |
| `shrinkage` | `5.0`；有限非负数字；越大越接近全库均值，0 为充分支持方向的原始加权质心。 |

仅切换策略时，在原 CUSTOM_CONFIG 的 `reranker` mapping 内修改 `strategy: legacy_mean` 或 `strategy: multi_interest_profile`，保留其他字段。`legacy` 配置入口显式使用旧策略，其他普通 preset 默认使用画像。画像方案要求 `reranker.experiments` 保持默认值；与 query、文本拼接或旧聚合实验冲突时明确报错，不静默混合未验证的算法。

某一参考集为空时，它的有效权重变为 0，另一个非空且正权重的参考集使用权重 1；显式零权重不会被恢复。无可用正权重参考集会报错。Zotero 读取/鉴权失败仍报错，不视为空库；即使 Zotero 权重为 0，仍读取库以排除已有论文。画像策略无库时取最佳关键词，无关键词时保持原 Zotero 时间衰减排序；关键词评分权重为 0 时，配置关键词仍用于方向归属，只对画像库相似度评分。

修改关键词只新增所需文本向量，修改权重、策略或画像参数立即重新计算分数并复用向量；本地持久缓存继续按模型、不可变 revision、prompt/encode 参数和运行环境隔离。额外的参考×关键词比较复用第一次已编码的文本，不重新编码，也不创建参考×参考矩阵。无需新增付费 API 或 LLM；若另行选择既有 `api` reranker，仍遵循其外部服务成本。邮件在顶部说明实际策略与有效权重，画像卡片显示匹配方向和低支持回退；历史待发论文保留原策略，混合批次不把旧分数说成新算法分数。

`executor.min_score` 在缺摘要调整和量纲转换后筛选，旧阈值会先映射到新量纲。期刊/预印本按 CUSTOM_CONFIG 配额和排序选择；random 从剩余合格未见候选中无放回抽取。不足时保留缺额，不跨组补齐。其他来源过滤、历史排除和预算逻辑不变。


## 摘要恢复、缺失原因与排序时机

摘要恢复逐条保存 `abstract_recovery_attempts`（提供方及结果），邮件中的缺摘要卡片显示原因：元数据没有摘要、DOI/标题不符、404、403/429、超时、出版社没有摘要或额度耗尽。失败不会自动隐藏论文，不以 RSS 截断文本、新闻导语或另一版本替代原摘要。历史记录没有新字段时仍可正常读取。

出版社恢复额度只由实际进入查找的论文消耗。一个出版社已返回访问拒绝后，后续同站跳过不会占用其他出版社的额度；跨论文共享拒绝状态，不重试或换身份。公开 Nature、APS PRL 和 ACS 页面仅接受经 DOI 验证的摘要区或 `citation_abstract`，拒绝截断摘要，不处理付费正文。元数据恢复使用零自动重试连接；来源原始检索的重试策略不变。

默认 `abstracts.pre_rank_max_papers: 0` 保留原有“先选推荐，再为选中论文恢复摘要”的开销模式。因此后补摘要不一定参与过入选决策。补回后会重新计算该论文的正常展示分数，依据改为 `abstract`，保留 `selection_score` 并在卡片注明入选分数与补全后的变化；不重新选论文或重抽随机组。

需要让更多恢复摘要参与排序时，可以在单独审核后配置：

```yaml
abstracts:
  pre_rank_max_papers: 25
  pre_rank_seconds: 90
```

`pre_rank_max_papers` 默认为 0，允许 0 到现有 `max_papers`（默认 50）；先按当前排名在期刊和预印本中交替选取缺摘要候选，最多补全指定篇数，然后对候选重新计算分数，最后执行分数门槛、25/15/5 配额及随机无放回抽样。新增摘要需要新向量，未变化文本继续使用已有 embedding 缓存，不调用 LLM 排序、不改模型或兴趣权重。该有限候选集可能漏掉初始标题排名较低的论文，不保证等同全量摘要排名。

`pre_rank_seconds` 默认为 90，允许整数 1–600；这是网络查找的准入截止时间，不是整个阶段的硬墙钟上限：在途请求和模型编码仍需完成。新请求的连接/读取超时会缩短到剩余时间。预排序与选中论文补全共享 `max_papers`、出版社及 APS 替代来源额度、已尝试论文和拒绝状态，避免重复请求；剩余额度用于最终选中论文。截止前已尝试但没完成全部来源的论文不在同次运行重新尝试，选中论文后补到的摘要会重算该论文分数并用于展示/总结，但不追溯改变已冻结的入选集合。此选项默认关闭，启用会增加查找与一次新文本编码成本；不是额外邮件或新的定时任务。

已验证出版社页面没有独立摘要时明确标记为“未提供独立摘要”，与访问拒绝/请求失败区分。元数据明确标为 correction/erratum 且摘要为空时保留此类型说明，不拿被更正论文摘要替代；不凭标题或新闻导语推定存在原摘要。


## 0–100 分数、缺摘要惩罚与旧数据兼容

所有 `score`、`raw_score`、`selection_score`、`keyword_score`、`zotero_score` 都使用 0–100 量纲；HTML、纯文本、RSS 与历史一致。映射为 `display = 5 × signed + 50`，对合法旧 [-10,10] 分数严格单调，保留排序和同分关系。分数是相似度排序指标，不是匹配概率、准确率或人工质量标签。

缺摘要时先在原有符号域 s 上应用 `reranker.missing_abstract_factor`（默认0.8，范围0–1）：正值乘以因子，负值除以因子且最低 -10；因子0直接取 -10，因子1不调整；之后才映射。不能直接拿新制分数乘因子。原摘要恢复后从相似度重新计算，不叠加旧惩罚。

| 原有符号分数 | 未惩罚显示分 | 缺摘要后显示分（0.8） |
|---|---|---|
| 8 | 90 | 82 |
| 5 | 75 | 70 |
| 0 | 50 | 50 |
| -5 | 25 | 18.75 |
| -10 | 0 | 0 |

旧配置兼容：`executor.min_score_scale: legacy` 为默认，因此旧 `min_score: -10` 仍表示全范围，旧 `6.5` 仍筛选相同论文（新制82.5）。新制阈值需明确写：

```yaml
executor:
  min_score_scale: "0_100"
  min_score: 82.5
```

阈值、排序和配额选择完成后，`selection_score` 固定记录入选分；后续摘要恢复可改变展示分，但不重抽随机项、不更换已入选成员。`raw_score` 表示未惩罚的融合分（同样已映射），不是内部有符号值。

每篇新历史记录带 `score_schema: relevance_0_100_v1`。缺少标记的旧记录按原制读取，五个分数字段只转换一次；新制记录再次读取不重复映射。容器仍为 version 1，论文身份、added 时间、渠道成功标记与待发送记录保留；读取不立即写文件，正常状态保存时才持久化转换。非有限数值和未知 schema 明确报错，不能清空历史重试。向量缓存不存推荐分数，因此量纲变化无需失效或重算 embedding；旧离线评测文件则应保留原量纲说明，不能把历史差值当新制分数。

## ChemRxiv 预印本来源

ChemRxiv 使用 [Crossref 官方元数据 API](https://api.crossref.org/works?filter=prefix:10.26434,type:posted-content&rows=1)。不抓取 ChemRxiv 页面或 RSS，不绕过其 API 的 403。Crossref 入库可能延迟或缺少字段，不能视为 ChemRxiv 网站的完整实时索引。默认查询全化学范围，交由现有本地 embedding 兴趣排序，不额外强制关键词筛选。

该来源只接受 DOI 前缀 `10.26434`、`chemrxiv` DOI 命名、Crossref member `316`、publisher `American Chemical Society (ACS)`、类型 `posted-content/preprint`，且主资源指向 `chemrxiv.org` 的记录。未知注册者/命名会被排除，来源变更需要重新核验。官方分类接口当前访问被拒绝；不能把自定义关键词当成官方分类 ID，也不承诺官方 subject taxonomy 筛选。

现有定时配置默认关闭 ChemRxiv。发布此代码后，如需启用，把以下块**合并**入原 `CUSTOM_CONFIG` 的 `preprint_interests`，保留其他配置：

```yaml
preprint_interests:
  chemrxiv:
    enabled: true
    categories: ["*"]
    keywords: []
```

`--config-name chemrxiv` 是继承现有 `interests` 的启用预设；`all` / `preprints` 的可选来源也包含 ChemRxiv，最终是否启用仍受 interests 开关控制。ChemRxiv 不是独占运行其他来源的模式。

| 参数 | 默认值与作用 |
| --- | --- |
| `preprint_interests.chemrxiv.enabled` | `interests` 中 `false`；显式启用后加入已有预印本候选池。定时任务保留此独立覆盖。 |
| `preprint_interests.chemrxiv.categories` | 继承 `source.chemrxiv.category: ["*"]`；仅支持全范围 `*`，其他值明确报错。 |
| `preprint_interests.chemrxiv.keywords` | `[]`，不作硬筛选；非空时按已有平台规则，对标题或摘要进行大小写不敏感的短语 OR 匹配（短语不跨字段）。不匹配即排除，区别于 `interest_profile.keywords` 的 embedding 语义排序。 |
| `source.chemrxiv.window_days` | `1`，整数 1–90；筛选 UTC 今日减 N 天至今日，两个日期边界均包含。元数据只有日精度，因此 1 表示昨天和今天两个日期，不是滚动 24 小时。工作流的 `WINDOW_DAYS` 同样作用于该来源。 |
| `source.chemrxiv.page_size` | `50`，整数 1–100，每页记录数。 |
| `source.chemrxiv.max_pages` | `20`，整数 1–100；超过上限视为不完整检索并报错，丢弃该来源部分结果，不伪装检索成功。 |
| `source.chemrxiv.mailto` | `null`；可选 Crossref 联系邮箱（会传给 Crossref User-Agent），不要填 API 密钥。 |

例如，希望只保留字面命中分子动力学或材料主题的候选时，可设 `keywords: [molecular dynamics, materials]`；这会收窄召回范围，不保证覆盖同义词。默认空列表更适合让现有兴趣模型决定相关性。ChemRxiv 沿用实际配置中的关键词/Zotero 融合权重、缺摘要系数、恢复限额及投递设置，不另设排序模型或 LLM。

API 在服务端按 `posted` 日期过滤，按 `indexed` 排序以兼容 cursor；客户端逐页读取，不因遇到窗口外日期提前停止，并再次核验完整发布日期。HTTP 401/403/429 不重试；重定向、重复页、无有效后续游标、畸形响应和页数上限均明确报错。单页上限 2 MB，连接/读取超时 5/20 秒，沿用 Crossref 串行限速。结果不是跨请求事务快照，服务端并发更新仍可能影响边界覆盖。

ChemRxiv 计入现有**预印本 15** 配额；期刊 25、剩余合格未见候选无放回 random 5 不变，三组不重叠。候选不足时留缺额，不重复、不放宽过滤、不从其他组强行补足。来源失败保留明确失败状态，其他来源继续按原流程处理。

同一家族的 ChemRxiv 版本共享推荐身份，当前检索中优先最新版本，但摘要查询始终使用精确版本 DOI。Crossref 明确提供 `is-preprint-of` / `has-preprint` DOI 关系时，可跨来源、Zotero 库和投递历史去重，并优先保留发表记录；不同版本或预印本/发表版之间不移植摘要。缺少 DOI 关系时无法保证识别改题发表的稿件，不会仅凭相似标题声称它们相同。历史新增可选 `related_dois`，旧记录兼容读取。

原始摘要优先使用该版本的 Crossref deposit，缺失时走既有合法元数据恢复；不拼接正文充当摘要、不访问受限全文。仍缺摘要时按现有 `missing_abstract_factor`（默认 0.8）降分。此来源未实现全文提取，已有全文总结模式会按原规则回退到摘要。

## arXiv 临时故障与启动时间

### 恢复已保存的待发批次

官方 arXiv API 可能返回 `http://arxiv.org/abs/...`。仅对准确匹配官方域名、论文路径及有效 ID 格式、且无认证信息/显式端口/查询/片段的 HTTP 链接升级为 HTTPS。新抓取、历史加载和 D1 引用校验共用该规则；一般 HTTPS-only 规则不放宽，`paper_id`、确认链接身份、摘要、分类、分数和配额不改变。retriever 的原始对象缓存也使用升级后的 URL，以保留后续全文补全查找。

`Replay authorized saved email batch` 仅服务本次明确授权的运行 `39507147167`：固定快照 `695e373bccdbe4ad7dd8e7219781686220926e2d`、50 篇及身份摘要，触发分支固定为 `delivery/replay-39507147167`。日常任务不会触发它，也不应为恢复而盲目重跑完整日常任务。脚本在网络操作前验证当前待发批次与原快照的内容和时间一致；全部已送达则无操作，部分已送达或内容改变则停止。

恢复流程不抓取、不重排、不生成摘要、不调用模型、不访问 Zotero，沿用每日任务的 `RECEIVER` secret 和 SMTP/D1 同步配置，不支持收件人覆盖。先校验全部50条引用、完成原 D1 写入和精确读回，再在原 `paper-state` 分支的独立 `delivery_replays.json` 中提交 SMTP intent，然后仅调用一次 SMTP。成功后将邮件标记及成功回执一起提交；仅对已知并发 CAS 冲突/本地回执 I/O 重试，不重试 SMTP，不回退或改写预算账本。流程使用与日常任务相同的 `daily-papers` 并发组。

若出现 `smtp_started` 而未完成投递标记，发送结果可能不确定：先查对应运行的 SMTP 日志，禁止直接重跑或重新推送分支导致重复发信。脚本会拒绝已有尝试的未完成批次。SMTP 接受仍不代表最终收件箱到达。

定时启动时间为 UTC **19:17**（北京时间次日 **03:17**），GitHub 调度可能延迟，不能保证准点发信。arXiv API 每页 100 条，正常翻页至少间隔 3 秒；连接/读取超时分别为 5/20 秒。对连接超时、连接失败及 HTTP 500/502/503/504，单页最多额外重试 3 次，等待 15/30/60 秒；关闭 SDK 的第二层重试。服务端 Retry-After 需要更长等待时遵守其要求，但超过 60 秒或格式无效就停止本次来源检索，不提前重试。401/403/429、重定向和代理拒绝不重试，不更换域名绕过。

定时任务的 arXiv 窗口至少为7天：论文按提交日期筛选，但公开和索引可能晚于提交，滚动24小时还可能因调度延迟出现漏扫间隙。该策略仅覆盖定时任务中 arXiv 的过短/null窗口，保留分类、关键词、交叉分类策略、更长窗口、其他来源、配额和投递历史。无需读取或修改 secrets；可另行清理 Actions variable `CUSTOM_CONFIG` 中旧的 `source.arxiv.window_days: 1`，但不是代码修复生效的前置条件。

HTTP 200 仍须通过完整性检查：有效 Atom/XML，非负的 totalResults/startIndex/itemsPerPage，与请求一致的起始位置和数量边界，以及条目的标题、摘要、作者、日期、分类、ID和链接。非空短页允许继续翻页；totalResults=0且无条目是合法成功。总数为正却空页、总数小于条目数、跨页总数变化、重复条目或SDK跳过记录均作为来源失败，丢弃该来源部分结果。诊断日志分别记录HTTP状态、查询窗口、页数量、API条目数、主分类保留数、转换数及关键词保留数；缺PDF链接单独计数，不假定摘要等于正文。

定时、非debug且启用state的成功检索会保存独立的 `data/arxiv_health.json`，通过原 `paper-state` 分支恢复和保存。文件仅含计数、UTC日期和范围摘要哈希，不含论文正文、关键词明文或凭据。相同范围连续三个不同UTC日零候选时警告；同日多次运行只计一天，同日曾有候选不被后续空结果覆盖，非空结果、日期中断或范围变化重置连续计数。失败/不完整检索和手动检查不推进计数；历史损坏/不可写时保留原文件并提示计数未知，不阻断其他来源投递。首次上线从新的观测开始，不根据邮件历史伪造过去的零结果计数。

持续故障或中途翻页失败仍作为来源失败报告，不交付不完整的 arXiv 候选集。其他来源可以正常投递并保存历史，因此 Actions 显示失败不等于邮件没发出；先检查 SMTP 接受日志及已保存投递状态，不要为了验证而重复运行发信。

邮件标题 `Daily Papers YYYY/MM/DD` 的日期按发送时的 `Asia/Singapore`（UTC+8）计算，不依赖 runner 系统时区。例如 UTC 10 月 4 日 19:17 对应标题日期 `2026/10/05`。预算也按新加坡日历日，但锚定首次预约的日期，发送时跨零点不会重获额度。正文中的论文发表日期保持来源日期；检索窗口、投递历史及 cron 不受此显示规则影响。

## 本地 embedding 缓存与可选 ONNX 对照

当前 Jina 模型固定为 revision `ac5d898c8d382b17167c33e5c8af644a3519b47d`；同一模型显式 `revision: null` 也回退到此固定值，其他模型不套用 Jina revision。默认仍使用 PyTorch、原有 document prompt 和原评分公式。ONNX 只是一条可切换实验路径，不能因量化或格式相同就假设排序相同。

```yaml
reranker:
  local:
    revision: ac5d898c8d382b17167c33e5c8af644a3519b47d
    backend: torch # torch / onnx_fp32 / onnx_int8
    cache_dir: null # 可设本机受控私有目录；默认关闭持久化
    onnx_directory: null # 已校验官方文件所在目录；运行时不自动下载 ONNX
    onnx_fallback: true # 可选 runtime/模型加载/推理失败时重新用 PyTorch 计算
```

持久缓存保存到 `cache_dir/private/<namespace>`。即使文本来自公开论文，推荐候选已经经过私人文库、历史或关键词筛选，其集合成员关系也属于私有信息；候选、文库、关键词的混合向量**从不归类为公开缓存**。本地明文目录没有上传接口；工作流只允许上传经 AES-256-GCM 整包加密后的单个文件，不能上传整个明文目录。文本哈希不构成脱敏，SHA 校验和只发现损坏，不抵御有写权限者投毒；只从受控、可信的本地目录读取。公共语料缓存若将来增加，必须在任何私人筛选之前独立生成，本次未接入。

命名空间包括固定模型版本、后端、官方 ONNX 文件 SHA、runtime、prompt/encode 参数、最大长度、维度、dtype、设备、线程与库版本；文本改变或这些身份改变时缓存失效，兴趣权重改变不会导致重编码。读取禁止 pickle，检查压缩/展开大小、维度、dtype、有限非零向量和校验和；损坏条目按缺失重算。文件使用私有权限和原子写入，磁盘不可用则继续使用内存缓存。日志仅输出模型准备、内存/磁盘命中、缺失/损坏数量、查找、编码和相似度耗时，不输出缓存文本。

**加密跨 Actions 缓存只有代码发布且专用密钥已配置后才可工作，未实际运行不能声称命中。** 用户在仓库 Actions Secrets 自行添加 `EMBEDDING_CACHE_KEY`：32 个随机字节的标准 Base64（44 字符，通常以 `=` 结尾）。不要把密钥发聊天或提交代码。程序只在可信仓库 main 的 schedule/workflow_dispatch 中使用此专用密钥；PR 不注入。缺失或格式无效时继续推荐并明确提示未启用。没有创建凭据、服务或自托管 runner。

可选 ONNX 需要 `uv sync --frozen --extra onnx`。下载命令会先检查空间，仅访问官方固定 revision 并核对图文件与 external-data 文件 SHA：

```bash
uv run --frozen --extra onnx python scripts/download_jina_onnx.py --backend onnx_fp32 --directory /private/models/jina
# 量化版本单独选择 onnx_int8，不会自动下载两组。
# onnx_directory 配置为 /private/models/jina/onnx；同 revision 的 tokenizer 须已在本地缓存。
```

FP32 官方文件约 849 MB，量化文件约 247 MB。可选后端固定 CPU provider；其他模型/版本、不支持的 encode 参数或缺文件会报错，开启 fallback 时回退到 PyTorch。两种后端的向量命名空间隔离，回退不会把部分 ONNX 向量混入 PyTorch 缓存。默认不切换后端，不改变配置配额、实际兴趣权重、缺摘要系数、预算、时间窗口或邮件行为。

加密包内包含所有向量文件名和 manifest；每次使用新 96-bit nonce，AES-256-GCM 认证绑定仓库/格式身份。外部 cache key 仅含固定格式版本、runner OS、run ID/attempt，不包含私人配置或文本哈希。认证通过后才检查/解包：最多 128 MiB、20000 个向量、单文件 1 MiB，拒绝路径穿越、重复成员、压缩膨胀、错误校验和和非法向量。模型/提示身份仍通过加密包内的缓存命名空间匹配。校验失败、密钥轮换或格式变化时安全重算；只在封包成功后保存新密文。密文大小和更新时间仍可见。包不可供不可信 PR 写入可信明文缓存；无正确 key 的伪造包无法通过认证。实际评分步骤只接收临时明文目录路径，不注入加密密钥。

加密快照最多保留 20,000 个最近使用的向量，并为归档索引和内部清单预留 16 MiB；总包仍限 128 MiB。磁盘命中会刷新本地使用时间，时间戳随清单一起加密并在还原后保留。超出快照容量的旧向量不上传，下次需要时重算；不会删除本地源缓存、交付历史或预算记录。本地手动指定的缓存目录由用户管理磁盘生命周期。

离线后端对照（模型与 tokenizer 必须已缓存，不自动下载）：

```bash
uv run --frozen --extra onnx python scripts/benchmark_embeddings.py --onnx-directory /private/models/jina/onnx --output /private/results/backends.json
```

该对照使用固定合成文本、独立子进程和相同输入/提示，报告缓存冷暖耗时、cosine 差异和 top-10 重叠；不含人工质量标签，不能据此宣称推荐准确性提升。默认后端仍为 PyTorch。

### 手动验证加密缓存（不投递）

Actions 的 **Validate encrypted cache (synthetic only)** 仅支持在本仓库 `main` 手动运行，无定时触发。两个独立 runner 依次执行：首个用 3 个固定合成向量建立冷缓存并上传 AES-GCM 密文；第二个按完全相同的 key 还原，断言 `disk_hits=3`、`misses=0`、`memory_hits=0` 且向量逐项一致。

此验证只向封装/还原步骤提供已有 `EMBEDDING_CACHE_KEY`，不注入 Zotero、SMTP 或 LLM 凭据，不运行应用主程序，也不修改投递历史或预算。专用 `embedding-validation-v1-` 缓存前缀与每日生产缓存隔离；仅缓存密文文件，不上传明文向量。日志中的命中计数和密文摘要可核对两阶段结果，但不能据此声称真实每日任务已提速。

脚本接口为 `scripts/validate_encrypted_cache.py {save,restore} --package PATH`，仅接受可信仓库 `main` 的 `workflow_dispatch` 环境；`PATH` 是密文包位置，保存时拒绝覆盖已有文件。工作流固定使用 runner 临时目录，无需用户输入参数。

### 可选推荐算法实验（默认关闭）

以下是原均值算法的单因素消融，需要显式选择 `legacy_mean`。`reranker.experiments` 的默认值保留原均值算法；实际使用的兴趣权重仍由 `interest_profile` 决定，不被实验开关覆盖。可以一次只改一个参数进行对照：

```yaml
reranker:
  strategy: legacy_mean
  experiments:
    keyword_prompt: document
    text_mode: abstract
    deduplicate_corpus: false
    corpus_aggregation: mean
    keyword_aggregation: mean
    top_k: 5
    temperature: 0.1
```

| 参数 | 说明与实验值 |
|---|---|
| `keyword_prompt` | 默认 `document` 沿用原 encode 配置。`query` 只把兴趣关键词编码为 query，候选和语料使用 document；要求本地模型具备同名 prompts，不支持的 API 后端报错。显式 `encode_kwargs.prompt` 与此实验冲突时拒绝运行。 |
| `text_mode` | `abstract` 使用摘要、缺失时回退标题；`title_abstract` 用 `标题 + 两个换行 + 摘要` 编码候选和语料。缺失摘要仍只用标题并应用原 0.8 惩罚，绝不假装有摘要。 |
| `deduplicate_corpus` | `false` 保留全部参考记录。`true` 仅合并完全相同的规范化 DOI，保留添加日期最新的一条；无 DOI、不同 DOI、只有相同标题的记录不合并，不跨论文版本猜测身份。 |
| `corpus_aggregation` | `mean` 为原时间衰减加权均值；`top_k` 对每个候选只取最高的 k 个相似度，并在这些参考记录上重新归一化原时间权重；`softmax` 权重为 `原时间权重 × exp((相似度−最大值)/temperature)` 后归一化。 |
| `keyword_aggregation` | 同样支持 `mean/top_k/softmax`，但关键词先验为均匀权重，与语料开关独立。 |
| `top_k` | 正整数，默认 5，超过可用参考数时自动取可用数。相同分数保持原参考顺序。 |
| `temperature` | 有限数且至少 0.000001，默认 0.1；越小越偏向最相似的参考。只影响 softmax。 |

例如仅比较关键词 query：`reranker.experiments.keyword_prompt=query`。模型/提示角色/文本改变会使用独立缓存；只改聚合、权重或惩罚会复用向量。ONNX query 推理若回退到 PyTorch，会同时重新取得 PyTorch document 向量，避免混合后端。实验不会改变来源过滤、去重历史、配置配额、随机抽样、邮件和预算。

### 私有本地盲评与成本对照

`scripts/evaluate_ranking.py` 只进行本地模型排序，不调用 Zotero、SMTP、LLM 或网络下载。先缓存指定版本的现有 Jina 模型。输入 JSON 包含 **200–300 条固定且身份唯一的候选**、参考语料和关键词，保存在访问受限的本地目录，不提交、不上传 Library、Actions artifacts 或公共缓存。不要把真实收藏夹路径、账号或密钥放入输入。

输入结构如下（示例仅说明结构，需填入足量真实候选）：

```json
{
  "corpus_role": "local evaluation corpus; describe sampling limitations",
  "keywords": ["interfacial water", "proton transfer"],
  "candidates": [{"source":"journals","title":"Paper title","abstract":"Original abstract","authors":[],"url":"https://example.org/paper","doi":"10.1234/example","publication_kind":"journal"}],
  "corpus": [{"title":"Reference title","abstract":"Reference abstract","doi":"10.1234/reference","added_date":"2026-01-01T00:00:00"}]
}
```

```bash
# Use a private local directory; never publish these generated files.
uv run --frozen python scripts/evaluate_ranking.py prepare --sample /private/sample.json --output /private/review
uv run --frozen python scripts/evaluate_ranking.py compare --sample /private/sample.json --output /private/review
# Fill every relevance_0_to_3 cell in review.csv, without looking at rankings.json.
uv run --frozen python scripts/evaluate_ranking.py evaluate --output /private/review
```

- `prepare`：默认 `--seed=20261004` 随机打乱候选，输出隐藏方法/分数的 `review.csv` 和仅本地保留的 `private-index.json`；已存在文件拒绝覆盖，CSV 文本防公式注入。
- 人工标签：0 不相关、1 边缘相关、2 有用、3 高度相关；先固定准则再标注，不能根据方法名称或排序调整标签。缺失、重复或样本不一致会阻止评估，不把空白当成负例。
- `compare`：固定 PyTorch FP32、4 CPU 线程、batch 16、0.4 关键词/0.6 语料、0.8 缺摘要因子。运行 baseline 及七个单因素对照；冷启动单列，各实验计时在 baseline 预热后进行，新增提示/文本可能仍需编码，因此不是独立冷启动竞赛。输出本地 `comparison.json` 成本/排名变化，以及含身份的私有 `rankings.json`。实际生产权重不被修改。
- `evaluate`：人工标签全部完成后，按固定离线评测方案输出期刊 top25 和预印本 top15 的 nDCG 与 Precision（这是原盲评基线，与现行 CUSTOM_CONFIG 的25/20/5生产配额不同）（≥2 视为相关）；组内不足时报告实际数，随机 5 篇不参与相关性排名指标。离线工具仅比较排序，不模拟历史过滤/每日时间窗/随机抽样，不据此宣称每日发送表现。人工标签与样本选择偏差都需保留在结论中。
- 参考库重复率低时，DOI 去重可能没有效果；top-k/softmax 的排名变化不等于质量提升。没有足够人工标签时不选择“优胜算法”。BGE/SPECTER 不在本次下载或默认替换范围内。

### 摘要请求稳定性与预算日期

`llm.request` 默认每次 SDK 请求超时 30 秒、每篇最多 2 次尝试、退避 1 秒、最大等待 5 秒；仅连接失败、超时和指定 5xx/408 可重试。每次尝试都在发送前扣除一个完整的最坏费用预留，超时的未知费用也不退回。SDK 自动重试始终关闭；连续 3 次临时失败会停止本次运行的后续调用。401/403/429、请求被拒绝、模型不可用和计费验证异常也会停止后续调用，不切换供应商、模型或凭据。

`Retry-After` 的数字和 HTTP 日期适用于本次运行的后续请求；超过等待上限或无法解析时停止本次调用，不提前重试。显式禁止重试的响应也会停止调用。固定的 `n=1`、`max_tokens=96`、`enable_thinking=false` 保持不变，生产请求还必须匹配预留时的供应商与模型。截断、空文本、错误语言、列表/多句或异常响应使用原始摘要，不标记为 AI 成功。日志和邮件显示成功比例、尝试次数或人类可读的失败原因；状态保存原因和预算日期，SMTP 恢复投递复用已保存的摘要，不重新收费。显式的一次摘要验证仍只有一次付费尝试。

预算采用 **Asia/Singapore** 日历日，默认金额为 ¥0.30，CUSTOM_CONFIG 的显式金额优先。同日延迟调度、重跑或验证只能预留一次；旧 UTC 预约仍阻止重叠的新加坡日期。已预约标记 `daily_budget_reserved`，旧窗口重叠标记 `legacy_budget_overlap`。实际结果基于现有账本，不清零、不补额度、不预留未来日或复用崩溃运行额度。
