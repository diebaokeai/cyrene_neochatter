# Cyrene NeoChatter（昔涟私聊引擎）

独立私聊引擎插件：自带 persona/WorldBook/分层记忆与心理活动流，
**不依赖** cyrene / dfc / nfc 等其它引擎，模型调用全走 MoFox LLM API，并预留本地推理接口位。

- 插件名：「cyrene_neochatter」
- 类型：私聊 Chatter（接管名单内用户的私聊）
- 版本：1.2.0（manifest）
- 当前配置：「config/plugins/cyrene_neochatter/config.toml」（默认禁用，需手动开启）

---

## 一、快速启用

1. 打开「config/plugins/cyrene_neochatter/config.toml」：
   - 「[general] enabled = true」开启引擎；
   - 「user_ids = ["3308747519"]」只对名单内私聊用户生效（留空 = 所有私聊）。
2. 重启 bot，或对运行实例调用 reload_plugin("cyrene_neochatter")。
3. 向名单内用户发一条私聊消息触发回合，确认日志不再出现「引擎回合异常」即正常。

> 引擎默认**只接管私聊**（chat_type=PRIVATE），群聊消息不会进入本引擎。

---

## 二、目录结构

~~~text
plugins/cyrene_neochatter/
├── plugin.py        # 插件入口：注册 Chatter / 迁移记忆 / 主动想起轮询
├── chatter.py       # BaseChatter 主循环（读未读→生成→分段发送→写心理流→自动写记忆）
├── config.py        # 配置模型（general/llm/wait/proactive/mental/memory/recall/memory_write）
├── llm.py           # Inference 门面：chat / embed / rerank（backend=api | local 预留）
├── memory.py        # 分层记忆存取 + 迁移 + WorldBook 解析 + 向量召回 + 去重
├── mental_log.py    # 会话内心理活动流（内心 / 心情 / 预期反应）
├── proactive.py     # 主动想起纯函数：沉默时长 / 概率 / 勿扰时段 / 预约判定
├── persona/         # 人设 md（chat_identity / soul / canon_quotes / chat_system / styles）
├── worldbook/       # WorldBook md（按触发词/常驻注入）
└── manifest.json
~~~

运行时数据（默认，按 QQ 分库）：

~~~text
data/cyrene_neochatter/users/<QQ>/memory.json           # 分层记忆（l0/l1/l2/relationship/entries/worldbook/meta…）
data/cyrene_neochatter/users/<QQ>/proactive_state.json  # 主动想起状态
data/cyrene_memory/users/<QQ>/memory.json               # DFC 人格层记忆
data/cyrene_neochatter/users/_shared/…                  # 解析不到人时的兜底（群聊等）
~~~

第一次启动会把旧版根目录下的 memory.json / proactive_state.json 自动搬进 users/<owner>/：
owner 取 [memory] legacy_owner_id，留空则取 [general] user_ids 的第一个。stream_id → QQ
解析失败时一律落到 users/_shared/，绝不会把记忆写进别人的目录。

---

## 三、回合流程（chatter.py execute）

1. 取该私聊流 context；无未读消息则 Wait() 无限挂起，等新消息唤醒；
2. 用户名单校验（名单为空 = 放行；否则仅名单内用户）；
3. 读 context.unread_messages 并格式化为「[称呼] 文本」行；
4. 通过 plugin._note_stream_activity() 刷新活跃时间（供主动想起计算沉默）；
5. 组装上下文：
   - 系统人格（assemble_system_persona：身份 + soul + 台词 + chat_system.md + 言行底线）
   - 记忆注入（[recall] 开关控制）：L0 画像 / L1 近期备忘 / relationship 关怀线索 /
     向量语义召回 / WorldBook 命中条目
   - 当前本地时间（开头注入「现在时间：…」，防止把时间说错）
   - 用户未读文本（「刚刚发生的事：…」）
   - 心理活动流（如有且非“还没有内心记录”）
