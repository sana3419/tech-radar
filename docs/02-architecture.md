# TechRadar — 架构与数据设计

> v1.0 · 2026-08-18

## 1. 总览

```
┌────────────────────── Collectors（每小时，幂等）──────────────────────┐
│ hn_algolia │ github_search │ rss_generic │ (P1) arxiv, gh_releases,   │
│ rsshub, devto │ (P2) wewe_rss, twitterapi │ manual_add (Telegram)    │
│   每个 fetcher 继承 BaseFetcher：限流 / 退避 / 月预算 / 健康记录       │
└───────────────────────────────┬─────────────────────────────────────┘
                                ▼ RawItem
┌────────────────────── Pipeline（状态机）─────────────────────────────┐
│ normalize → canonical_key → upsert items/item_sources → snapshot     │
│   → rule_filter(订阅命中/TopN/作者白名单) → enqueue enrich           │
│ enrich job（每 2-4h 批量）: LLM 结构化摘要/打标/实体匹配 → enriched  │
│ score job: 规则排序 + score_breakdown → scored                       │
│ digest job（每日 08:00）: 选 Top8+折叠10+回顾 → Telegram → digested  │
└───────────────────────────────┬─────────────────────────────────────┘
                                ▼
┌────────────────────── Storage: Postgres + pgvector ─────────────────┐
│ items · item_sources · snapshots · source_health · subscriptions     │
│ entities · entity_aliases · entity_timeline · feedback · feedback_features
│ agent_tasks · llm_usage · digests · digest_items                     │
└──────────┬──────────────────────────────────┬───────────────────────┘
           ▼                                  ▼
┌── Presentation ──────────────┐   ┌── Knowledge projection ──────────┐
│ Telegram bot（日报 + inline   │   │ Obsidian vault/TechRadar/        │
│   按钮 + 发文字即搜索 + /add）│   │   entities/*.md · digests/*.md   │
│ Web: FastAPI + HTMX + Jinja2 │   │   research/*.md（只读生成）      │
└──────────┬───────────────────┘   └──────────────────────────────────┘
           ▼ feedback
┌── Agents ───────────────────────────────────────────────────────────┐
│ llm/ 统一调用层（schema、cache、usage、预算）                        │
│ enrich_agent（Haiku 级，批量）· research_agent（Opus 级，按需，工具白名单）
│ chat_agent（P1：混合检索 → 带 ID 引用回答 → 出处校验）              │
└─────────────────────────────────────────────────────────────────────┘
```

设计原则：

1. **DB 是唯一真相源**；Obsidian、Telegram、Web 都是投影。
2. **每一步幂等**，靠 items.status 状态机恢复，崩溃后重跑不产生重复。
3. **LLM 在管线末端**，只处理通过规则筛选的条目；所有调用走统一层，可记账、可降级。
4. **为脆弱源预留位置但不在 P0 接入**：BaseFetcher 与 item_sources 从 Day1 支持多源、多指标、无指标源。
5. **可解释**：每条排序结果保存 score_breakdown，日报显示入选原因。

## 2. 数据模型（Postgres）

