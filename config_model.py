from __future__ import annotations

from typing import Callable, Literal, TypeVar

from maibot_sdk import Field, PluginConfigBase

ConfigSection = TypeVar("ConfigSection", bound=type[PluginConfigBase])


def ui_labels(**labels: str) -> Callable[[ConfigSection], ConfigSection]:
    """声明配置页展示用的中文字段名，配置文件键名保持不变。"""

    def decorate(config_class: ConfigSection) -> ConfigSection:
        # SDK 从 Field 的 json_schema_extra 读取 label，因此无需依赖 MaiBot 主程序改动。
        for field_name, label in labels.items():
            field_info = config_class.model_fields[field_name]
            field_extra = dict(field_info.json_schema_extra or {})
            field_extra["label"] = label
            field_info.json_schema_extra = field_extra
        return config_class

    return decorate


@ui_labels(enabled="启用插件", config_version="配置版本")
class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "sparkles"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="启用抖音冲浪与分享功能")
    config_version: str = Field(default="1.2.9", description="配置版本")


@ui_labels(values="筛选底线", reply_style="分享文案风格")
class IdentitySection(PluginConfigBase):
    __ui_label__ = "筛选与分享文案"
    __ui_icon__ = "message-circle"
    __ui_order__ = 1

    values: str = Field(default="只分享来源明确、适合当前聊天环境的内容；拒绝恶意引流、虚假夸张和不安全内容。", description="内容筛选底线")
    reply_style: str = Field(default="自然、简短、具体；不编造事实，不使用攻击性表达。", description="自动分享附言的表达方式")


@ui_labels(
    active_hours="工作时段",
    startup_delay_seconds="启动延迟秒数",
    interval_minutes="扫描间隔分钟",
    directions_per_cycle="每轮标签数量",
    search_results_per_query="每标签候选数",
    curator_model="筛选文本模型（自定义填“文本模型直连”）",
    vision_model="筛选视觉模型（自定义填“视觉模型直连”）",
    retry_backoff_minutes="失败退避分钟",
    batch_cooldown_minutes="分批筛选冷却分钟",
    max_candidates_per_batch="单批候选上限",
    candidate_inventory_pause_at="候选库存上限",
    candidate_inventory_resume_below="候选补货下限",
    replenish_cycle_pause_seconds="连续补货间隔秒数",
)
class SurfSettings(PluginConfigBase):
    """自动冲浪的固定参数。"""

    enabled: bool = Field(default=False, description="定时浏览抖音并建立候选库")
    active_hours: str = Field(default="09:00-23:00", description="自动冲浪允许工作的本地时间段")
    startup_delay_seconds: int = Field(default=180, ge=0, description="重启后首次冲浪的等待秒数")
    interval_minutes: int = Field(default=30, ge=5, description="普通扫描间隔分钟数")
    directions_per_cycle: int = Field(default=3, ge=1, le=10, description="每轮抽取的标签数")
    search_results_per_query: int = Field(default=2, ge=1, le=20, description="每个标签进入候选池的最大结果数")
    curator_model: str = Field(default="utils", description="从 MaiBot 已配置任务中选择", json_schema_extra={"x-widget": "select", "hint": "选“自定义 API”后，到“模型与维护”标签页填写“文本模型直连”。"})
    vision_model: str = Field(default="vlm", description="识别视频截图、封面时使用的视觉模型任务；默认使用 MaiBot 的 vlm", json_schema_extra={"x-widget": "select", "hint": "选“自定义 API”后，到“模型与维护”标签页填写“视觉模型直连”。"})
    retry_backoff_minutes: int = Field(default=15, ge=1, description="模型失败后的退避分钟数")
    batch_cooldown_minutes: int = Field(default=10, ge=0, description="候选分批筛选的冷却分钟数")
    max_candidates_per_batch: int = Field(default=8, ge=1, le=30, description="单次交给模型的最大候选数")
    candidate_inventory_pause_at: int = Field(default=30, ge=1, description="候选库存达到该数值时暂停补货")
    candidate_inventory_resume_below: int = Field(default=10, ge=0, description="候选库存不高于该数值时恢复补货")
    replenish_cycle_pause_seconds: int = Field(default=2, ge=1, le=300, description="连续补货时两轮之间的间隔秒数")