6. Inference.chat() 单轮生成（[llm] backend=api，model_task=actor，上限 max_tokens）；
7. 按「完整句」切分逐条发送：句末标点（。！？!?；;）以及特殊尾缀（～♪♫…… 等）后强制切分；
   无句末标点的短诗行会自动与前/后句拼成完整一句，避免消息切太细；每条 send_text 一条消息，
   句间按文本长度模拟输入间隔（约 0.8s + 每字 0.12s，下限 1.2s、上限 4.0s）；空回复跳过；
8. 首段写入心理流（MentalLog）；
9. context.flush_unreads_to_history() 收口，进入下一轮 Wait。

> 中途异常（模型超时等）不会清空未读，下一条消息会重试；成功回合才会推进历史。

---

## 四、文本与表情策略（发送硬约束）

- 正文发送前统一调用「src/app/plugin_system/api/text_policy.py::strip_emoji」**剥离所有内联 emoji**
  （笑脸/捂脸/发怒等表情字形、旗帜、ZWJ 组合、星星装饰等），保证平台消息不出现 emoji 字符；
- 人设允许的装饰符号 **♪ / ♫ 保留**；
- 某段原本含真 emoji 时，发送干净文本后自动经 **emoji_sender** 服务
  （send_best_detailed）补发**一张图片式表情包**替代表达：
  - 每个流 **90 秒限频**；
  - emoji_sender 未启用 / 表情库为空 / 检索失败时**静默跳过**，不影响正文；
- 主动想起（proactive）发送同样先清洗；
- ASCII 颜文字（XD、(・ω・) 等）只做人格提示约束，不做硬剥离，避免误伤正文符号。

依赖：可选「emoji_sender」插件（提供图片表情包检索/发送）；未启用时本引擎退化为“纯文字无 emoji”。

---

## 五、记忆系统

### 5.1 结构（memory.json 顶层）

| 键 | 含义 |
|---|---|
| l0 | 长期画像（昵称/偏好/身份等） |
| l1 | 近期滚动备忘（对象压成可读文本） |
| l2 / evidence / reflection / conflict | 迁移的深层记录（当前未消费） |
| relationship | 逐轮关系记录（userText / userMood / importantMoment / nextCareCue …） |
| entries | 语义记忆条目（text + embedding，供向量召回/去重） |
| worldbook | 已解析的 WorldBook 条目（resident/triggers/content） |
| meta | migrated / 迁移时间等 |

### 5.2 迁移（memory.migrate）

- 源目录默认「%APPDATA%/live2d-cyrene」（[memory] source_dir 可覆盖），启动/首回合自动执行；
- 幂等：已迁移（meta.migrated）只做增量：
  - 自动补合并 worldbook/（若为空）；
  - 修复旧解析器产物：l1 若是 str(dict) 垃圾或本地为空而源有新内容 → 重算；缺
    reflection/conflict 且源存在 reflectionLogs/conflictLogs → 补齐；
- 键名兼容 reflectionLogs/conflictLogs（真实 schema）与旧 reflection/conflict。

### 5.3 召回注入（[recall]）

| 开关 | 默认 | 行为 |
|---|---|---|
| profile | true | 注入 L0 画像 + L1 近期备忘 |
| relationship_care | true | 取最近一条非“过度解读”的 nextCareCue |
| semantic | true | 对未读文本 embed 后余弦召回 entries（top_k=3, min_score=0.18） |
| worldbook | true | 命中触发词/常驻条目（worldbook_max=2） |

### 5.4 自动写入（[memory_write]）

- 每 interval_turns=6 回合触发一次；
- 用 utils_small 任务抽取 JSON：relationship（情绪信号/重要时刻/下次关怀线索）与
  memory_candidate（稳定信息）；
- 写 relationship 前校验开关；entries 带向量并在 dedupe_min_score=0.86 以上时跳过；
- 写入失败不影响回合（仅记 warning）。

---

## 六、心理活动流（mental_log）

- 会话内第一人称内心记录，上限「[mental] max_log_entries=60」；
- 回合中首段回复会自动 push 为一条内心记录，并在后续上下文回注（“你的内心活动”）；
- 「[mental] save_to_file」目前未实现（预留）。

---

## 七、主动想起 / 勿扰 / 预约（proactive + plugin 轮询）