```sql
-- 去重后的条目（一个"事物"）
CREATE TABLE items (
  id              BIGSERIAL PRIMARY KEY,
  canonical_key   TEXT UNIQUE NOT NULL,        -- 见 §3
  kind            TEXT NOT NULL,               -- repo | paper | article | release | post | other
  title           TEXT NOT NULL,
  url             TEXT NOT NULL,               -- 主 URL（去 utm）
  lang            TEXT,                        -- zh | en | ...
  published_at    TIMESTAMPTZ,
  first_seen_at   TIMESTAMPTZ NOT NULL,
  last_seen_at    TIMESTAMPTZ NOT NULL,
  content         TEXT,                        -- 正文/README/摘要（P1）
  content_level   SMALLINT DEFAULT 0,          -- 0 仅标题 1 摘要 2 全文
  status          TEXT NOT NULL DEFAULT 'new', -- new|filtered|queued|enriched|scored|digested|expired
  -- enrich 结果
  summary_one     TEXT,
  summary_points  JSONB,                       -- ["...","...","..."]
  tags            JSONB,                       -- {"domain":[...],"stack":[...],"type":"release"}
  enrich_model    TEXT, enrich_version TEXT,
  embedding       VECTOR(1024),                -- P1
  embedding_model TEXT,
  -- score
  score           DOUBLE PRECISION,
  score_breakdown JSONB,                       -- {"sub_hit":1,"heat":0.7,"decay":0.9,"author":1,...}
  ranker_version  TEXT,
  reasons         JSONB,                       -- ["命中订阅: 推理框架","3 平台同时出现"]
  event_id        BIGINT,                      -- P1 事件折叠
  created_at      TIMESTAMPTZ DEFAULT now(),
  updated_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON items (status, score DESC);
CREATE INDEX ON items (first_seen_at DESC);
CREATE INDEX ON items USING GIN (to_tsvector('simple', coalesce(title,'')||' '||coalesce(summary_one,'')));

-- 同一 item 在各平台的出现
CREATE TABLE item_sources (
  id            BIGSERIAL PRIMARY KEY,
  item_id       BIGINT REFERENCES items(id) ON DELETE CASCADE,
  source        TEXT NOT NULL,                 -- hackernews | github | arxiv | rss:<feed_id> | wechat:<mp> | x
  external_id   TEXT NOT NULL,
  source_url    TEXT,
  author        TEXT,
  author_key    TEXT,                          -- 归一后的作者标识，用于作者权重
  metrics_raw   JSONB,                         -- {"points":320,"comments":88} / {"stars":2100,"forks":90}
  seen_at       TIMESTAMPTZ NOT NULL,
  raw           JSONB,
  UNIQUE (source, external_id)
);
CREATE INDEX ON item_sources (item_id);

-- 指标快照（时间序列，按年龄分层回访：<24h 每小时，<7d 每 6h，>7d 停）
CREATE TABLE snapshots (
  item_source_id BIGINT REFERENCES item_sources(id) ON DELETE CASCADE,
  ts             TIMESTAMPTZ NOT NULL,
  metrics        JSONB NOT NULL,
  PRIMARY KEY (item_source_id, ts)
);

CREATE TABLE source_health (
  source               TEXT PRIMARY KEY,
  last_run_at          TIMESTAMPTZ,
  last_success_at      TIMESTAMPTZ,
  last_items           INT,
  consecutive_failures INT DEFAULT 0,
  last_error           TEXT,
  month_calls          INT DEFAULT 0,
  month_budget         INT
);

-- 订阅（从 YAML 同步进来，DB 便于统计命中）
CREATE TABLE subscriptions (
  id       SERIAL PRIMARY KEY,
  kind     TEXT NOT NULL,        -- topic | author | entity | source
  key      TEXT NOT NULL,        -- 关键词 / author_key / entity_id / source
  config   JSONB,                -- {"queries":[...],"boost":1.5,"sources":[...]}
  weight   DOUBLE PRECISION DEFAULT 1.0,
  active   BOOLEAN DEFAULT TRUE,
  UNIQUE (kind, key)
);

-- 实体（P1 白名单起步）
CREATE TABLE entities (
  id             SERIAL PRIMARY KEY,
  canonical_name TEXT UNIQUE NOT NULL,
  type           TEXT NOT NULL,     -- project | model | company | person | concept
  anchors        JSONB,             -- {"github":"vllm-project/vllm","site":"..."}
  first_seen_at  TIMESTAMPTZ,
  watched        BOOLEAN DEFAULT FALSE,   -- 收藏/深挖过 → 变动提醒
  notes          TEXT,
  status         TEXT DEFAULT 'active'    -- active | needs_review | merged
);
CREATE TABLE entity_aliases (
  alias     TEXT PRIMARY KEY,
  entity_id INT REFERENCES entities(id) ON DELETE CASCADE
);
CREATE TABLE entity_timeline (
  id         BIGSERIAL PRIMARY KEY,
  entity_id  INT REFERENCES entities(id) ON DELETE CASCADE,
  item_id    BIGINT REFERENCES items(id) ON DELETE CASCADE,
  event_type TEXT,               -- release | paper | discussion | incident | mention
  ts         TIMESTAMPTZ NOT NULL,
  note       TEXT,
  UNIQUE (entity_id, item_id)
);

-- 用户反馈
CREATE TABLE feedback (
  id        BIGSERIAL PRIMARY KEY,
  item_id   BIGINT REFERENCES items(id) ON DELETE CASCADE,
  action    TEXT NOT NULL,       -- save | ignore | mute_source | click | expand | dig | read
  channel   TEXT,                -- telegram | web
  note      TEXT,
  ts        TIMESTAMPTZ DEFAULT now()
);
-- 反馈时刻的特征快照（供后续离线评估/训练）
CREATE TABLE feedback_features (
  feedback_id     BIGINT PRIMARY KEY REFERENCES feedback(id) ON DELETE CASCADE,
  ranker_version  TEXT,
  score           DOUBLE PRECISION,
  score_breakdown JSONB,
  rank_in_digest  INT,
  tags            JSONB,
  sources         JSONB
);
-- 属性级偏好（Beta 平滑：alpha 正反馈，beta 负反馈）
CREATE TABLE preferences (
  kind  TEXT NOT NULL,           -- tag | source | author | entity
  key   TEXT NOT NULL,
  alpha DOUBLE PRECISION DEFAULT 1,
  beta  DOUBLE PRECISION DEFAULT 1,
  muted_until TIMESTAMPTZ,
  PRIMARY KEY (kind, key)
);

-- Agent 任务队列（研究/摘要批次/聊天）
CREATE TABLE agent_tasks (
  id          BIGSERIAL PRIMARY KEY,
  type        TEXT NOT NULL,     -- enrich_batch | research | chat
  payload     JSONB NOT NULL,
  status      TEXT DEFAULT 'pending',   -- pending | running | done | failed
  attempts    INT DEFAULT 0,
  result      JSONB,
  error       TEXT,
  model       TEXT, prompt_version TEXT,
  tokens_in   INT, tokens_out INT, cost_usd NUMERIC(10,5),
  created_at  TIMESTAMPTZ DEFAULT now(),
  finished_at TIMESTAMPTZ
);
CREATE INDEX ON agent_tasks (status, created_at);

CREATE TABLE llm_usage (
  day       DATE PRIMARY KEY,
  calls     INT DEFAULT 0,
  tokens_in BIGINT DEFAULT 0, tokens_out BIGINT DEFAULT 0,
  cost_usd  NUMERIC(10,4) DEFAULT 0
);

CREATE TABLE digests (
  id        SERIAL PRIMARY KEY,
  day       DATE UNIQUE NOT NULL,
  kind      TEXT DEFAULT 'daily',   -- daily | weekly
  markdown  TEXT,
  sent_at   TIMESTAMPTZ,
  stats     JSONB                   -- {"pushed":8,"folded":10,"cost":0.12}
);
CREATE TABLE digest_items (
  digest_id INT REFERENCES digests(id) ON DELETE CASCADE,
  item_id   BIGINT REFERENCES items(id) ON DELETE CASCADE,
  section   TEXT,      -- top | folded | recall
  position  INT,
  PRIMARY KEY (digest_id, item_id)
);
-- P1: events(id, key, title, summary, first_seen_at, item_count)
```