@ui_labels(enabled="启用自动冲浪")
class SurfSection(SurfSettings):
    __ui_label__ = "自动冲浪"
    __ui_icon__ = "globe"
    __ui_order__ = 2


@ui_labels(api_base_url="API 地址", api_key="API 密钥", model_name="模型名", temperature="生成温度", max_tokens="最大输出 token", timeout_seconds="请求超时（秒）", extra_json="额外 JSON 参数")
class DirectTextModelSection(PluginConfigBase):
    """供筛选文本模型和手动短评文本模型选择“自定义 API”时填写。"""

    __ui_label__ = "文本模型直连（筛选 / 手动短评）"
    __ui_icon__ = "link"
    __ui_order__ = 3

    api_base_url: str = Field(default="", description="OpenAI 兼容 API 地址，例如 https://api.example.com/v1")
    api_key: str = Field(default="", description="调用 API 使用的密钥", json_schema_extra={"input_type": "password"})
    model_name: str = Field(default="", description="服务商实际模型名，例如 gpt-4.1-mini")
    temperature: float = Field(default=0.2, ge=0, le=2, description="筛选内容时的生成温度")
    max_tokens: int = Field(default=2800, ge=100, le=16000, description="单次请求最大输出 token")
    timeout_seconds: int = Field(default=300, ge=10, le=600, description="单次 API 请求最长等待秒数")
    extra_json: str = Field(default="{}", description="附加到请求体的 JSON 对象，例如 {\"thinking\":{\"type\":\"disabled\"}}", json_schema_extra={"x-widget": "textarea", "rows": 3})


@ui_labels(api_base_url="API 地址", api_key="API 密钥", model_name="视觉模型名", temperature="生成温度", max_tokens="最大输出 token", timeout_seconds="请求超时（秒）", extra_json="额外 JSON 参数")
class DirectVisionModelSection(DirectTextModelSection):
    """供筛选视觉模型和手动短评视觉核验选择“自定义 API”时填写；模型必须支持图片输入。"""

    __ui_label__ = "视觉模型直连（筛选 / 手动短评核验）"
    __ui_icon__ = "image"
    __ui_order__ = 4


@ui_labels(
    min_like_count="最低点赞数",
    min_comment_count="最低评论数",
    min_collect_count="最低收藏数",
    min_share_count="最低转发数",
    allow_douyin_notes="允许抖音图文",
    max_video_duration_seconds="候选视频最长秒数",
    max_publish_age_days="候选最远天数",
)
class CandidateFilterSection(PluginConfigBase):
    """综合搜索和推荐流共用的互动数据与发布时间门槛。"""

    __ui_label__ = "候选筛选"
    __ui_icon__ = "filter"
    __ui_order__ = 3

    min_like_count: int = Field(default=5000, ge=0, description="候选至少需要的点赞数；0 为不限制")
    min_comment_count: int = Field(default=0, ge=0, description="候选至少需要的评论数；0 为不限制")
    min_collect_count: int = Field(default=0, ge=0, description="候选至少需要的收藏数；0 为不限制")
    min_share_count: int = Field(default=0, ge=0, description="候选至少需要的转发数；0 为不限制")
    allow_douyin_notes: bool = Field(default=False, description="是否将抖音图文笔记纳入候选；关闭后只收录和分享视频")
    max_video_duration_seconds: int = Field(default=180, ge=1, le=3600, description="只候选不超过该时长的抖音视频；单位：秒")
    max_publish_age_days: int = Field(
        default=0,
        ge=0,
        description="候选距今天最多多少天；0 为不限制，30 表示只候选最近 30 天内发布的视频",
    )


