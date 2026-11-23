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
        # 2026-11-19 复测：4.7 已恢复可用，且结构遵循度明显好于老版
        # （老版会写出"什么情况？""该怎么办？"这种设问小标题，还自造命令）。
        # 故主用 4.7，限流时降级到老版 glm-4-flash。两者都免费。
        "model": "glm-4.7-flash",
        "fallback_model": "glm-4-flash",
        "key_env": "ZHIPU_API_KEY",
    },
    "siliconflow": {
        "name": "硅基流动（注册送额度+免费模型）",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "key_env": "SILICONFLOW_API_KEY",
    },
    "deepseek": {
        "name": "DeepSeek（按量付费，flash 档最便宜）",
        # 2026-11-20 对照官方文档核对过两个变化，老教程写的都是错的：
        # ① base_url 不需要 /v1，SDK/手写请求都直接拼 /chat/completions；
        # ② deepseek-chat 和 deepseek-reasoner 已于 2026-07-24 下线，
        #    现在只剩 deepseek-flash（V4.1-Flash）和 deepseek-v4-pro 两个名字，
        #    填旧名会返回 400 model not found。
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        # 故意不设备用模型：DeepSeek 的 429 只在并发超限时出现（2500 并发），
        # 咱们每天 5 篇根本碰不到；而降级到 v4-pro 单价贵 4 倍多，
        # 宁可这一篇失败退回素材卡，也不要限流时静默烧钱。
        # 真要兜底，把下面改成 "deepseek-v4-pro" 即可。
        "fallback_model": "",
        "key_env": "DEEPSEEK_API_KEY",
    },
}

# 换平台不用改代码：在 .env 里写 AI_PROVIDER=deepseek 就行。
# 写错名字不至于让服务起不来，打一行日志回退到默认的智谱。
AI_PROVIDER = os.getenv("AI_PROVIDER", "zhipu")
if AI_PROVIDER not in AI_PROVIDERS:
    print(f"[config] AI_PROVIDER={AI_PROVIDER} 不认识，回退 zhipu（可选：{'/'.join(AI_PROVIDERS)}）")
    AI_PROVIDER = "zhipu"

# 深度思考开关。智谱 4.7 和 DeepSeek V4.1 都默认开启思考模式：
# 写稿是"按模板把素材讲清楚"，不需要推理，开着只会更慢、更贵、还容易把思考过程写进正文。
# 默认关；想让它先想再写就在 .env 里写 AI_THINKING=1。
AI_THINKING = os.getenv("AI_THINKING", "0") == "1"

_provider = AI_PROVIDERS[AI_PROVIDER]
AI_BASE_URL = _provider["base_url"]
AI_MODEL = _provider["model"]
AI_FALLBACK_MODEL = _provider.get("fallback_model", "")  # 同平台备用模型（限流时降级）

# ---- 跨平台兜底 ----
# 为什么需要它：智谱免费档约 1 QPS，早高峰必 429；原先的兜底是同平台 glm-4-flash，
# 它写出过"懒人养生，真的有效吗？"这种设问标题，比不写还糟。
# 现在改成：4.7 限流/报错时，直接换 DeepSeek 的付费模型重试一次。
# 只有限流时段才产生费用，按每天 5 篇算每月仍是个位数。
# 想关掉就留空；想换回弱模型兜底，把 AI_FALLBACK_PROVIDER 留空即可（走上面的 AI_FALLBACK_MODEL）。
AI_FALLBACK_PROVIDER = os.getenv("AI_FALLBACK_PROVIDER", "").strip()
if AI_FALLBACK_PROVIDER and AI_FALLBACK_PROVIDER not in AI_PROVIDERS:
    print(f"[config] AI_FALLBACK_PROVIDER={AI_FALLBACK_PROVIDER} 不认识，已忽略")
    AI_FALLBACK_PROVIDER = ""

_fb_provider = AI_PROVIDERS.get(AI_FALLBACK_PROVIDER) if AI_FALLBACK_PROVIDER else None
AI_FALLBACK_BASE_URL = _fb_provider["base_url"] if _fb_provider else ""
# 允许只改模型名（比如兜底也用 flash，但想换 v4-pro 时）
AI_FALLBACK_MODEL = (
    os.getenv("AI_FALLBACK_MODEL", "").strip() or (_fb_provider["model"] if _fb_provider else AI_FALLBACK_MODEL)
)
AI_FALLBACK_API_KEY = os.getenv(_fb_provider["key_env"], "") if _fb_provider else ""