## 3. canonical_key 规则

按优先级：

1. GitHub 仓库 URL → `gh:owner/repo`（小写，去 `.git`、`/tree/...`）
2. arXiv → `arxiv:2408.12345`（去版本号 v2）
3. DOI → `doi:10.xxxx/...`
4. Hugging Face 模型/数据集 → `hf:org/name`
5. 其他 URL：展开短链 → 去 utm_*/ref/source 参数 → 去 fragment → 去尾斜杠 → 强制 https → 域名小写 → `url:<sha1(normalized)>`
6. HN/Reddit 帖子若指向外链，key 取**外链**的 key（帖子本身进 item_sources）；纯文本帖取 `hn:<id>`

P1 软合并：同 48h 内 title embedding 余弦 ≥ 0.9 且无冲突锚点 → 合并 item_sources 到较早 item，记录 merged_from 供撤销。

## 4. 排序

### P0（规则）
```
score = w_sub * sub_hit            # 订阅命中：0/1，命中多个取 max(boost)
      + w_heat * heat_pct          # 平台内热度分位数 [0,1]，无指标源 = 0.5
      + w_author * author_w        # 白名单作者权重，默认 0
      - decay(age_hours)           # 时间衰减：age 用 published_at，缺失用 first_seen_at
score *= pref_multiplier(tags, source, author)   # Beta 偏好均值 ∈ [0.5,1.5]，muted → 0
```
默认 w_sub=3, w_heat=1, w_author=1；decay = 0.5*log1p(age_hours/24)。热度分位数按 (source, metric) 滚动 14 天 log 值分布。全部因子写入 score_breakdown；reasons 由命中的因子生成文案。