@ui_labels(
    like_weight="点赞权重",
    comment_weight="评论权重",
    collect_weight="收藏权重",
    share_weight="转发权重",
    recency_weight="发布日期权重",
    ai_weight="AI 内容权重",
    like_target="点赞满分参考值",
    comment_target="评论满分参考值",
    collect_target="收藏满分参考值",
    share_target="转发满分参考值",
    recency_window_days="发布日期满分天数",
)
class ScoringSection(PluginConfigBase):
    """将抓取互动数据和深读模型判断合成为最终分享分。"""

    __ui_label__ = "候选评分"
    __ui_icon__ = "chart"
    __ui_order__ = 4

    like_weight: float = Field(default=0.18, ge=0, le=1, description="点赞子分的权重")
    comment_weight: float = Field(default=0.12, ge=0, le=1, description="评论子分的权重")
    collect_weight: float = Field(default=0.12, ge=0, le=1, description="收藏子分的权重")
    share_weight: float = Field(default=0.13, ge=0, le=1, description="转发子分的权重")
    recency_weight: float = Field(default=0.10, ge=0, le=1, description="发布日期子分的权重")
    ai_weight: float = Field(default=0.35, ge=0, le=1, description="AI 深读内容判断的权重")
    like_target: int = Field(default=100000, ge=1, description="达到该点赞数时点赞子分为满分；对数曲线计算")
    comment_target: int = Field(default=10000, ge=1, description="达到该评论数时评论子分为满分；对数曲线计算")
    collect_target: int = Field(default=10000, ge=1, description="达到该收藏数时收藏子分为满分；对数曲线计算")
    share_target: int = Field(default=10000, ge=1, description="达到该转发数时转发子分为满分；对数曲线计算")
    recency_window_days: int = Field(default=30, ge=1, description="当天发布为时效满分，超过该天数时效子分为 0")



@ui_labels(
    enabled="启用该聊天流",
    tags="聊天流标签",
    active_hours="分享时段",
    min_quiet_minutes="最短静默分钟",
    cooldown_hours="分享冷却小时",
    daily_limit="每日分享上限",
    candidate_inventory_pause_at="候选库存上限",
    candidate_inventory_resume_below="候选补货下限",
)
class ChatSharingRule(PluginConfigBase):
    """单个 QQ 群聊或私聊的独立候选及分享规则。"""

    target_type: Literal["group", "private"] = Field(default="group", description="分享目标类型：group 为群聊，private 为私聊")
    target_id: str = Field(default="", min_length=1, description="群聊时填写群号；私聊时填写 QQ 号")
    enabled: bool = Field(default=True, description="启用该聊天流的自动分享")
    tags: list[str] = Field(default_factory=lambda: ["感觉至上", "二次元"], description="这个聊天流感兴趣的标签")
    active_hours: str = Field(default="09:00-23:00", description="该聊天流允许自动分享的时间段")
    min_quiet_minutes: int = Field(default=8, ge=0, description="距离最后一条消息至少安静多少分钟后，才允许自动分享；设为 0 表示即时模式，不因新群消息暂缓（单位：分钟）")
    cooldown_hours: float = Field(default=1.0, ge=0.0833333333, description="两次自动分享的最短间隔小时数；最小 5 分钟")
    daily_limit: int = Field(default=0, ge=0, description="每天最多自动分享次数；0 为不限")
    candidate_inventory_pause_at: int = Field(default=30, ge=1, description="该聊天流候选上限")
    candidate_inventory_resume_below: int = Field(default=10, ge=0, description="该聊天流候选低于该数值时补货")


