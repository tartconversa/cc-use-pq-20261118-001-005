# Meridian 区域媒体证据分析服务

本服务供研究团队对本地提供的媒体观察记录进行地区、议题和受众比较。系统不抓取网页、不自动推断报道倾向，也不声称合成演示能代表真实群体。

来源登记绑定地区、媒体和允许词表；观察记录按来源和外部编号保留修订。两名研究员独立编码，分歧由第三人裁定。按地区、故事、议题和受众去重，报告显示分母、排除原因和原始证据数。

## 非互斥多议题编码

一条稿件可同时涉及科技与文化等多个议题，系统不要求整条观察硬选单一议题，也不靠复制记录制造多议题：

- 每名审核员按「观察 × 议题」分别提交 decision（同一审核员可对同一观察的不同议题各编码一次）；每个议题独立经历待审、共识、争议和裁定。科技部分达成共识而文化部分仍有争议时，只有科技贡献进入确定性趋势，整条稿件不会被排除，文化争议保留在审核队列中。
- 裁定按议题立案（`case_key` 含议题），第三人可只裁定争议议题，已共识议题不受影响。
- 报告始终区分两种分母：故事级 `unique_story_count`（区域内 `(region, story_key)` 去重，多议题故事只算一个，不虚增来源采集量）与议题级 `topic_contribution_count`（每个 `(story, topic)` 贡献一行，各带倾向、受众和证据）；`multi_topic_story_share` 给出非互斥议题占比，倾向占比以议题贡献为分母并在 `stance_share_denominator` 标注。
- 跨来源转载同一故事时，各来源的议题覆盖与倾向分别计入对应贡献行；更正或撤回作用于观察头部修订，旧修订与旧编码保留在血缘中，当前有效贡献随之改变。
- 发布前质量门对未解决的议题争议（`disputed_topic_count`）放行拦截；仅等待第二人编码的议题（`awaiting_topic_count`）不拦截已确认贡献，但在 `/reports/readiness` 以 `open_topic_reviews` 警告暴露。冻结报告封存当时的完整分母与逐项证据，`/releases/{id}/compare` 同时给出故事数、议题贡献数和多议题占比的前后变化，用于判断占比变化究竟来自报道变化还是多议题归属方式。
- 每项议题贡献在 `stories[].evidence` 与导出包 `evidence[].topic_units` 中携带原稿坐标（origin/external_id/revision/hash）、审核员 decision id 与裁定依据，可逐项追溯。

使用 `python -m meridian.api --db meridian.sqlite3` 启动 FastAPI。主要接口包括 `POST /sources`、`POST /observations`、`POST /decisions`、`POST /adjudications`、`POST /batches`、`POST /reports` 和 `POST /releases`。采集窗口使用 `/frames`，导出使用 `/reports/export` 或 `/releases/{release_id}/export`。

运行 `python -m unittest discover -s tests -v`、`python -m compileall -q meridian` 和 `python -m meridian.demo`。数据库使用 SQLite；测试和演示使用临时本地数据库。
