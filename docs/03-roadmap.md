# TechRadar — 实施路线与验收

> v1.0 · 2026-08-18

## W1 — P0 闭环（目标：Day5 起每天 08:00 收到可用日报）

| Day | 交付 | 验收 |
|---|---|---|
| D1 | docker-compose 起 Postgres+pgvector；models + alembic 初始迁移；BaseFetcher（限流/退避/健康记录）；hn_algolia fetcher | `techradar fetch hn` 入库 ≥ 50 条，source_health 有记录 |
| D2 | github_search fetcher（近 7 天新建按 star）+ rss_generic；settings/subscriptions/taxonomy/profile 配置加载 | 三源都能入库；同一 URL 不重复 |
| D3 | canonical_key + dedupe（items/item_sources 分层）；三时间字段；snapshot job；状态机；expire job | HN 指向的 GitHub repo 与 github 源合并成一个 item、两个 item_sources |
| D4 | rule_filter → queued；score（P0 公式 + breakdown + reasons）；preferences 表与 pref_multiplier | `techradar score` 输出 Top20，每条有 reasons |
| D5 | services 层（search/feedback/inbox/health/usage）+ MCP server 骨架（stdio）；llm/client（结构化输出 + usage + 预算）；enrich_batch（Haiku）；digest_daily（Top8+折叠10+告警+花费）；Telegram 发送 + inline 按钮 → feedback + feedback_features；APScheduler 常驻 | 08:00 收到日报；点 ⭐/🙈 有效；llm_usage 有记录 |

## W2 — 观察与补齐

- 跑满 7 天，记录：推送数 / 点击 / 收藏 / 忽略比例 / 各源条数 / 花费
- 新源：arxiv、gh_releases Atom、Hugging Face papers、RSSHub 自建（掘金 / V2EX）
- Batches API 离线 enrich
- Telegram `/add <url>`、`/mute`
- Web：FastAPI + HTMX 单流 `/`、`/inbox`、`/item/<id>`；未读过期
- 忽略即隐藏相似项（同 canonical 域名 + 标题相似）
- Postgres 全文检索 → Telegram 发文字即搜索（关键词版）

## W3 — 记忆与助手

- embedding（bge-m3 本地或 Voyage）+ 软合并事件折叠 + 混合检索
- 白名单实体档案（entities/aliases/timeline）+ 实体页 + **watched 实体变动提醒**（日报回顾段）
- 收藏备注 + 收藏按实体归组
- 研究助手 `/dig`：模板化报告 → Telegram + Obsidian
- 周报；evaluate.py 离线回放；订阅调参页；`/debug/scores`
- Obsidian 只读渲染

## W4+ — 按指标决定

- 若日报 7 天 precision@8 ≥ 0.4 且愿意继续用 → 接公众号（wewe-rss，白名单）与 X（twitterapi.io，List）
- 开放式实体抽取 + 别名合并 + 人工确认
- burst 上升话题榜
- Chat 完整版（多轮、出处校验）
- 中英信息差标记

## 风险与预案

| 风险 | 预案 |
|---|---|
| GitHub search 限流 | token + 5000/h 足够；退避；月预算 |
| HN Algolia 变更 | 官方 Firebase API 兜底 |
| LLM 费用失控 | 日预算硬上限，超限只处理订阅命中 |
| 日报不好看不想用 | W2 观察期即调整条数/文案，验收以"愿意看完"为准 |
| 实体污染 | 白名单先行，timeline 挂 item_id 可批量撤销 |
| 单人维护倦怠 | 任何非 P0 功能都可停；P0 只依赖 3 个官方 API |