@ui_labels(
    enabled="启用主动分享",
    declined_share_cooldown_minutes="候选暂缓分钟",
    declined_share_max_attempts="候选最大尝试次数",
    minimum_share_score="最低分享质量分",
    candidate_selection_mode="候选发送顺序",
    forward_body_max_chars="原帖摘要最大字符",
    screenshot_enabled="允许附带截图",
    screenshot_probability="附图概率",
    screenshot_max_bytes="截图最大字节数",
    douyin_video_forward_enabled="允许转发抖音视频",
    video_sender_adapter="视频发送适配器",
    douyin_video_max_bytes="视频最大字节数",
    reaction_window_minutes="反馈记录窗口分钟",
)
class SharingSettings(PluginConfigBase):
    enabled: bool = Field(default=False, description="允许自动分享已筛选的抖音候选")
    declined_share_cooldown_minutes: int = Field(default=60, ge=1, description="模型未发送候选后的再次尝试等待分钟数")
    declined_share_max_attempts: int = Field(default=2, ge=1, description="同一候选最多尝试次数")
    minimum_share_score: float = Field(default=0.60, ge=0, le=1, description="进入自动分享队列的最低质量分")
    candidate_selection_mode: Literal["随机发送", "最高分优先", "最早收录优先"] = Field(
        default="最高分优先",
        description="达到最低质量分后，选择下一条候选的顺序；随机发送、最高分优先或最早收录优先",
    )
    forward_body_max_chars: int = Field(default=360, ge=80, description="原帖摘要最大字符数")
    screenshot_enabled: bool = Field(default=True, description="允许为非视频内容附带截图")
    screenshot_probability: float = Field(default=0.9, ge=0, le=1, description="满足条件时附图概率")
    screenshot_max_bytes: int = Field(default=5_000_000, ge=100_000, description="截图最大字节数")
    douyin_video_forward_enabled: bool = Field(default=True, description="允许下载并通过选定适配器发送到 QQ 群；不支持时自动只发链接")
    video_sender_adapter: Literal["自动识别", "NapCat", "SnowLuma"] = Field(
        default="自动识别",
        description="QQ 群原生视频的发送适配器；自动识别会依次尝试 NapCat 和 SnowLuma",
    )
    douyin_video_max_bytes: int = Field(default=11_500_000, ge=1_000_000, le=80_000_000, description="允许转发的视频最大字节数；NapCat 的整帧限制会使视频 Base64 膨胀，QQ 群安全上限约 11500000，超过时插件会自动按此上限处理")
    reaction_window_minutes: int = Field(default=120, ge=1, description="记录分享反馈的时间窗口分钟数")


@ui_labels(enabled="启用主动分享", stream_configs="聊天流独立规则")
class SharingSection(SharingSettings):
    __ui_label__ = "主动分享"
    __ui_icon__ = "send"
    __ui_order__ = 4

    stream_configs: list[ChatSharingRule] = Field(default_factory=list, description="点击添加项目后，选择群聊或私聊，再填写对应群号或 QQ 号")


@ui_labels(target_type="目标类型", target_id="群号或 QQ 号")
class CommandAccessRule(PluginConfigBase):
    """一条可使用抖音交互命令的 QQ 群聊或私聊授权。"""

    target_type: Literal["group", "private"] = Field(default="group", description="group 为群聊，private 为私聊")
    target_id: str = Field(default="", min_length=1, description="群聊时填写群号；私聊时填写 QQ 号")


@ui_labels(enabled="启用指令白名单", allowed_targets="允许使用指令的目标")
class CommandAccessSection(PluginConfigBase):
    """限制会发起浏览或打开浏览器的交互命令。"""

    __ui_label__ = "指令权限"
    __ui_icon__ = "shield-check"
    __ui_order__ = 5

    enabled: bool = Field(default=True, description="开启后，只有下方列出的 QQ 群聊或私聊能使用抖音搜索、冲浪和浏览器登录命令")
    allowed_targets: list[CommandAccessRule] = Field(default_factory=list, description="点击添加项目后，选择群聊或私聊，再填写对应群号或 QQ 号；未添加任何目标时禁止这些指令")