### P1（对数域加权求和完整版）
```
score = Σ w_i * f_i
f_heat     = percentile(log1p(metric))                     # 平台内 14d 分位
f_growth   = clip(log1p(Δmetric/Δt / baseline), 0, 3)      # 按平台快慢分档；首见取中性
f_cross    = log1p(#distinct sources)                       # 硬合并即生效
f_author   = author_weight
f_sub      = max(boost of hits)
f_pref     = Beta 均值偏好
f_recall   = watched 实体命中（回顾段用）
decay      = gravity * log1p(age_hours/24)，gravity 按 type 分（release 慢、post 快）
```
探索配额：日报 Top8 中预留 1-2 个位置给"高热度但未命中订阅"的条目，防兴趣过窄。
每周评估：precision@8（点击+收藏 / 推送）、忽略在 Top5 占比、来源熵、订阅命中率；`evaluate.py` 用 feedback_features 离线回放不同权重。

## 5. Agent 设计

### 5.1 统一 LLM 层 `llm/client.py`
- `call(schema: PydanticModel, system, user, model, cache_prefix)` → 结构化对象；重试 2 次；失败抛给 agent_tasks
- 每次调用写 llm_usage；超过日预算 → 降级：只处理订阅命中条目
- Provider 可切换：默认 OpenAI 兼容接口（DeepSeek：enrich `deepseek-chat`，research `deepseek-reasoner`；也可指向 OpenAI 官方/网关），Anthropic 作为可选 provider；结构化输出用 json_object + schema 提示 + pydantic 校验（失败带错误重试 1 次）；缓存命中按 provider 返回的 cached tokens 计费

### 5.2 enrich（P0）
输入：title + url + content（若有）+ 候选白名单实体列表
输出 schema：
```json
{
 "summary_one": "≤40字中文一句话",
 "points": ["…","…","…"],
 "type": "release|tool|paper|opinion|tutorial|incident|other",
 "domains": ["llm","infra",...],        // 封闭枚举，来自 config/taxonomy.yaml
 "stacks": ["python","rust",...],       // 封闭枚举
 "entities": ["vLLM"],                  // 只能从候选列表选
 "lang": "zh|en"
}
```
批量：每 2-4h 把 queued 条目分批（20 条/请求或 Batches API）。

### 5.3 research（P1）
- 触发：Telegram 🔍 / Web 按钮 → agent_tasks(type=research)
- 工具白名单：fetch_url、github_readme、github_issues(top)、search_items(本地 DB)、arxiv_abs；max_steps=15；task_budget=$0.5
- 输出固定 schema：`{tldr, should_follow(是/否/观望 + 理由), key_facts[], relation_to_known[]（引用本地 item/entity id）, risks[], sources[]}` → 渲染为 ≤500 字 Telegram 消息 + Obsidian `research/<slug>.md`
- 副作用：相关实体 watched=true

### 5.4 Agent runtime 与工具接口（决策：自研 loop，不用 Hermes/pi）
- research / chat 的 agent loop 用 anthropic SDK 自研（工具白名单 + max_steps + 预算），不引入第三方 agent runtime；评估结论见 00-review 后记：TechRadar 80% 是确定性管线与结构化记忆，agent 只占 20%，不值得让 runtime 决定架构。
- **核心能力全部通过 MCP server 暴露**（`techradar/mcp_server.py`，stdio + streamable HTTP），使 Telegram bot、Web、自研 agent、以及未来任何外部客户端（Claude Code、其他 agent runtime）都走同一套接口，核心与前门解耦：

| 工具 | 说明 |
|---|---|
| `search_items(query, since?, until?, only_saved?, limit)` | 混合检索（P0 全文 / P1 向量），返回 id/标题/一句话/来源/时间 |
| `get_item(id)` | 详情 + 来源 + 摘要 + 要点 + 分数拆解 |
| `get_digest(day?)` | 某日日报及条目 |
| `list_inbox(limit)` | 收藏列表 |
| `feedback(item_id, action, note?)` | save / ignore / read / dig |
| `mute(kind, key, days)` | 静音来源/标签/作者 |
| `add_url(url, note?)` | 手动投喂 |
| `get_entity(name_or_id)` / `list_watched_entities()` / `entity_timeline(id, since?)` | 实体档案（P1） |
| `list_sources_health()` | 源健康 |
| `trigger_research(item_id?|entity_id?, question?)` | 入队研究任务，返回 task_id |
| `get_task(task_id)` | 任务状态与结果 |
| `usage(day?)` | LLM 花费 |

