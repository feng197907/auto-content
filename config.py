"""自动化内容矩阵配置。

改这个文件就能换 AI 模型、换选题方向、换写作风格，不用动核心代码。
"""

import os
from pathlib import Path
from typing import Any

# ---- 读取项目根目录的 .env 文件（标准库实现，无需装依赖）----
# 格式：KEY=VALUE，一行一个。密钥放这里，不写进代码、不进 git。
ENV_PATH = Path(__file__).resolve().parent / ".env"


def load_env_file(path: Path) -> None:
    """把 .env 里的 KEY=VALUE 注入环境，已存在的环境变量优先。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


load_env_file(ENV_PATH)

# ---- AI 接口（全部 OpenAI 兼容，只换地址+模型名即可）----
# 预设了几家，默认用【智谱 GLM-4-Flash：永久免费】，不用充钱。
# 想换平台，只改 AI_PROVIDER 一行即可（key 从对应环境变量自动读取）。
AI_PROVIDERS: dict[str, dict[str, str]] = {
    "zhipu": {
        "name": "智谱 GLM-Flash（永久免费）",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        # 生产实测：4.7 在当前 IP 持续 429 限流，主用稳定的老版；
        # 老版万一限流则降级到 4.7。两者都免费。想换回 4.7 主用，对调这两行即可。
        "model": "glm-4-flash",
        "fallback_model": "glm-4.7-flash",
        "key_env": "ZHIPU_API_KEY",
    },
    "siliconflow": {
        "name": "硅基流动（注册送额度+免费模型）",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "key_env": "SILICONFLOW_API_KEY",
    },
    "deepseek": {
        "name": "DeepSeek（低价，送 500 万 token）",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
    },
}
AI_PROVIDER = "zhipu"  # 可选：zhipu / siliconflow / deepseek

_provider = AI_PROVIDERS[AI_PROVIDER]
AI_BASE_URL = _provider["base_url"]
AI_MODEL = _provider["model"]
AI_FALLBACK_MODEL = _provider.get("fallback_model", "")  # 限流时降级用

# 读 key 的三种方式（按优先级）：
#   1. 项目根目录 .env 文件里的 ZHIPU_API_KEY（推荐，重启也生效）
#   2. 系统环境变量
#   3. 直接填在下面的 AI_API_KEY 字符串里
# 全部留空 → 系统走"真实素材卡"模式，不编造任何正文。
AI_API_KEY = os.getenv(_provider["key_env"], "") or os.getenv("AI_API_KEY", "")

# 每次生成的文章数量（建议 3-5，精修时够挑）
BATCH_SIZE = 5

# ---- 写作方向 ----
# 这里决定写什么。越具体，AI 写出来越能用。
WRITING_NICHE = "AI 与数码科技资讯：新工具、模型发布、芯片手机、科技动态与实用解读"
WRITING_STYLE = (
    "写给关注科技的普通读者和从业者。开头用一两句说清这件事是什么、为什么值得看；"
    "中间讲清楚关键事实、背后的逻辑，以及读者真正该关心的那一点，"
    "不要机械套用'亮点/局限/影响/清单'这类模板，那样写出来全是空话；"
    "必须有增量信息或明确的判断，不能只是复述素材。"
    "必须基于真实素材，不确定的地方直说不确定。语气像懂行的朋友在聊天，"
    "不要参数堆砌，不要标题党和营销腔（不用'揭秘''震惊''炸裂''盛宴'这类词）。"
)

# ---- 热榜选题 ----
# 抓全网实时热榜，再用下面的关键词筛出与本服务领域相关的。
# 关掉后只用备用关键词（FALLBACK_TOPICS）。
USE_HOT_TOPICS = True

# 领域关键词：命中"强相关"的话题优先，弱相关次之，完全不相关的丢弃。
# 按你的写作方向调整，这套是针对"AI/数码科技资讯"主题的。
KEYWORDS_STRONG = [
    "AI", "人工智能", "大模型", "ChatGPT", "GPT", "OpenAI", "DeepSeek",
    "豆包", "文心", "通义", "智谱", "Sora", "芯片", "显卡", "英伟达",
    "手机", "华为", "小米", "苹果手机", "iPhone", "发布会", "鸿蒙", "机器人",
]
KEYWORDS_WEAK = [
    "科技", "数码", "互联网", "软件", "系统", "更新", "智能", "算法",
    "数据流", "算力", "云计算", "自动驾驶", "新能源", "电池", "屏幕",
    "相机", "拍照", "性能", "应用", "平台", "开源", "代码",
]

# ---- 选题源（RSS）----
# 全部为国内站点，服务器境内网络实测可正常解析、带真实摘要和原文链接。
# 顺序即优先级，pipeline 会按源轮转取，保证来源多样而不被单一源占满。
# 已排除：HuggingFace/V2EX（连不上）、知乎（401）、微博（403）、
#         掘金（接口返回 0 条）、虎嗅（超时）、品玩/机器之心（非标准 RSS）、
#         InfoQ（摘要只有"点击查看原文"）——都实测过，不予采用。
TOPIC_SOURCES: list[dict[str, str]] = [
    {"name": "IT之家", "url": "https://www.ithome.com/rss/"},
    {"name": "开源中国", "url": "https://www.oschina.net/news/rss"},
    {"name": "雷峰网", "url": "https://www.leiphone.com/feed"},
    {"name": "爱范儿", "url": "https://www.ifanr.com/feed"},
    {"name": "博客园", "url": "https://www.cnblogs.com/rss"},
    {"name": "量子位", "url": "https://www.qbitai.com/feed"},
]

# 掘金技术热榜：国内开发者社区实时榜，纯技术内容（带热度/阅读量）
# 注：CSDN 热榜接口有反爬，实测短时间多次请求即 521，故不用
USE_JUEJIN_TOPICS = True

# GitHub 热门项目作为兜底选题源（调官方 Search API）。
# 境外站点，服务器实测时通时不通，故只在国内源都不够时补位，不再优先。
USE_GITHUB_TOPICS = True

# 备用选题关键词：热榜抓不到相关话题时，用这些让 AI 自己出题
FALLBACK_TOPICS: list[str] = [
    "普通用户现在最值得用的 AI 工具有哪些",
    "大模型能力又升级了，对我们的实际影响是什么",
    "买新手机别只看参数，这几个体验才是关键",
    "AI 生成内容怎么分辨真假，实用辨别方法",
    "国产芯片和系统的最新进展意味着什么",
]

# ---- 审核后台 ----
APP_HOST = "0.0.0.0"
APP_PORT = 5000
# 会话签名密钥：登录一次后记住状态用的。生产建议在 .env 里设一个随机串。
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")

# 公网访问密码：部署到服务器后必须设，否则审核页裸奔
# 微信推送链接会自动带 ?pwd=xxx，点开直接进；本地开发可留空
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

# 审核页公网地址：推送到微信的链接就指向这里，带密码
# 例：https://your-domain.com/?pwd=你的密码
REVIEW_URL = os.getenv("REVIEW_URL", "")

# ---- 微信推送（Server酱）----
# key 放 .env 的 SERVERCHAN_KEY，这里读出来供 notify.py 用
SERVERCHAN_KEY = os.getenv("SERVERCHAN_KEY", "")

# ---- 队列存储 ----
QUEUE_FILE = "data/queue.json"

# ---- 稿件保留期（天）----
# 待审核/已退回：超过 N 天未处理，自动删除（避免垃圾堆积）
KEEP_PENDING_DAYS = 3
# 已通过/已发布：超过 N 天自动删除（按通过时间 approved_at 算）
KEEP_APPROVED_DAYS = 30

# 发布目标平台（仅用于成品标注，不做自动发布）
TARGET_PLATFORMS = ["头条号", "百家号", "公众号"]

# 成品预览里显示的署名
SITE_AUTHOR = "远方守护"