@ui_labels(
    enabled="启用抖音浏览器",
    page_timeout_seconds="页面超时秒数",
    max_text_chars="页面最大读取字符",
    native_site_browsing_enabled="使用登录态真实浏览",
    douyin_search_retry_count="搜索重试次数",
    douyin_recommendation_cards_per_cycle="每轮推荐流作品数",
    douyin_recommendation_candidates_per_cycle="每轮视觉初筛上限",
    douyin_keywords_per_cycle="每轮搜索标签数",
    auto_douyin_min_results_before_scroll="自动搜索首屏最低结果",
    auto_douyin_scroll_rounds="自动搜索下拉次数",
    manual_douyin_search_results="手动搜索结果上限",
    manual_douyin_target_results="手动搜索目标有效视频数",
    manual_douyin_search_timeout_seconds="手动搜索最长秒数",
    manual_douyin_comment_thinking_enabled="手动短评使用模型思考",
    manual_douyin_comment_model="手动短评文本模型（自定义填“文本模型直连”）",
    manual_douyin_visual_check_enabled="手动短评视觉核验",
    manual_douyin_visual_model="手动短评视觉模型（自定义填“视觉模型直连”）",
    manual_douyin_visual_frame_samples="手动短评视觉核验帧数",
    allowed_domains="允许访问域名",
    login_pages="登录页地址",
)
class BrowserSettings(PluginConfigBase):
    enabled: bool = Field(default=True, description="使用插件专用 Chrome 档案浏览抖音")
    page_timeout_seconds: int = Field(default=45, ge=5, description="单页面读取超时秒数")
    max_text_chars: int = Field(default=30000, ge=1000, description="单页面最多读取字符数")
    native_site_browsing_enabled: bool = Field(default=True, description="使用已登录抖音页面真实浏览")
    douyin_search_retry_count: int = Field(default=1, ge=0, le=3, description="搜索结果不足时的重试次数")
    douyin_recommendation_cards_per_cycle: int = Field(default=20, ge=1, le=30, description="每轮推荐流划过的最大作品数")
    douyin_recommendation_candidates_per_cycle: int = Field(default=20, ge=1, le=30, description="每轮交给视觉初筛的最大作品数")
    douyin_keywords_per_cycle: int = Field(default=3, ge=1, le=6, description="每轮搜索的标签数")
    auto_douyin_min_results_before_scroll: int = Field(default=2, ge=1, le=20, description="自动搜索首屏最低结果数")
    auto_douyin_scroll_rounds: int = Field(default=4, ge=1, le=4, description="自动搜索补充下拉次数")
    manual_douyin_search_results: int = Field(default=12, ge=6, le=20, description="/抖音 最多保留多少条合格候选；会自动不低于目标有效视频数")
    manual_douyin_target_results: int = Field(default=10, ge=1, le=20, description="/抖音 在综合页累计到此数量的合格视频后停止下拉")
    manual_douyin_search_timeout_seconds: int = Field(default=300, ge=30, le=900, description="/抖音 整次搜索允许持续下拉的最长时间；到时从已有合格候选中挑选")
    manual_douyin_comment_thinking_enabled: bool = Field(default=False, description="开启后，/抖音 只对最终选中的作品调用一次文本模型，生成有内容依据的自然短评；关闭时沿用本地随机短评")
    manual_douyin_comment_model: str = Field(default="utils", description="开启手动短评模型思考后使用的文本模型任务", json_schema_extra={"x-widget": "select", "hint": "选“自定义 API”后，到“模型与维护”→“文本模型直连（筛选 / 手动短评）”填写地址、密钥和模型名。"})
    manual_douyin_visual_check_enabled: bool = Field(default=False, description="开启手动短评模型思考后，可额外抽取最终视频的代表画面交给视觉模型核验；视觉失败时仍继续文字短评")
    manual_douyin_visual_model: str = Field(default="vlm", description="手动短评视觉核验使用的模型任务", json_schema_extra={"x-widget": "select", "hint": "选“自定义 API”后，到“模型与维护”→“视觉模型直连（筛选 / 手动短评核验）”填写地址、密钥和模型名。"})
    manual_douyin_visual_frame_samples: int = Field(default=3, ge=1, le=4, description="每次视觉核验从最终视频均匀抽取的代表画面数；更多画面更完整，但会增加下载时间和视觉模型消耗")
    allowed_domains: list[str] = Field(default_factory=lambda: ["douyin.com"], description="允许访问的域名白名单")
    login_pages: list[str] = Field(default_factory=lambda: ["https://www.douyin.com/?recommend=1"], description="登录命令默认打开的推荐页")