后台每 check_interval=60s 对活跃私聊流检查：

- 前置条件（proactive.py::should_trigger）：
  - 非暂停、不在勿扰（quiet_start=23:00 ~ quiet_end=07:00）；
  - 距上次消息 ≥ silence_threshold=7200s（沉默兜底）；
  - 距上次主动 ≥ min_interval=1800s；
  - 满足后：有预约到期则必发，否则按 trigger_probability=0.3 概率触发；
- 触发后单轮生成一句简短贴合关系的话并 send_text；
- “活跃/上次消息时间”由每次回合的 _note_stream_activity 维护（曾为空转 bug，已修复）；
- scheduled_at / pause / 预约写入 目前没有上游写入方（预留，config 的
  schedule_guidance 仅为引导文本）。

---

## 八、配置速查（默认值）

| Section | 键（默认） | 说明 |
|---|---|---|
| general | enabled(false)、model_task("actor")、private_only(true)、user_ids([]) | 引擎开关 / 模型任务 / 私聊限定 / 名单 |
| llm | backend("api")、embed_task("embedding")、rerank_task("")、max_tokens(8000) | 推理接口 |
| wait | enabled(true)、min/max_seconds、max_consecutive_timeouts | 目前未参与调度（恒 Wait 等消息） |
| proactive | enabled(true)、silence_threshold(7200)、trigger_probability(0.3)、min_interval(1800)、quiet 时段、check_interval(60) | 主动想起 |
| mental | max_log_entries(60)、save_to_file(false) | 心理流 |
| memory | enabled(true)、data_dir、source_dir、auto_migrate(true)、legacy_owner_id("") | 记忆目录/迁移（旧数据归属人） |
| recall | profile/relationship_care/semantic/worldbook(true)、top_k(3)、min_score(0.18) | 召回注入 |
| memory_write | enabled(true)、task("utils_small")、interval_turns(6)、write_relationship/entries、dedupe_min_score(0.86)、compress_every_n_rounds(50) | 自动写记忆（压缩预留） |

---

## 九、已知限制 / 未完成（v0.1 骨架）

- [wait] 定时唤醒未接入：回合一律 Wait() 无限等待新消息；
- mental.save_to_file、proactive 预约（scheduled_at/paused）无写入方；
- L2→L1 压缩（compress_every_n_rounds）与 rerank（rerank_task）已预留未消费；
- llm.backend="local" 会抛 LocalNotReadyError（未实现）；
- 单轮无工具调用/子代理，只发纯文本（图片表情包经 emoji_sender 服务兜底而非模型动作）；
- ASCII 颜文字仅提示约束、不硬剥离。

---

## 十、维护备注