# 单次请求超时（秒）。原来 120 秒够用，改成长稿（1300-1700 字）后
# 4.7 生成一篇常要 2 分钟以上，120 秒会直接 Read timeout，白白丢一版稿。
AI_TIMEOUT = int(os.getenv("AI_TIMEOUT", "240"))

# 读 key 的三种方式（按优先级）：
#   1. 项目根目录 .env 文件里的 ZHIPU_API_KEY（推荐，重启也生效）
#   2. 系统环境变量
#   3. 直接填在下面的 AI_API_KEY 字符串里
# 全部留空 → 系统走"真实素材卡"模式，不编造任何正文。
AI_API_KEY = os.getenv(_provider["key_env"], "") or os.getenv("AI_API_KEY", "")

# 头图走智谱 CogView，DeepSeek 不生图。所以生图 key 永远取智谱那把，
# 跟 AI_PROVIDER 解耦——否则哪天把主力换成 DeepSeek，头图会静默失效。
ZHIPU_IMAGE_KEY = os.getenv("ZHIPU_API_KEY", "") or os.getenv("AI_API_KEY", "")


def ai_targets() -> list[dict[str, str]]:
    """按顺序返回要尝试的模型：主模型 → 兜底模型。

    配了 AI_FALLBACK_PROVIDER 就跨平台兜底（智谱→DeepSeek），
    没配就退回同平台的备用模型（4.7→glm-4-flash）。
    用列表而不是单个 (model) 字符串，是因为跨平台时 base_url 和 key 都要跟着换，
    只传模型名会把 DeepSeek 的模型名发到智谱的接口上去。
    """
    targets: list[dict[str, str]] = [
        {
            "provider": AI_PROVIDER,
            "base_url": AI_BASE_URL,
            "model": AI_MODEL,
            "api_key": AI_API_KEY,
        }
    ]
    # 跨平台兜底：平台、地址、key 一起换
    if AI_FALLBACK_PROVIDER and AI_FALLBACK_API_KEY:
        targets.append(
            {
                "provider": AI_FALLBACK_PROVIDER,
                "base_url": AI_FALLBACK_BASE_URL,
                "model": AI_FALLBACK_MODEL,
                "api_key": AI_FALLBACK_API_KEY,
            }
        )
    # 同平台备用模型：只在没配跨平台兜底时才用（否则会先掉进弱模型，兜底等于白配）
    elif AI_FALLBACK_MODEL and AI_FALLBACK_MODEL != AI_MODEL:
        targets.append(
            {
                "provider": AI_PROVIDER,
                "base_url": AI_BASE_URL,
                "model": AI_FALLBACK_MODEL,
                "api_key": AI_API_KEY,
            }
        )
    return [t for t in targets if t["api_key"]]

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

# ---- 稿件结构模板 ----
# 教训来源：2026-11-19 那篇 NGINX 漏洞稿，读者读完不知道该怎么办，
# 全文没有版本号、没有自查命令、三节内容互相重复。
# 这里把"结论先行 + 可执行"的结构写死进提示词，生成时逐条要求。
# 标题规则：决定打开率，单独抽出来供两个提示词共用
TITLE_RULE = """\
1. 第一行只写标题，12-20 字，不要加"标题："字样、不要加任何标记或书名号。
   四种公式选一种：
   ① 具体信息点——带版本号、金额、时间或数量（"升到 1.30.7 才算修完"）
   ② 数字盘点——"5 个免费工具，第 3 个我用了半年"
   ③ 反常识——先说大家默认的想法，再给出相反的事实
   ④ 利益直给——"三条命令，查你的服务器中没中招"
   标题里至少要有一个具体名词或数字，让人一眼看出跟自己有关。
   禁止"揭秘""震惊""速看""炸裂""马上删"这类诱导词。
   标题里出现的数字/金额必须和素材完全一致，拿不准就别在标题里写数字。"""

# ---- 标题三备选：换标题时一次出 3 个，让人挑而不是碰运气 ----
# 借鉴 Easel 的 hook-title-formulas：公式分类 + 一次只用一个公式 + 具体优于空泛。
# 但按本号调性裁剪：去掉小红书式的"谁懂啊""闭眼入"，保留专业号能用的四类。
TITLE_VARIANTS_RULE = """\
基于下面这篇文章的内容，给出 3 个备选标题。要求：

1. 三个标题必须分别用下面三个不同的公式，不要写同一个公式的三种变体：
   ① 具体信息点：带版本号、金额、时间或数量，让读者一眼知道具体是什么事
   ② 反差/反常识：先说大家默认的想法，再给出相反的事实，制造认知缺口
   ③ 利益直给：说清读者看完能得到什么（一个动作、一个判断、一个可避开的坑）
2. 每个标题 12-22 字，必须有具体名词或数字，禁止空泛的"值得关注""影响几何"。
3. 禁止诱导词：揭秘、震惊、速看、炸裂、马上删、惊呆、血亏、封神。
4. 标题里的数字必须能在下面的原文素材里找到，拿不准就别写数字。
5. 不要给文章换角度、不要改正文——只改标题本身。

输出格式（严格遵守，每行一个，不要编号、不要解释、不要空行）：
公式①的标题
公式②的标题
公式③的标题"""

