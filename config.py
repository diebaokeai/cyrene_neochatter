"""cyrene_neochatter 配置（自包含，独立于其它插件）。"""

from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


DEFAULT_SYSTEM_TEMPLATES: list[str] = ["default_chatter_system_prompt"]
DEFAULT_USER_TEMPLATES: list[str] = ["default_chatter_user_prompt"]
STYLE_FILES: list[str] = [
    "01_default.md", "02_lively.md", "03_healing.md", "04_focused.md", "05_sweet.md",
]


class CyreneNeoConfig(BaseConfig):
    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "Cyrene NeoChatter 昔涟私聊引擎配置"

    @config_section("general", title="基础", tag="plugin")
    class GeneralSection(SectionBase):
        enabled: bool = Field(default=False, description="是否启用引擎（默认关闭，开启后接管私聊）")
        model_task: str = Field(default="actor", description="主对话 LLM 模型任务（model.toml 中 task）")
        private_only: bool = Field(default=True, description="仅私聊；群聊直接放行给其它 chatter")
        user_ids: list[str] = Field(default_factory=list, description="仅对名单内用户（私聊）生效；留空=所有私聊", tag="list")
        history_limit: int = Field(default=12, ge=0, le=50, description="每回合注入的最近历史对话条数（0=不注入；用于保持多轮上下文连续）")

    @config_section("llm", title="推理接口", tag="ai")
    class LLMSection(SectionBase):
        backend: str = Field(default="api", description="api=走 MoFox LLM API；local=预留本地推理接口（尚未实现，会提示）", input_type="select", choices=["api", "local"])
        embed_task: str = Field(default="embedding", description="向量模型任务（bge-m3）")
        rerank_task: str = Field(default="", description="可选 rerank 任务名；留空表示不使用 rerank")
        max_tokens: int = Field(default=8000, ge=256, le=128000, description="单次输出上限")

    @config_section("wait", title="等待", tag="timer")
    class WaitSection(SectionBase):
        enabled: bool = Field(default=True)
        min_seconds: float = Field(default=10.0)
        max_seconds: float = Field(default=600.0)
        max_consecutive_timeouts: int = Field(default=3, ge=1, le=20)

    @config_section("proactive", title="主动想起", tag="timer")
    class ProactiveSection(SectionBase):
        enabled: bool = Field(default=True)
        silence_threshold: int = Field(default=7200, ge=0, le=2592000, description="沉默多久(秒)后可能主动想起")
        trigger_probability: float = Field(default=0.3, ge=0.0, le=1.0)
        min_interval: int = Field(default=1800, ge=0, le=2592000)
        unanswered_boost: float = Field(default=0.2, ge=0.0, le=1.0, description="对方连续未回应时每次增加的触发概率（最多按 3 次累计）")
        state_file: str = Field(default="proactive_state.json", description="主动行为状态落盘文件名（相对记忆目录）")
        quiet_start: str = Field(default="23:00", description="勿扰开始 HH:MM")
        quiet_end: str = Field(default="07:00", description="勿扰结束 HH:MM")
        check_interval: int = Field(default=60, ge=5, le=86400)
        schedule_guidance: str = Field(default="预约是长线联系的主力：每次对话后自然想想下次何时联系；沉默触发只是兜底。", description="预约工具使用引导（预留）")
        inject_context: bool = Field(default=True, description="主动发消息前注入最近对话（真实历史；运行时可用功能开关覆盖）")
        inject_memory: bool = Field(default=True, description="主动发消息前注入记忆（画像/摘要/经历/反思/关系/语义召回；运行时可用功能开关覆盖）")
        context_limit: int = Field(default=12, ge=0, le=50, description="注入的最近对话条数（0=不注入上下文）")
        context_hours: float = Field(default=48.0, ge=1.0, le=720.0, description="最近对话回溯时长（小时）")

    @config_section("mental", title="心理活动流", tag="ai")
    class MentalSection(SectionBase):
        max_log_entries: int = Field(default=60, ge=5, le=500)
        save_to_file: bool = Field(default=False, description="把 mental log 落盘便于回看")

    @config_section("memory", title="记忆", tag="file")
    class MemorySection(SectionBase):
        enabled: bool = Field(default=True)
        data_dir: str = Field(default="data/cyrene_neochatter", description="分层记忆目录（独立，不依赖 booku/cyrene）")
        source_dir: str = Field(default="", description="F 桌用户数据源（%APPDATA%/live2d-cyrene），留空自动探测")
        auto_migrate: bool = Field(default=True, description="首次加载自动迁移 F 桌记忆（rag/relationship/memory.json L0/L1/L2）")
        legacy_owner_id: str = Field(
            default="",
            description="按人分库时旧数据归属谁（留空 = general.user_ids 的第一个人）",
        )

    @config_section("recall", title="召回注入", tag="ai")
    class RecallSection(SectionBase):
        profile: bool = Field(default=True)
        relationship_care: bool = Field(default=True)
        semantic: bool = Field(default=True)
        worldbook: bool = Field(default=True)
        top_k: int = Field(default=3, ge=1, le=8)
        min_score: float = Field(default=0.18, ge=0.0, le=1.0)
        rerank_multiplier: int = Field(default=4, ge=1, le=10, description="配置了 rerank 时先按 top_k×该倍数放宽召回，再精排取 top_k")
        prefilter_min_score: float = Field(default=0.05, ge=0.0, le=1.0, description="rerank 前的粗筛阈值（应低于 min_score，给精排更多候选）")
        per_text_chars: int = Field(default=220, ge=40, le=600)
        total_chars: int = Field(default=2200, ge=200, le=6000)
        worldbook_max: int = Field(default=2, ge=1, le=5)

    @config_section("memory_write", title="自动写入", tag="ai")
    class MemoryWriteSection(SectionBase):
        enabled: bool = Field(default=True)
        task: str = Field(default="utils_small")
        interval_turns: int = Field(default=6, ge=1, le=200)
        min_interval_seconds: int = Field(default=600, ge=0, le=86400)
        write_relationship: bool = Field(default=True)
        write_entries: bool = Field(default=True)
        max_entry_chars: int = Field(default=240, ge=40, le=600)
        dedupe_min_score: float = Field(default=0.86, ge=0.0, le=1.0)
        compress_every_n_rounds: int = Field(default=50, ge=0, le=1000, description="每 N 回合压缩 L2→L1（0=关闭）")
        compress_task: str = Field(default="utils_small")

    @config_section("understanding", title="回复前理解", tag="ai")
    class UnderstandingSection(SectionBase):
        enabled: bool = Field(default=True, description="回复前先做一次轻量上下文理解（指代消解/查询改写/焦点提取）")
        task: str = Field(default="utils_small", description="理解阶段使用的 LLM 任务")
        timeout_seconds: float = Field(default=8.0, ge=1.0, le=60.0, description="理解阶段超时（秒），超时按失败回退")
        max_history_chars: int = Field(default=1200, ge=0, le=6000, description="送入理解阶段的最近历史字符上限")
        max_chars: int = Field(default=200, ge=40, le=800, description="resolved_query/focus 等字段字数上限")

    @config_section("context", title="上下文窗口", tag="performance")
    class ContextSection(SectionBase):
        token_budget: int = Field(default=3000, ge=200, le=32000, description="注入历史的 token 预算（按 chars_per_token 估算）")
        chars_per_token: float = Field(default=1.8, ge=0.5, le=4.0, description="字符/token 估算比例（中文约 1.5~1.8）")
        summary_enabled: bool = Field(default=True, description="超出预算的旧历史是否滚动摘要成一段摘要注入")
        summary_max_chars: int = Field(default=600, ge=100, le=2000, description="滚动历史摘要最大字数")
        summary_task: str = Field(default="utils_small", description="生成历史摘要使用的 LLM 任务")

    @config_section("memory_depth", title="记忆深度（事件/反思/冲突）", tag="ai")
    class MemoryDepthSection(SectionBase):
        events_enabled: bool = Field(default=True, description="写入并注入 l2 事件（经历层）")
        events_keep: int = Field(default=300, ge=20, le=2000, description="l2 事件保留上限")
        inject_recent_events: int = Field(default=5, ge=0, le=20, description="注入最近 N 条事件")
        reflection_enabled: bool = Field(default=True, description="写入并注入反思（reflection）")
        reflection_keep: int = Field(default=100, ge=10, le=1000, description="反思保留上限")
        inject_reflections: int = Field(default=2, ge=0, le=10, description="注入最近 N 条反思")
        conflict_enabled: bool = Field(default=True, description="记录矛盾/事实更新（conflict）")
        conflict_keep: int = Field(default=100, ge=10, le=1000, description="冲突记录保留上限")
        inject_conflicts: int = Field(default=2, ge=0, le=10, description="注入最近 N 条未解决冲突")

    @config_section("voice", title="语音回复", tag="ai")
    class VoiceSection(SectionBase):
        enabled: bool = Field(default=False, description="开启后私聊每句话都会合成一条语音发出（需 tts_http_server + gsv_tts_provider 已启用）")
        mode: str = Field(default="after_text", description="after_text=先发文字再补一条语音；voice_only=只发语音（合成失败自动退回文字）", input_type="select", choices=["after_text", "voice_only"])
        provider: str = Field(default="", description="TTS provider 名称；留空=服务端默认（如 gsv）")
        endpoint: str = Field(default="http://127.0.0.1:8002/router/tts_http_server/api/tts/v1/synthesize", description="HTTP 回退用的 TTS 接口（进程内服务不可用时使用，端口按 core.toml http_router_port）")
        timeout: float = Field(default=30.0, ge=1.0, le=300.0, description="单句 TTS 超时（秒）")
        max_chars: int = Field(default=120, ge=10, le=500, description="单句超过该字数则不合成语音（避免超长）")
        mime_type: str = Field(default="audio/wav", description="请求 TTS 时声明的 MIME 类型")

    @config_section("webui", title="控制台", tag="performance")
    class WebuiSection(SectionBase):
        auto_open: bool = Field(default=False, description="bot 启动完成后自动用默认浏览器打开 Cyrene WebUI")
        auto_open_url: str = Field(default="", description="要打开的地址；留空=自动探测 http_router host/port 后打开 /cyrene-webui/")
        auto_open_delay: float = Field(default=3.0, ge=0.0, le=120.0, description="启动后等待服务就绪的秒数（探测最多再等 60 秒）")

    general: GeneralSection = Field(default_factory=GeneralSection)
    llm: LLMSection = Field(default_factory=LLMSection)
    wait: WaitSection = Field(default_factory=WaitSection)
    proactive: ProactiveSection = Field(default_factory=ProactiveSection)
    mental: MentalSection = Field(default_factory=MentalSection)
    memory: MemorySection = Field(default_factory=MemorySection)
    recall: RecallSection = Field(default_factory=RecallSection)
    memory_write: MemoryWriteSection = Field(default_factory=MemoryWriteSection)
    understanding: UnderstandingSection = Field(default_factory=UnderstandingSection)
    context: ContextSection = Field(default_factory=ContextSection)
    memory_depth: MemoryDepthSection = Field(default_factory=MemoryDepthSection)
    voice: VoiceSection = Field(default_factory=VoiceSection)
    webui: WebuiSection = Field(default_factory=WebuiSection)
    # ===================== DFC 人格 / 记忆层（并入自 cyrene 插件） =====================

    @config_section("dfc_plugin", title="插件设置", tag="plugin")
    class DfcPluginSection(SectionBase):
        """插件基础开关。"""

        enabled: bool = Field(
            default=True,
            description="是否启用昔涟人格层插件。关闭后不注入任何内容，可作旧 cyrene_consistency 的停用替代。",
            label="启用插件",
            tag="plugin",
        )
        debug_log: bool = Field(
            default=False,
            description="输出注入过程的调试日志",
            label="调试日志",
            tag="debug",
        )

    @config_section("persona", title="人格板块", tag="ai")
    class PersonaSection(SectionBase):
        """系统提示词里注入哪些人格板块（全部来自 plugins/cyrene_neochatter/persona/ 下的 md，尽量原样）。"""

        role_name: str = Field(
            default="昔涟",
            description="角色本名。平台账号昵称（nickname）不等于本名，本插件在身份行会同时说明。",
            label="角色名",
            tag="general",
        )
        target_system_templates: list[str] = Field(
            default_factory=lambda: DEFAULT_SYSTEM_TEMPLATES.copy(),
            description="要注入人格层的系统模板名（on_prompt_build 的 name）。",
            label="系统模板",
            tag="list",
        )
        target_user_templates: list[str] = Field(
            default_factory=lambda: DEFAULT_USER_TEMPLATES.copy(),
            description="要注入风格档的用户模板名（on_prompt_build 的 name）。",
            label="用户模板",
            tag="list",
        )
        include_identity: bool = Field(
            default=True,
            description="注入 chat_identity.md（聊天身份定位）",
            label="身份定位",
            tag="ai",
        )
        include_soul: bool = Field(
            default=True,
            description="注入 soul.md（人格核心，Cyrene 原版 457 行）",
            label="人格核心 soul",
            tag="ai",
        )
        include_chat_system: bool = Field(
            default=True,
            description="注入 chat_system.md（聊天模式硬规则：事实边界/输出规则/身份应答口径）",
            label="聊天规则",
            tag="ai",
        )
        include_canon_lite: bool = Field(
            default=True,
            description="注入 canon_quotes_lite.md（精简原作台词语气基准）",
            label="台词样本(lite)",
            tag="ai",
        )
        include_canon_full: bool = Field(
            default=False,
            description="额外注入完整版 canon_quotes.md（体积更大，仅语气样本更全时开启）",
            label="台词样本(full)",
            tag="ai",
        )
        canon_usage_enabled: bool = Field(
            default=True,
            description="注入台词使用指引：允许在贴合场景自然化用/轻引 canon 台词（如道别说明天见、安慰说笑一笑好吗）",
            label="台词化用指引",
            tag="ai",
        )
        include_harness: bool = Field(
            default=False,
            description="额外注入 cyrene_harness.md（执行/工具循环中的昔涟表达约束）",
            label="执行人设 harness",
            tag="ai",
        )
        include_verbal_limits: bool = Field(
            default=True,
            description="追加昔涟言行红线（轻笑克制、禁用长笑/网络大笑腔/表情符号等）。这是对原样 md 的补充硬规则，置于人格层最末尾。",
            label="言行红线",
            tag="ai",
        )
        verbal_limits: list[str] = Field(
            default_factory=lambda: [
                "笑要轻盈克制：开心时用短促的「嘻~」「哈哈~」点缀即可，禁止连续四个以上「哈」（如「哈哈哈哈」），也不要说「笑死(人家)了」等网络大笑腔。",
                "不要在文本里粘贴 🤣😂😆 等 emoji、颜文字或贴纸式符号（如 XD、(・ω・)、[ENJOY]）；想用表情时请调用 send_emoji_meme 动作发送表情包，正文文字保持干净。不用「蚌埠住了」「真的会谢」这类网感词——你的俏皮来自措辞与节奏。",
                "别人（如 RIPPLE）只是在闲聊或叹气时，先温柔接住对方的话，不要先用一串大笑开场、更不要拿对方当笑料。",
                "输出是纯文字聊天：不要使用括号或星号描写动作/神态/心理（如（叹气）、（笑）、*脸红*、{摸摸头}），不要先写场景再说话——你真正发出的就是你要说的话。",
                "不在正文文字里粘贴 emoji/颜文字（🤣😄🤔、XD、(・ω・) 等）；情绪想用图表达时通过 send_emoji_meme 动作发送系统表情包，文字里不出现 emoji 符号。",
            ],
            description="言行红线条目，逐条以「- 」追加到人格层末尾",
            label="红线条目",
            tag="list",
        )
        override_intro: bool = Field(
            default=True,
            description="把 dfc 系统模板里的 <introduce>…</introduce> 替换为简短身份行（移除通用 AI 任务愿景长文）",
            label="替换 intro",
            tag="ai",
        )

    @config_section("style", title="表达风格", tag="text")
    class StyleSection(SectionBase):
        """按聊天类型选择 person/styles/ 下的风格档（每轮追加到用户 extra）。"""

        private: str = Field(
            default="05_sweet.md",
            description="私聊风格档文件名（persona/styles/ 下）。05=撒娇黏人；想保守可改 01_default.md",
            label="私聊风格",
            tag="text",
        )
        group: str = Field(
            default="01_default.md",
            description="群聊风格档文件名（persona/styles/ 下）。默认温柔和善",
            label="群聊风格",
            tag="text",
        )
        enabled: bool = Field(
            default=True,
            description="是否在每轮用户提示词 extra 注入所选风格档",
            label="注入风格",
            tag="text",
        )


    # ===================== 记忆（并入自 cyrene_memory） =====================

    @config_section("dfc_memory")
    class DfcMemorySection(SectionBase):
        enabled: bool = Field(default=True, description="是否启用记忆（语义召回/关系/WorldBook/画像注入）")
        debug_log: bool = Field(default=False, description="记忆注入调试日志", tag="debug")

    @config_section("memory_scope")
    class MemoryScopeSection(SectionBase):
        private_enabled: bool = Field(default=True, description="私聊（含 NFC 私聊桥）是否启用完整记忆")
        group_enabled: bool = Field(default=False, description="群聊是否启用记忆注入（保持 false 用原记忆系统）")
        user_ids: list[str] = Field(default_factory=list, description="仅对名单内用户生效；留空=所有私聊", tag="list")
        nfc_bridge: bool = Field(default=True, description="接受 NFC 兼容桥（on_prompt_build 无 chat_type、带 content）按私聊处理")

    @config_section("memory_templates")
    class MemoryTemplatesSection(SectionBase):
        user_prompts: list[str] = Field(
            default_factory=lambda: ["default_chatter_user_prompt"],
            description="注入记忆的目标用户模板名（dfc 与 NFC 默认注入点同名；NFC 若改 flashback.injection_point 需在此补名）",
            tag="list",
        )

    @config_section("memory_inject")
    class MemoryInjectSection(SectionBase):
        profile: bool = Field(default=True, description="注入长期画像")
        recall: bool = Field(default=True, description="注入语义召回旧记忆")
        care_cue: bool = Field(default=True, description="注入最近的关系/关怀提示")
        worldbook: bool = Field(default=True, description="注入触发式背景（WorldBook）")
        top_k: int = Field(default=3, ge=1, le=8, description="召回条数")
        min_score: float = Field(default=0.18, ge=0.0, le=1.0, description="召回最低余弦相似度")
        per_text_chars: int = Field(default=220, ge=40, le=600, description="每条记忆注入字数上限")
        total_chars: int = Field(default=2200, ge=200, le=6000, description="记忆板块总字数上限")
        worldbook_max: int = Field(default=2, ge=1, le=5, description="单轮 WorldBook 条数上限")
        inject_mode: str = Field(
            default="extra",
            description="extra=写入 values.extra；append_template=追加模板末尾（kfc/NFC 无 extra 模板时用）",
            input_type="select", choices=["extra", "append_template"],
        )
        nfc_system_override: bool = Field(default=False, description="生成 NFC system_prompt_override 建议文件（不自动改 NFC 配置）")

    @config_section("dfc_memory_write")
    class DfcMemoryWriteSection(SectionBase):
        enabled: bool = Field(default=True, description="自动写入（Cyrene 式）：私聊互动后自动沉淀关系/记忆到 cyrene 存储（独立于 booku_memory）")
        task: str = Field(default="utils_small", description="抽取使用的 LLM 模型任务")
        interval_turns: int = Field(default=6, ge=1, le=200, description="每 N 次私聊回合处理后执行一次写入")
        min_interval_seconds: int = Field(default=600, ge=0, le=86400, description="两次写入最小间隔（秒），防止频繁调用")
        enable_relationship: bool = Field(default=True, description="写入 relationship（情绪/重要时刻/关怀提示）")
        enable_entries: bool = Field(default=True, description="把值得长期记住的话写入语义记忆（自动向量化）")
        max_entry_chars: int = Field(default=240, ge=40, le=600, description="单条语义记忆字数上限")
        dedupe_min_score: float = Field(default=0.86, ge=0.0, le=1.0, description="与已有记忆相似度低于该值才写入（去重）")

    @config_section("memory_store")
    class MemoryStoreSection(SectionBase):
        data_dir: str = Field(default="data/cyrene_memory", description="记忆数据目录")
        source_dir: str = Field(default="", description="Cyrene 数据源目录（留空取 %APPDATA%/live2d-cyrene）")
        auto_migrate: bool = Field(default=True, description="首次加载且库为空时自动迁移")
        worldbook_dir: str = Field(default="F:/昔涟agent/prompts/worldbook", description="WorldBook md 目录")

    dfc_plugin: DfcPluginSection = Field(default_factory=DfcPluginSection)
    persona: PersonaSection = Field(default_factory=PersonaSection)
    style: StyleSection = Field(default_factory=StyleSection)
    dfc_memory: DfcMemorySection = Field(default_factory=DfcMemorySection)
    memory_scope: MemoryScopeSection = Field(default_factory=MemoryScopeSection)
    memory_templates: MemoryTemplatesSection = Field(default_factory=MemoryTemplatesSection)
    memory_inject: MemoryInjectSection = Field(default_factory=MemoryInjectSection)
    dfc_memory_write: DfcMemoryWriteSection = Field(default_factory=DfcMemoryWriteSection)
    memory_store: MemoryStoreSection = Field(default_factory=MemoryStoreSection)


# 兼容旧代码/旧配置的别名
CyreneConfig = CyreneNeoConfig