- 热加载：改动插件代码后需重启或 reload_plugin("cyrene_neochatter") 才生效；
- 文本策略依赖框架模块「src/app/plugin_system/api/text_policy.py」（勿单独删除）；
- 插件目录当前被仓库根 .gitignore 的 plugins/* 忽略（未放行），本地改动不会入库；
  如需要纳入版本管理，请在根 .gitignore 增加 !plugins/cyrene_neochatter/。

---

## 合并说明（单一插件）

自本次合并起，**所有私聊相关能力都只在这一个插件里修改**：原 `plugins/cyrene`（DFC 人格层）
已整体并入本插件并删除，persona 资源只有 `plugins/cyrene_neochatter/persona/` 一份。

本插件现在包含的组件：

| 组件 | 名称 | 作用 |
|---|---|---|
| chatter | cyrene_neochatter | 私聊引擎（记忆/心理流/主动想起/逐句发送） |
| router | cyrene_neochatter_memory_admin | Cyrene WebUI 插件控制台（/cyrene-webui） |
| event_handler | cyrene_persona_injector | 给 DFC 系统提示词注入昔涟人格 |
| event_handler | cyrene_style_injector | 按私聊/群聊注入风格档 |
| event_handler | cyrene_memory_injector | 给 DFC 用户提示词注入记忆（画像/召回/关怀/WorldBook） |
| event_handler | cyrene_memory_writer | DFC 侧自动写入记忆（data/cyrene_memory） |

原 `cyrene` 的配置段已并入本插件 config，冲突段改名：
`plugin → dfc_plugin`、`memory → dfc_memory`、`memory_write → dfc_memory_write`；
其余 `persona / style / memory_scope / memory_templates / memory_inject / memory_store` 保持原名。

两套记忆各管各的，互不干扰，但**都在 Cyrene WebUI 里管理**（`/cyrene-webui`）：

- 私聊引擎：`data/cyrene_neochatter/users/<QQ>/memory.json`（+ 会话心理流）→ 页面「总览/画像/关系记录/语义条目」面板；
- DFC 人格层：`data/cyrene_memory/users/<QQ>/memory.json` → 页面「DFC 记忆」面板（画像/条目/关系/WorldBook/重新导入）。

页面顶部的**「私聊对象」下拉框**（`?user=<QQ>`，选择记在浏览器本地）决定看谁：每个 QQ 的分层记忆、
DFC 记忆、主动状态都是独立文件，互不污染。

原独立的 DFC 记忆后台 `/cyrene-memory` 已移除，相关路由组件与页面文件已删除。

---

## 本次增强（对齐原版差距）

| 能力 | 说明 | 开关 |
|---|---|---|
| 回复前理解（CITA 轻量版） | 每轮先做一次轻量理解：指代消解 / 查询改写 / 焦点提取，结果以「【本轮理解】」注入 | [understanding] enabled/task/timeout_seconds |
| 上下文窗口（token 预算） | 历史按 token 预算选取（token_budget × chars_per_token），超出部分用 utils_small 滚动摘要并落盘 memory.json 的 history_summary | [context] token_budget/summary_enabled/summary_max_chars |
| 主动行为状态化 | 落盘 proactive_state.json（unanswered_count / desire / affinity / last_scene / epoch）；未回应与欲望提升触发概率 | [proactive] unanswered_boost/state_file |
| 记忆深度 | 同一轮抽取额外产出 events(l2) / reflection / conflict，写入并注入「最近的经历 / 我的反思 / 需要注意的变化」 | [memory_depth] events/reflection/conflict 各开关与保留上限 |
| rerank 精排 | 配置 rerank 任务后：先按 top_k×rerank_multiplier 粗筛（prefilter_min_score），再精排取 top_k；失败回退余弦 | [llm] rerank_task + [recall] rerank_multiplier/prefilter_min_score |

运行成本提示：理解与滚动摘要各多一次小模型调用（utils_small），记忆深度复用同一轮抽取调用、不额外加钱；rerank 每次语义召回多一次 HTTP 调用（SiliconFlow bge-reranker-v2-m3）。

上述三个运行时开关（回复前理解 / 历史滚动摘要 / 记忆 rerank）也可以在私聊记忆后台页面
「功能开关」卡片里随时切换，立即生效、重启后保留（存储在 data/feature_switches.json）。

「功能开关」里另有三个**接收开关**（默认全开，关闭 = 直接不收）：

| 开关 | 键名 | 关闭后 |
| --- | --- | --- |
| 接收 B 站直播弹幕 | recv_bilibili_live | 直播间弹幕在消息管线最上游被拦截：不回复、不写记忆，连排队插件也不处理 |
| 接收私聊消息 | recv_private | 私聊消息完全不处理 |
| 接收群聊消息 | recv_group | 群聊消息完全不处理 |

实现是 `event_handler:cyrene_receive_filter`（weight=120，高于弹幕插件的 100），因此它排在其他
处理器之前；拦截发生在框架的接收层（`BEFORE_MESSAGE_RECEIVED` 返回 STOP），消息不会进入任何插件的
后续流程。该 handler 随本插件加载：**插件停用则接收开关一起失效**。

此外，后台页面新增「全部配置」区块：自动列出本插件 20 个配置节的所有字段
（含说明、控件类型、范围/选项），可直接编辑并保存到 config/plugins/cyrene_neochatter/config.toml；
保存后会重载 plugin.config，运行中的引擎按新配置工作（部分需要组件重建的项建议再重载插件）。