# ---- 信息增量：解决"文章没营养" ----
# 借鉴 Easel 的选题评分思路，但用在正文：每节必须有读者拿得到的新东西，
# 而不是把同一件事换成三种说法。这条在生成和重写两个提示词里都注入。
INFO_GAIN_RULE = """\
【每节必须有信息增量，这是硬要求】
读者读完一节，必须拿到下面三类中的至少一样，否则这一节就是废话：
① 一个具体事实：素材里的数字、版本号、时间、机构名、产品名；
② 一个明确判断：这件事对谁有利、对谁有风险，你判断该怎么做（要说清依据）；
③ 一个可执行细节：读者马上能查、能改、能确认的具体动作或标准。
不要写"值得关注""影响深远""需要加强重视""让我们拭目以待"这类没有信息的话。
不要把素材里的一句话换三种说法凑三节——同一个事实全文只出现一次。
如果某个事实你不确定，就直说不确定，不要用模糊话把它包装成结论。
写完后自己逐节检查：遮住这一节，读者会不会漏掉任何有用的东西？不会，就删掉重写。"""

ARTICLE_SKELETON = """\
【结构模板，必须按此顺序写】
1. 开头一段（不超过 80 字）：钩子。直接对读者说话，让他三秒内意识到"这事跟我有关"。
   四种写法任选：① 反常识判断 ② 具体场景 ③ 数字冲击 ④ 直接提问。
   禁止用"近日""据悉""随着……的发展""在当今时代"开头。
2. 第二段：先给答案。一句话说清结论——是什么、影响谁、要不要处理。
   素材里有版本号/时间/数字就写具体值；没有就写"具体以官方公告为准"，绝对不许编。
3. 主体 3 个小标题（每个不超过 14 字，用"## "开头）。标题必须是陈述句，
   禁止设问句——不许写"这事儿有多严重？""背后逻辑是什么？"这类不回答问题的空壳问句。
4. 每个小标题下面写 300-450 字，分 2-4 段：
   - 第一句用**加粗**写，一句话点明这一节结论；
   - **加粗句必须和后面的正文连成同一段**，不要加粗完单独成段、再用同样的话复述一遍；
     同一个事实全文只出现一次，不要换个说法反复讲；
   - 每段不超过 100 字（手机上四五行的长度），一段只讲一件事，不要把 300 字塞成一块；
   - 每 300 字里至少要有一个"记忆点"：一个具体数字、一个类比、一处对比，
     或者一句能被单独摘出来引用的话。没有记忆点的段落等于白写；
   - 需要操作时给可执行步骤，不要写"建议及时更新"这种谁都会说的空话。
   **小标题要自己概括这一节在讲什么，不要把素材里的半句话直接截出来当标题。**
5. 涉及操作、配置、命令时，用三个反引号单独包一段命令，一行一条，方便读者直接复制。
   只允许两类：① 查看类命令（如查版本、查配置）② 动作类命令（重载配置）。
   **配置文件的示例行必须来自素材，素材没给就只描述"移除 listen 指令里的 http2 参数"，
   不要自己贴一行 listen 配置出来。具体版本号、参数值同样必须来自素材。**
   拿不准的命令宁可不写——写错一条命令会让人把服务搞坏，比不写更糟。
6. 结尾一段：给一个明确动作或一句互动提醒（自查、提问、提醒转发都行），不要写成清单。
   禁止写"综上所述""希望对你有帮助""希望对大家有所启发"这类收尾套话。
7. 全文加粗控制在 5 处以内，每节最多一句。满篇加粗等于没有加粗。

【怎么把文章写厚而不注水】
篇幅不够时，从下面五种"合法展开"里挑 2-3 种真正适合这一篇的补，
不要逐条套用，更不要靠形容词和套话凑字数：
① 拆细影响：这件事对不同处境的人结论不一样，说清哪类人该动、哪类人可以等；
② 给判断标准：给出能自己对照的分界线，让人知道自己落在哪一边；
③ 讲清机制：它为什么能行/为什么会出问题，用通用原理讲，不假造具体细节；
④ 列常见误解：大多数人第一反应是什么，为什么这个反应是错的；
⑤ 给操作路径：从哪一步开始、做完怎么验证，写成能照着做的顺序。
**禁止写成'对普通用户而言 / 对从业者而言 / 对小团队而言'这种把读者硬分成三类的排比，
也不要把每节都写成'影响 + 意义 + 建议'三连——那是模板，不是内容。**
以上五类属于通用知识和方法论，可以写；但**具体数字、人名、时间、机构、事件经过
只能来自素材**，素材没给就一个字都不能编。宁可少写一段，也不能编一句。
"""