- 内部调用与 MCP 调用共用同一层 `techradar/services/*.py`（纯函数 + DB session），MCP 只是薄封装；这样 Web/bot 直接 import service，外部客户端走 MCP，行为一致。

### 5.5 chat / 搜索（P1）
- Telegram 非命令文本 → 搜索：tsvector + pgvector 混合，rerank，按时间过滤（"上个月"）与 feedback 过滤（"我收藏过的"）
- 回答模板强制带 `[#item_id]`，后处理校验 id 存在，否则剔除句子

## 6. 调度

| Job | 频率 | 说明 |
|---|---|---|
| fetch:<source> | 每小时（错峰） | 各源独立，失败只影响自身 |
| snapshot | 每小时 | 分层回访 |
| enrich_batch | 每 2-4h | 处理 queued |
| score | enrich 后 | 重算近 72h 条目 |
| digest_daily | 08:00 | 生成 + 发送 + 记 digest_items |
| expire | 每日 | 48h 未读 → expired |
| digest_weekly | 周日 20:00 | P1 |
| obsidian_render | 每日 | P1，hash 比对只写变更 |
| research worker | 常驻轮询 agent_tasks | P1 |

APScheduler in-process；研究任务用 DB 表当队列（SELECT … FOR UPDATE SKIP LOCKED）。

## 7. 呈现

- **Telegram**：python-telegram-bot；日报为一条消息（超长拆两条）；inline 按钮 callback_data=`act:save:<item_id>`；`/add <url>`、`/mute <source> 7d`、`/web`；非命令文本 → 搜索
- **Web**：FastAPI + Jinja2 + HTMX；路由：`/`（今日未读流）、`/inbox`、`/item/<id>`、`/search`（P1）、`/entity/<id>`（P1）、`/debug/scores`（P1）、`/config/subscriptions`（P1）
- **Obsidian**：`render/obsidian.py` 用同一套 Jinja 模板输出 MD；frontmatter 含 entity_id / generated_at / hash

## 8. 技术栈

Python 3.12 · FastAPI · SQLAlchemy 2 + Alembic · psycopg · pgvector · APScheduler · httpx · feedparser · pydantic v2 · anthropic SDK · python-telegram-bot · Jinja2 + HTMX · Docker Compose（postgres + app）· pytest

## 9. 目录结构

```
tech-radar/
├── docs/                      # 本目录
├── config/
│   ├── settings.example.toml  # DB/Telegram/LLM keys、预算、日报参数
│   ├── subscriptions.yaml     # topics / authors / entities / sources
│   ├── profile.yaml           # 初始兴趣画像 20-30 行
│   └── taxonomy.yaml          # domains / stacks / types 封闭枚举
├── techradar/
│   ├── models.py              # SQLAlchemy 模型
│   ├── db.py
│   ├── fetchers/
│   │   ├── base.py            # BaseFetcher：限流/退避/预算/健康
│   │   ├── hn.py  github.py  rss.py
│   │   └── (P1) arxiv.py  gh_releases.py  rsshub.py  manual.py
│   ├── pipeline/
│   │   ├── normalize.py  canonical.py  dedupe.py  snapshot.py
│   │   ├── filter.py          # 规则筛选 → queued
│   │   ├── enrich.py          # 调 llm，写回 items
│   │   └── score.py           # 排序 + breakdown + reasons
│   ├── llm/
│   │   ├── client.py  schemas.py  prompts/
│   ├── agents/
│   │   ├── research.py  chat.py  (P1)
│   ├── digest/
│   │   ├── daily.py  weekly.py  templates/
│   ├── bot/                   # telegram
│   ├── web/                   # fastapi + templates/
│   ├── render/obsidian.py     # P1
│   ├── scheduler.py
│   └── cli.py                 # techradar fetch|enrich|score|digest|evaluate
├── scripts/evaluate.py
├── tests/
├── alembic/
├── docker-compose.yml
└── pyproject.toml
```