@ui_labels(enabled="启用抖音浏览器")
class BrowserSection(BrowserSettings):
    __ui_label__ = "抖音浏览器"
    __ui_icon__ = "browser"
    __ui_order__ = 6



@ui_labels(
    enabled="启用自动清理",
    ordinary_candidate_days="未分享候选保留天数",
    dismissed_days="已筛除候选保留天数",
    shared_days="已分享记录保留天数",
)
class RetentionSettings(PluginConfigBase):
    enabled: bool = Field(default=True, description="每天清理超过保留期的候选和分享记录")
    ordinary_candidate_days: int = Field(default=1, ge=1, description="未分享候选保留天数")
    dismissed_days: int = Field(default=7, ge=1, description="已筛除候选保留天数")
    shared_days: int = Field(default=90, ge=1, description="已分享记录保留天数")


@ui_labels(enabled="启用自动清理")
class RetentionSection(RetentionSettings):
    __ui_label__ = "冲浪记录保留"
    __ui_icon__ = "archive"
    __ui_order__ = 7



@ui_labels(
    enabled="启用视频处理",
    frame_samples="普通视频抽帧数",
    douyin_frame_samples="抖音视频抽帧数",
    douyin_browser_first="抖音优先读取浏览器页面",
    max_subtitle_chars="视频文字最大字符",
)
class VideoSettings(PluginConfigBase):
    enabled: bool = Field(default=True, description="允许在浏览器无法读取详情时下载视频抽帧")
    frame_samples: int = Field(default=8, ge=1, le=20, description="普通视频抽帧数")
    douyin_frame_samples: int = Field(default=4, ge=1, le=20, description="抖音视频抽帧数")
    douyin_browser_first: bool = Field(default=True, description="抖音优先读取浏览器页面，关闭后才下载抽帧")
    max_subtitle_chars: int = Field(default=50000, ge=1000, description="视频文字信息最大字符数")


@ui_labels(enabled="启用视频处理")
class VideoSection(VideoSettings):
    __ui_label__ = "视频处理"
    __ui_icon__ = "video"
    __ui_order__ = 8



@ui_labels(
    plugin="插件基础设置",
    identity="筛选与分享文案",
    surf="自动冲浪",
    direct_text_model="文本模型直连（筛选 / 手动短评）",
    direct_vision_model="视觉模型直连（筛选 / 手动短评核验）",
    candidate_filter="候选筛选",
    scoring="候选评分",
    sharing="主动分享",
    command_access="指令权限",
    browser="抖音浏览器",
    video="视频处理",
    retention="冲浪记录保留",
)
class DouyinSurfConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    identity: IdentitySection = Field(default_factory=IdentitySection)
    surf: SurfSection = Field(default_factory=SurfSection)
    direct_text_model: DirectTextModelSection = Field(default_factory=DirectTextModelSection)
    direct_vision_model: DirectVisionModelSection = Field(default_factory=DirectVisionModelSection)
    candidate_filter: CandidateFilterSection = Field(default_factory=CandidateFilterSection)
    scoring: ScoringSection = Field(default_factory=ScoringSection)
    sharing: SharingSection = Field(default_factory=SharingSection)
    command_access: CommandAccessSection = Field(default_factory=CommandAccessSection)
    browser: BrowserSection = Field(default_factory=BrowserSection)
    video: VideoSection = Field(default_factory=VideoSection)
    retention: RetentionSection = Field(default_factory=RetentionSection)