# ---- 稿件长度 ----
# 教训来源：2026-11-20 用户反馈"提交审核的文章有点短"。
# 原来统一 600-900 字，且提示词里还有一句"素材撑不满时宁可写短"，
# 两条规则打架，模型一律走最短的。现在按素材厚度分三档，并在代码层做字数校验。
# 单位是正文可见字数（不含标题和标记）。
ARTICLE_WORDS = {
    "rich": (1300, 1700),       # 素材 ≥150 字：能写出信息量
    "thin": (1000, 1300),       # 素材 40-150 字：靠机制/标准/操作展开
    "title_only": (900, 1200),  # 只有标题：只写概念与方法
}
# 低于该字数的稿件判为"内容太薄"，触发一次扩写（同一素材，不许新增事实）
ARTICLE_MIN_WORDS = {"rich": 1100, "thin": 850, "title_only": 750}
# 每节（两个小标题之间）低于此字数视为敷衍
SECTION_MIN_WORDS = 220

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
    # 2026-11-20 补：热榜里出现但原词表漏掉的国产模型/产品名（知乎榜实测漏了"混元"）
    "混元", "千问", "元宝", "Claude", "Gemini", "Llama", "智能体", "Agent",
]
KEYWORDS_WEAK = [
    "科技", "数码", "互联网", "软件", "系统", "更新", "智能", "算法",
    "数据流", "算力", "云计算", "自动驾驶", "新能源", "电池", "屏幕",
    "相机", "拍照", "性能", "应用", "平台", "开源", "代码",
    # 账号定位是"AI + 副业"，社交平台热榜里的副业类话题也算相关
    "副业", "搞钱", "兼职", "自由职业", "远程办公",
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


# ---- 多平台热榜源（2026-11-20 新增）----
# 目标是把选题面从"技术媒体"扩到"全网七个平台"，与公众号爆款选题的判断一致：
# 微博看情绪、抖音看传播、知乎看讨论、快手看下沉、微信看同行在写什么。
#
# 每个源单独开关，挂了或不想用就在 .env 里写 =0，不用改代码：
#   USE_WEIBO_TOPICS=0
#
# 摘要有无要分清（直接影响能写出多长的稿）：
#   · 有摘要：百度、知乎、微博（note 字段）、微信（搜狗返回摘要）→ 能写解读
#   · 只有标题：抖音、快手、头条 → 只能写"是什么 + 怎么看"，别指望它出深度稿
USE_WEIBO_TOPICS = os.getenv("USE_WEIBO_TOPICS", "1") == "1"     # 微博热搜
USE_DOUYIN_TOPICS = os.getenv("USE_DOUYIN_TOPICS", "1") == "1"   # 抖音热榜
USE_ZHIHU_TOPICS = os.getenv("USE_ZHIHU_TOPICS", "1") == "1"     # 知乎热榜（摘要最完整）
USE_KUAISHOU_TOPICS = os.getenv("USE_KUAISHOU_TOPICS", "1") == "1"  # 快手热榜
USE_BILIBILI_TOPICS = os.getenv("USE_BILIBILI_TOPICS", "1") == "1"  # B站热搜（科技区相关度高）
# 微信侧：微信搜一搜热搜榜没有公开接口（只能在微信客户端里看），
# 这里用搜狗微信的公众号文章搜索替代——不是榜单，是"同行正在写什么"，
# 拿真实标题+摘要+链接，反而更适合找选题角度。频控较严，关键词别超 3 个。
USE_WECHAT_SEARCH = os.getenv("USE_WECHAT_SEARCH", "1") == "1"
WECHAT_SEARCH_KEYWORDS = [
    k.strip()
    for k in os.getenv("WECHAT_SEARCH_KEYWORDS", "AI 副业,AI 工具").split(",")
    if k.strip()
]

# GitHub 热门项目作为兜底选题源（调官方 Search API）。
# 境外站点，服务器实测时通时不通，故只在国内源都不够时补位，不再优先。
USE_GITHUB_TOPICS = True

# ---- DailyHotApi 聚合源（可选，默认关闭）----
# 自部署项目（imsyy/DailyHotApi），一个接口拿多平台热榜，省掉逐站适配。
# 但公共实例常年不稳定：2026-11-19 实测 api-hot.imsyy.top 与本机代理均不可达，
# 所以默认关闭，代码就绪但等验证通过再开。
# 启用步骤：
#   1. 在服务器上跑 `python hot_topics.py --check dailyhot` 看能不能通
#   2. 通了就在 .env 里写 USE_DAILYHOT=1（或自建实例改 DAILYHOT_BASES）
USE_DAILYHOT = os.getenv("USE_DAILYHOT", "0") == "1"

# 多实例按顺序尝试，第一个能通的就用（自建实例填自己的地址）
DAILYHOT_BASES = [
    b.strip()
    for b in os.getenv(
        "DAILYHOT_BASES",
        "https://api-hot.imsyy.top",
    ).split(",")
    if b.strip()
]

# 要拉的平台（对应 DailyHotApi 的路由名）
DAILYHOT_PLATFORMS = ["zhihu", "weibo", "36kr", "juejin"]

# 备用选题关键词：热榜抓不到相关话题时，用这些让 AI 自己出题
FALLBACK_TOPICS: list[str] = [
    "普通用户现在最值得用的 AI 工具有哪些",
    "大模型能力又升级了，对我们的实际影响是什么",
    "买新手机别只看参数，这几个体验才是关键",
    "AI 生成内容怎么分辨真假，实用辨别方法",
    "国产芯片和系统的最新进展意味着什么",
]

# ---- 审核后台 ----
# 监听地址与端口。端口走环境变量，不写死：
# 同一台机器上若已有别的项目占了 5000（实测撞到过 gunicorn 起的另一个 Flask 应用），
# 在 .env 里加一行 APP_PORT=5001 即可，不用改代码、不用改 git。
# 改端口时记得同步 nginx 的 proxy_pass 和 .env 里的 REVIEW_URL（要带端口）。
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT") or 5000)
# 会话签名密钥：登录一次后记住状态用的。生产建议在 .env 里设一个随机串。
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")

# 公网访问密码：部署到服务器后必须设，否则审核页裸奔
# 微信推送链接会自动带 ?pwd=xxx，点开直接进；本地开发可留空
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

# 审核页公网地址：推送到微信的链接就指向这里，带密码
# 例：http://1.2.3.4:8080/?pwd=你的密码
# 变量名在历史上换过一次（旧名见下面 _review_url 的说明），不同机器的 .env 里
# 可能还是旧名。这里按 REVIEW_ 前缀宽松匹配，取到非空值就用，避免出现
# "推送成功但链接是空字符串" 这种很难发现的故障。
def _review_url() -> str:
    """取审核页公网地址，兼容 REVIEW_URL 与旧变量名。"""
    preferred = os.getenv("REVIEW_URL", "").strip()
    if preferred:
        return preferred
    for key, value in os.environ.items():
        if key.startswith("REVIEW_") and value.strip():
            return value.strip()
    return ""


REVIEW_URL = _review_url()

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
# 改这一个值即可换署名；也可在 .env 里写 SITE_AUTHOR=你的账号名 覆盖（不用改代码）
SITE_AUTHOR = os.getenv("SITE_AUTHOR", "Plan B 的AI笔记")

# ---- 富文本增强 ----
# 给裸文本加 <span leaf=""> 包裹：多一层保险，避免公众号对裸文本的样式继承出错。
# 默认关闭——当前输出已经实测能正常粘贴，先开校验观察一段时间，需要时再开这个。
WECHAT_LEAF_WRAP = os.getenv("WECHAT_LEAF_WRAP", "0") == "1"

# 相邻两节之间自动插一条极淡的分隔线（#eceff4）。长文换气用，默认开；
# 觉得啰嗦就在 .env 里写 WECHAT_SECTION_DIVIDER=0。
WECHAT_SECTION_DIVIDER = os.getenv("WECHAT_SECTION_DIVIDER", "1") == "1"

# ---- 微信公众号草稿箱（可选）----
# 配了才能用「推到草稿箱」；不配就继续用复制粘贴，不影响其他功能。
# AppID / AppSecret 在公众号后台「设置与开发 → 基本配置」里。
# 还要把服务器出口 IP 加到该页面的「IP 白名单」，否则报 40164。
WECHAT_APPID = os.getenv("WECHAT_APPID", "").strip()
WECHAT_SECRET = os.getenv("WECHAT_SECRET", "").strip()
