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

# 每次生成的文章数量。默认按三条内容线的每日配额自动算（见下方排期配置），
# 命令行/py 脚本可用 --count 显式覆盖。
# BATCH_SIZE 定义在文件下方 DAILY_NEWS_COUNT / DAILY_SIDE_BIZ_COUNT 之后。

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
   五种公式选一种：
   ① 具体信息点——带版本号、金额、时间或数量（"升到 1.30.7 才算修完"）
   ② 数字盘点——"5 个免费工具，第 3 个我用了半年"
   ③ 反常识——先说大家默认的想法，再给出相反的事实
   ④ 利益直给——"三条命令，查你的服务器中没中招"
   ⑤ 避坑/损失——点出不处理的具体后果（"还用旧配置？小心下次升级直接失败"），
      写具体后果，不写"赶紧看""再不看就晚了"这类空喊
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

# ---- AI副业栏专用标题规则（2026-11-24 新增）----
# 为什么单独一套、不跟早报共用：
# 副业栏已发 4 篇里 3 篇标题都是"劝退"结论（门槛没那么低 / 学费交给了老师 /
# 能力才是起点），阅读量 5 / 1 / 2；唯一给出规模和收益追问的
# 《AI智能体挂机副业，10个号跑一周能赚多少》拿到 45，是其余三篇的 9-45 倍。
# 副业读者点开是想知道"有没有机会、要投多少、多久见钱"，不是来听劝阻的。
# 注意：这套只作用于 column=ai_sidebiz，早报（ai_news）继续用上面的 TITLE_RULE，
# 两边不共用，改这里不会影响早报标题风格。
SIDE_BIZ_TITLE_RULE = """\
1. 第一行只写标题，12-20 字，不要加"标题："字样、不要加任何标记或书名号。
   四种公式选一种，全部指向"能不能做、要投多少、多久见钱"：
   ① 规模设问——给具体数量与周期，再把结果当问题抛出（"10 个号跑一周，能落多少"）
   ② 算账——把一个具体成本或收入拆开算（"一单 30 元，扣掉返工还剩多少"）
   ③ 路径——从零到第一单要走的几步，写清起点和终点（"从注册到第一单，卡在哪一步"）
   ④ 门槛/代价——写清入门要付出的具体时间、钱或技能（"每天 1 小时，两周够不够"）
2. 严禁把"劝退"当结论写进标题。像"没那么容易""你会被骗""能力才是起点"
   "别急着做"这类只有否定、不给代价数字和出路的标题一律不许用——
   读者要的是判断依据，不是被劝回去。
3. 不许写收益承诺："月入过万""躺赚""轻松""稳赚""日结三位数"全部禁止。
4. 不许用第一人称编造亲历："我测了""我做了三个月""亲测有效"——
   除非素材里确实有这次实测，否则写成设问或路径，不要冒充当事人。
5. 标题里的数字必须和素材或正文完全一致，拿不准就别在标题里写数字。
6. 禁止诱导词：揭秘、震惊、速看、炸裂、马上删、惊呆、血亏、封神。"""

# 副业栏换标题时用的三备选（与上面的 SIDE_BIZ_TITLE_RULE 同一套逻辑）
SIDE_BIZ_TITLE_VARIANTS_RULE = """\
基于下面这篇副业文章的内容，给出 3 个备选标题。要求：

1. 三个标题分别用下面三个公式，不要写成同一个公式的三种变体：
   ① 规模设问：给具体数量与周期，把结果当问题抛出
   ② 算账：把一个具体成本或收入拆开算，让读者看到净额
   ③ 路径/门槛：写清从零到第一单的步骤，或入门要付出的具体代价
2. 每个标题 12-22 字，必须有具体名词或数字，禁止"值得关注""影响几何"这类空泛说法。
3. 严禁劝退式结论（"没那么容易""你会被骗""能力才是起点""别急着做"）——
   只否定不给依据的标题一律不合格。
4. 不许写收益承诺（月入过万、躺赚、轻松、稳赚），
   也不许用第一人称编造亲历（我测了、我做了三个月），除非素材里确有其事。
5. 标题里的数字必须能在下面的原文素材或正文里找到，拿不准就别写数字。
6. 不要给文章换角度、不要改正文——只改标题本身。

输出格式（严格遵守，每行一个，不要编号、不要解释、不要空行）：
公式①的标题
公式②的标题
公式③的标题"""

# ---- AI副业栏专用：数字硬要求（2026-11-24 新增）----
# 实测：9 篇副业稿平均只带 1.2 个"带单位的数字"，4 篇一个都没有
# （对照已发 AI 资讯稿单篇 5-6 个）。正文于是只剩"隐性成本""质量控制"
# "竞争激烈"这种任何行业都适用的话，读者拿不到任何可核对的东西。
# 这里把"必须给数字"写死，但同时封住编造口子——数字只许来自素材或有依据的换算，
# 素材确实没有时必须直说没有，不许用"一般来说""通常"包装成事实。
# 只注入 column=ai_sidebiz，早报不受影响。
SIDE_BIZ_DATA_RULE = """\
【副业稿的数字硬要求】
副业读者只关心三件事：要投多少、多久能做出来、能落多少钱。所以正文必须给出数字，
但**只允许两种来源**，别的一律算编造：

1. 素材里本来就有的数字：原样引用，不许换算、不许四舍五入、不许改单位。
2. 你自己按素材做的换算或估算：必须把算式和依据写在同一句里，
   例如"按每单 30 元、一周接 20 单算，毛收入约 600 元，还没扣返工"。
   没有依据可依的换算不许写。

硬性数量：全文至少 3 处带单位的具体数字（金额、时长、单量、抽成比例、
平台结算周期都算），且必须落在不同小节，不能三处都堆在同一段。

素材确实一个数字都没有时：直说"这类副业没有公开的收益数据"，
然后给读者可核查的口径——具体平台名、结算方式、去哪里问真实单价、
自己怎么用一天时间试出来。**不许**用"一般来说""通常能到""业内普遍"
把这些糊过去，那等于编造。

平台必须写具体名字（小红书/闲鱼/抖音/视频号/猪八戒/Fiverr/Upwork 等），
不许写"某个平台""一些渠道"。不确定某平台是否还开放某类目，就写"以平台当前规则为准"，
不要替它承诺。"""

# ---- 知乎问答素材的引用规则（2026-11-24 新增）----
# 知乎回答是"第三方自述"，不是可核实事实。实测搜到的摘要里就有
# "副业月收入稳定在 5000-8000 元""月入过万都有真实案例"这类说法——
# 直接搬进正文等于替别人做收益承诺，也正是这个号最该避免的。
# 这条只在素材来自知乎问答/知乎搜索时注入。
ZHIHU_QA_RULE = """\
【这条素材来自知乎，属于第三方自述，引用有边界】
1. 回答者/作者说的收入、单价、单量都是**他个人的自述，未经核实**。
   引用时必须写明「有回答者自述」「有作者称」，不许写成客观事实，
   更不许写进标题。
2. 不许把多个回答里的数字相加、平均，或据此推算"普通人能赚多少"。
3. 可以用它做三件事：判断读者在关心什么、列出常见的几种做法、
   指出这些说法之间哪里互相矛盾（矛盾本身就是好角度）。
4. 素材是「知乎搜索」而不是「知乎问答」时，说明我们只拿到搜索引擎的页面摘要，
   不是高赞回答原文。这种时候不要写"高赞回答认为""最高赞说"——
   我们没有那个信息，写了就是编造。"""

# 副业栏取题顺序（2026-11-24 新增）
# 原来第一个来源是搜狗微信搜索（同行写过的稿），实测 9 篇副业稿 100% 来自这里，
# 0 篇来自热榜或一手源——素材只有一句同行标题 + 一句摘要，正文只能靠通用道理推演。
# 打开后改为：热榜/一手源命中副业词的优先（有真实事件和数据），
# 公众号搜索退为素材补充，兜底词库最后。设 0 恢复旧顺序。
SIDE_BIZ_WECHAT_AS_FALLBACK = os.getenv("SIDE_BIZ_WECHAT_AS_FALLBACK", "1") == "1"
# 副业候选的素材门槛：摘要短于这个字数且一个带单位数字都没有的，
# 排到候选末尾（不直接丢，否则深夜容易没稿）。0 = 不排序。
SIDE_BIZ_MIN_DESC_CHARS = max(0, int(os.getenv("SIDE_BIZ_MIN_DESC_CHARS", "40")))

# ---- 知乎问答素材源（2026-11-24 新增）----
# 目标：拿"读者真实在问的问题 + 高赞回答里的信息"，替代同行公众号二手稿。
# 实测（2026-11-24，服务器直连）：
#   知乎问题页/专栏页/搜索页对无登录态一律 403（返回 zh-zse-ck 反爬挑战页）；
#   search_v3 与 answers API 返回 401 ZERR_NOT_LOGIN。
#   所以真正能拿到"高赞回答"的唯一路径是带登录 cookie（z_c0）。
# 没配 cookie 时自动降级：用必应 RSS 搜 site:zhihu.com <关键词>，
#   能拿到问题/专栏标题 + 搜索结果摘要（实测 6 个查询 57 条里 9 条知乎，
#   其中 4 条问题页），够当选题线索，但**不是高赞回答全文**，每条会标明来源。
USE_ZHIHU_QA = os.getenv("USE_ZHIHU_QA", "1") == "1"
# 只要 z_c0 这一段就够，整串 cookie 也能用（会自动只取 z_c0）。
# 取法：浏览器登录知乎 → F12 → Application/存储 → Cookie → www.zhihu.com → 复制 z_c0 的值。
# 有效期约 1 个月，失效后知乎返回 401，日志会提示重新贴。
ZHIHU_COOKIE = os.getenv("ZHIHU_COOKIE", "").strip()
# 搜哪些问题。用"问题式"说法比单个词命中率高（实测"副业 接单 单价"能搜到问题页，
# "小红书 副业 起号" 0 命中）。逗号分隔，只取前 3 个。
ZHIHU_QA_KEYWORDS = [
    k.strip()
    for k in os.getenv(
        "ZHIHU_QA_KEYWORDS", "AI副业,副业 接单 单价,AI 变现 真实收入"
    ).split(",")
    if k.strip()
][:3]
# 每个问题取几条高赞回答拼素材（有 cookie 时生效）。
ZHIHU_QA_ANSWERS = max(1, int(os.getenv("ZHIHU_QA_ANSWERS", "2")))
# 无 cookie 降级时，搜索摘要短于这个字数就不当素材（太薄等于没信息）。
ZHIHU_QA_MIN_DESC = max(0, int(os.getenv("ZHIHU_QA_MIN_DESC", "50")))

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

# ---- 传播力三段：解决"写得对但没人转" ----
# 爆款 = 内容质量 × 传播设计。上面几条把"写对写实"做到了，这里补决定打开和转发的三段：
# 开头戳痛点、正文留金句、结尾给转发理由。仍受真实性硬要求约束，不许编造焦虑或收益。
VIRAL_HOOK_RULE = """\
【传播力三段：让人想点开、想转走】
在真实性和信息增量的前提下，额外做到三点——这三点决定文章能不能被点开和转发：

① 开头戳中一个具体担心（写在第一段，不另起一段）：
   别只说"这件事发生了"，要让读者立刻对号入座——他是不是正在用旧版本、
   是不是以为设置好了其实没生效、是不是也遇到过这个坑。
   写法是直接点破一个真实顾虑，不要写成"你还在为 XX 烦恼吗"这种营销腔设问。
   这个担心必须是素材能支撑的，没有依据的焦虑不许制造。

② 全文留一句能被单独截图转走的话：
   从文章里挑一处加粗句，写成"脱离上下文也成立、也有信息量"的判断——
   一个具体数字对比、一条避坑结论、一个反常识判断，让人截个图发群里也有价值。
   "值得关注""影响深远"这种空话不算。全文至少一句，最多两句。

③ 结尾给一个明确的转发理由，不要只喊"欢迎留言"：
   说清"哪类人该看这条、为什么现在就该转给他"——比如手里还跑着旧版本的朋友、
   正准备升级的人、负责这台机器的同事。理由要具体到场景，不要泛泛地"转给需要的朋友"。
   转发理由之外再带一句轻量互动（提问或求在看）即可，别写成求转发广告。

自检：把开头、金句、结尾三处单独摘出来看——陌生人看了会不会想点、想存、想转给某个人？
三处都平淡，就是"写对了但没人看"，回去改这三处，别去改中间的事实。"""

GITHUB_COLUMN_RULE = """\
【GitHub 深度专栏专用写法——本栏目不适用上面的资讯结构模板，按这里写】
这是一期只讲透一个开源项目的栏目。读者是想知道"这东西能不能用在我身上、怎么上手"的开发者，
不是来看新闻的。写法像一位用过这个项目的朋友在做演示，不是新闻通稿。

结构固定为四节，每节用"## "开头的陈述句小标题，顺序不许变：
1. ## 一句话说清它是什么
   第一段先用**加粗**写一句定位（它是什么 + 给谁用的），紧跟一句话说清它解决的具体痛点。
   开头禁止"近年来""随着……发展"这类套话。
2. ## 它到底能帮你做什么
   写 2-4 个具体用途，每个用途要落到真实场景（比如"给团队搭一个私有问答入口"），
   不要写成"功能强大、应用广泛"这种空话。素材里给了能力/亮点就照实写，没给的不许编。
3. ## 怎么用：从安装到跑起来
   这一节给出可复制的操作路径，是全文最有价值的部分：
   - 命令、配置用三个反引号单独包成代码块，一行一条，读者可以直接复制；
   - 只允许写两类命令：① 官方素材/仓库 README 里明确给出的安装或启动命令
     ② 由项目类型能安全推断的通用命令（如 pip install 项目名、docker 拉取官方镜像）；
   - **绝对不许编造具体版本号、参数值、API 路径、配置项名字**——素材没给的，
     就写"具体参数以项目 README 为准"，并给出仓库地址让读者自己核对；
   - 每给一条命令，后面跟一句"这一步在干什么 + 怎么确认成功"。
4. ## 谁适合用，以及上手前要知道的
   写清楚：哪类人/哪种场景最适合、什么情况下不必用。
   再补一条务实的提醒——star 多不等于适合你，许可协议和最近是否还在维护，
   用之前去仓库看一眼 license 和最近提交（素材里有这些信息就照实写，没有就提醒读者自己核对）。

真实性硬要求（本栏目同样必须遵守，违反即作废）：
- star 数、许可协议、最近更新时间只能用素材里给的值，一个字都不许编；
- 仓库地址必须是素材里的真实链接，不许自己拼、不许猜；
- 项目能力只写素材里有的；不确定的地方直说"素材未说明，建议看仓库 README"；
- 不许写"据说是""广受好评""社区活跃"这类没有依据的评价。

收尾：全文最后给一句具体的"谁现在就该去试一下"，不要写"希望对你有帮助"这类套话。
全文加粗控制在 5 处以内。"""

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
    # 2026-11-24：手机/OS 厂商名从强相关降为弱相关（见 KEYWORDS_WEAK）。
    # 报道方向已收窄到"AI 行业信息"，降级前实测会选出"出货暴跌40% 非洲成最惨手机市场"
    # 这种纯数码消费新闻，占掉了早报名额。降级而非删除：华为/小米的 AI 动态仍能命中。
    "发布会", "机器人",
    # 2026-11-20 补：热榜里出现但原词表漏掉的国产模型/产品名（知乎榜实测漏了"混元"）
    "混元", "千问", "元宝", "Claude", "Gemini", "Llama", "智能体", "Agent",
    # ---- 2026-11-24 报道方向调整：五个方向里的"大模型与 Agent 产品更新" ----
    # 海外四家：产品名和公司名都要列，热榜标题常只写产品不写公司（如"Gemini 3 发布"）
    "Anthropic", "Google", "Meta", "Mistral", "xAI", "Grok",
    "Copilot", "Cursor", "Perplexity", "Midjourney", "Runway", "Suno",
    "Gemma", "Qwen", "Phi", "Whisper", "Veo", "Imagen", "Nano Banana",
    # 国产六小虎（智谱/月之暗面/百川/零一万物/阶跃/MiniMax），
    # 以及它们各自最常被标题直接引用的产品名
    "月之暗面", "Kimi", "百川", "零一万物", "阶跃", "StepFun", "MiniMax",
    "GLM", "海螺", "万知", "Yi-", "abab",
    # Agent / 模型能力类：决定是不是"产品更新"，比"科技"精准得多
    # 注意："Agent""智能体"已在上面列过，这里不重复——重复会让同一篇稿被加两次分
    "MCP", "多模态", "推理模型", "开源模型", "工作流",
    "RAG", "fine-tune", "微调", "上下文", "token",
]
KEYWORDS_WEAK = [
    "科技", "数码", "互联网", "软件", "系统", "更新", "智能", "算法",
    "数据流", "算力", "云计算", "自动驾驶", "新能源", "电池", "屏幕",
    "相机", "拍照", "性能", "应用", "平台", "开源", "代码",
    # 账号定位是"AI + 副业"，社交平台热榜里的副业类话题也算相关
    "副业", "搞钱", "兼职", "自由职业", "远程办公",
    # 2026-11-24 从强相关降级：手机/OS 消费类资讯只在"顺带提到 AI"时才算相关，
    # 单靠"手机"两个字上不了早报
    "手机", "华为", "小米", "苹果手机", "iPhone", "鸿蒙",
    # 2026-11-23 补：副业变现方向，抓"AI副业 / 被动收入"主题
    "AI副业", "被动收入",
    # ---- 2026-11-24 报道方向调整：方向二"AI 办公、内容创作、开发者工具新功能" ----
    # 这三个方向的具体品类词放弱相关：命中就算相关，但优先级低于模型/厂商本身，
    # 避免"某办公软件更新了个插件"这类低价值资讯挤掉真正的大模型动态。
    # ① AI 办公
    "办公", "文档", "表格", "PPT", "幻灯片", "会议", "纪要", "协作",
    "Notion", "飞书", "钉钉", "WPS", "Office", "Excel", "邮箱",
    # ② 内容创作
    "创作", "剪辑", "配音", "字幕", "封面", "设计", "配图", "生图",
    "Canva", "Figma", "即梦", "可灵", "剪映", "Midjourney", "音色",
    # ③ 开发者工具
    "IDE", "编程", "开发", "SDK", "API", "插件", "终端", "部署",
    "GitHub", "仓库", "提效", "重构", "测试", "数据库",
]

# 不写的主题（命中即丢弃，优先级高于上面的相关词）。
# 账号定位是 AI + 数码科技资讯，汽车整车/购车资讯不是这个号的内容——
# 以前 RSS 源（IT之家等）不设防，汽车稿会直接混进来。这里做硬过滤；
# 页面「账号画像 → 不写的主题」可随时改，改完下次生成生效。
# 注意只列"整车消费资讯"的词，不列"电池/自动驾驶"——那些属于科技话题。
EXCLUDE_TOPIC_KEYWORDS = [
    "汽车", "新车", "车型", "SUV", "轿车", "MPV", "越野车", "车展",
    "车企", "续航", "大五座", "提车", "试驾", "落地价", "车机",
    "比亚迪", "东风日产", "蔚来", "理想汽车", "小鹏汽车", "特斯拉",
    "问界", "极氪", "吉利汽车", "宝马", "奔驰", "奥迪", "丰田",
    "本田", "保时捷", "路虎", "沃尔沃", "小米汽车", "大众汽车",
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

# ---- 大模型厂商官方源（2026-11-24 新增）----
# 为什么要单列一组：报道方向第一条就是"大模型与 Agent 产品更新"，
# 靠国内媒体转述容易拿到二手解读、版本号还常写错。官方源是版本号和日期的唯一可信处。
# 组内各类在 pipeline._fetch_rss_items 里按"国内媒体优先、官方源补位"两段轮转，
# 不会因为英文源太多把国内技术媒体的位置挤掉。
#
# 2026-11-24 在服务器（101.201.48.64）逐条实测：
#   OpenAI  news/rss.xml                      200  1.6s  ✅
#   Google  blog.google/.../technology/ai/rss 200  1.4s  ✅（旧地址会 301，直接写最终地址）
#   DeepMind blog/rss.xml                     200  1.3s  ✅
#   Meta    about.fb.com/news/feed/           200        ✅（ai.meta.com 被重置，改用新闻中心）
#   Anthropic 无 RSS（/rss.xml 等三种都 404）   → 走 news 页 HTML 解析，见 fetch_anthropic
# 国产六小虎（智谱/月之暗面/百川/零一万物/阶跃/MiniMax）无稳定 RSS，
# 靠 KEYWORDS_STRONG 里的产品名词命中热榜、HN 与国内媒体来覆盖。
USE_OFFICIAL_SOURCES = os.getenv("USE_OFFICIAL_SOURCES", "1") == "1"
OFFICIAL_SOURCES: list[dict[str, str]] = [
    {"name": "OpenAI", "url": "https://openai.com/news/rss.xml"},
    {
        "name": "Google AI",
        "url": "https://blog.google/innovation-and-ai/technology/ai/rss/",
    },
    {"name": "DeepMind", "url": "https://deepmind.google/blog/rss.xml"},
    {"name": "Meta", "url": "https://about.fb.com/news/feed/"},
]
# Anthropic 没有 RSS，单独抓新闻页链接（实测能解析出 20+ 条）
USE_ANTHROPIC_HTML = os.getenv("USE_ANTHROPIC_HTML", "1") == "1"

# ---- Hacker News 首页（2026-11-24 新增）----
# 用 Algolia 的 front_page 接口（实测 0.8s，比官方 Firebase 的 6.8s 快一个数量级，
# 且直接给 points / num_comments）。HN 条目多数没有正文摘要，
# 所以抓下来后会用 og:description 补一次——不补的话只能写"标题级"的稿。
USE_HN_TOPICS = os.getenv("USE_HN_TOPICS", "1") == "1"
# 2026-11-24 实测：门槛 80 分太松，放进来的是"NariQ 153 分"这种圈内小众讨论，
# 写出来读者不认识、也没人转。HN 首页真正的热点都在 1000 分上下，
# 而 400 分是"技术圈普遍在聊"的底线，低于它不值得占早报一个名额。
HN_MIN_POINTS = max(0, int(os.getenv("HN_MIN_POINTS", "400")))

# ---- Product Hunt 当日榜（2026-11-24 新增）----
# 官方 Atom feed，实测 1.0s、50 条，顺序即当日热度序。
# 注意：feed 里只有一句话 tagline（约 60 字），属于"薄素材"，
# 下游会按 thin 档写作（1000-1300 字，靠机制与判断展开，不靠细节）。
#
# 2026-11-24 第二轮调整：PH 是"新品发现"，不是"热点事件"——
# 它没有票数、没有讨论量，榜位只说明"今天上新了"。
# 所以 prescore 从 4 降到 1：仍能进候选池（英文标题过不了中文关键词表，
# 必须自带分），但排在所有国内热榜之后，只在国内题不够时兜底。
# 原来把榜位换算成 hot=60/52 当成"热度"参与排序，是把新品冒充热点，已去掉。
USE_PRODUCTHUNT_TOPICS = os.getenv("USE_PRODUCTHUNT_TOPICS", "1") == "1"
PRODUCTHUNT_PRESCORE = max(1, int(os.getenv("PRODUCTHUNT_PRESCORE", "1")))

# ---- V2EX 热帖（2026-11-24 新增，默认关闭）----
# 实测结论：服务器直连不通，且没有可用替代路径——
#   www.v2ex.com/api/topics/hot.json   超时
#   v2ex.com/index.xml                 TCP 直接连不上（62ms 就断）
#   cn.v2ex.com                        超时
#   rsshub.app/v2ex/topics/hot         超时
# 代码保留（含解析逻辑），等有代理或换机房再开：.env 写 USE_V2EX_TOPICS=1。
# 在你那台机器上它必然返回空，所以默认关，免得每天刷一条失败日志。
USE_V2EX_TOPICS = os.getenv("USE_V2EX_TOPICS", "0") == "1"

# ---- 海外源英文关键词（HN / Product Hunt 用）----
# 为什么不复用 KEYWORDS_STRONG：那是一套"子串包含"匹配，中文标题里用没问题，
# 但英文标题一律小写后就炸了——"ai" 会命中 "said""detail""main"。
# 所以英文源单独用一组词，并且按单词边界（\b）匹配。
AI_KEYWORDS_EN = [
    "ai", "llm", "llms", "gpt", "openai", "anthropic", "claude", "gemini",
    "deepmind", "llama", "mistral", "qwen", "glm", "kimi", "deepseek",
    "agent", "agents", "agentic", "mcp", "rag", "embedding", "embeddings",
    "transformer", "diffusion", "multimodal", "inference", "fine-tuning",
    "chatbot", "copilot", "cursor", "prompt", "prompts", "tokenizer",
    "vision model", "text-to-image", "text-to-video", "speech-to-text",
    "machine learning", "neural", "gpu", "nvidia", "tpu", "hugging face",
    "langchain", "vector database", "open-source model", "local model",
]

# ---- GitHub Trending 当日涨星 TOP（2026-11-24 新增）----
# 方向三要的是"当日涨星 TOP3 的 AI 项目"，与"每 3 天 1 篇的深度专栏"是两件事：
# 专栏 = 吃透一个项目（长稿、专用提示词）；这里 = 每日热点资讯（占早报 1 条）。
# Trending 的 since=daily 分支自带 "N stars today" 字段，正是"当日涨星"。
USE_GITHUB_TRENDING_DAILY = os.getenv("USE_GITHUB_TRENDING_DAILY", "1") == "1"
GITHUB_TRENDING_DAILY_TOP = max(1, int(os.getenv("GITHUB_TRENDING_DAILY_TOP", "3")))
# 每天早报里给 Trending 留几条位置。默认 1——保持"一条资讯一个焦点"，
# 想要一次把 TOP3 都写成三条，把它设成 3 即可。
GITHUB_TRENDING_DAILY_SLOTS = max(1, int(os.getenv("GITHUB_TRENDING_DAILY_SLOTS", "1")))
# 判断一个仓库是不是"AI 项目"：查仓库名 + 官方简介（英文为主，按单词边界匹配）
GITHUB_TRENDING_AI_KEYWORDS = [
    "ai", "llm", "agent", "agents", "gpt", "claude", "gemini", "llama",
    "rag", "mcp", "diffusion", "transformer", "chatbot", "prompt",
    "embedding", "multimodal", "vision", "speech", "tts", "whisper",
    "copilot", "openai", "anthropic", "qwen", "deepseek", "glm", "kimi",
    "semantic", "neural", "nlp", "inference", "model", "skill", "skills",
]

# ---- 一手源优先级（2026-11-24 新增）----
# 条目分怎么来的：中文热榜命中关键词得 3 分（KEYWORDS_STRONG 每条 +3），
# 而海外/官方源的标题是英文，打不了中文关键词分，改为直接给 prescore。
# prescore 必须高于 3，否则同分时会被热榜的"万"级热度反超——
# 实测 HN 的 GLM-5.4 就是这么被"百度热搜·非洲手机市场"挤掉的。
# 2026-11-24 第二轮：名额已改由 NEWS_DOMESTIC_FIRST 的槽位规则决定，
# 这里的分只影响候选池内部的先后次序，不再决定国内/海外占比。
FIRSTHAND_PRESCORE = int(os.getenv("FIRSTHAND_PRESCORE", "4"))


# ---- 选题来源均衡（2026-11-24 新增）----
# 背景：知乎热榜的 hot 是"万"级、HN 的 points 是"千"级、RSS 是 0，
# 单纯按 (相关分, 热度) 排序，海外源和官方源永远排在热榜后面，
# 五个方向里后三个等于白接。
# 打开后 collect() 改为"按来源轮转"：每个来源先出自己最高分的那条，
# 再出第二条……这样早报 5 条自然覆盖到热榜/HN/RSS 多个方向。
NEWS_SOURCE_DIVERSIFY = os.getenv("NEWS_SOURCE_DIVERSIFY", "1") == "1"

# ---- 早报国内/海外配比（2026-11-24 第二轮调整）----
# 实测问题：早报 5 条里 4 条是海外（GitHub 涨星 + OpenAI 官方 + HN×2），
# 唯一一条国内还是"百度热搜·大学生造电子汴京城"。而当天真正的大热点
# （英伟达暴涨、知乎 1478 万热度）恰恰被英文版 HN 挤掉了名额。
# 对中文读者来说，海外榜单第 60 名的新品、HN 153 分的小众讨论都不叫热点。
#
# 现在改成：GitHub 当日涨星 1 条 + 厂商官方源 1 条固定（这两条是报道方向
# 明确要求的），其余名额**先给国内**（知乎/百度/微博/国内技术媒体 RSS），
# 国内题不够时才由海外（HN/Product Hunt）兜底，且最多 OVERSEAS_NEWS_MAX 条。
# 关掉 NEWS_DOMESTIC_FIRST 就回到"全部混在一起按分排"的老逻辑。
NEWS_DOMESTIC_FIRST = os.getenv("NEWS_DOMESTIC_FIRST", "1") == "1"
OVERSEAS_NEWS_MAX = max(0, int(os.getenv("OVERSEAS_NEWS_MAX", "1")))

# 这些 source 名字算"国内源"（补位时优先占名额）。
# 公众号搜索单独归并——它的 source 是"公众号·某某号"，一个号一个名字。
DOMESTIC_SOURCE_NAMES = [
    "知乎热榜", "百度热搜", "微博热搜", "公众号搜索", "头条热榜",
    "B站热搜", "抖音热榜", "快手热榜", "掘金热榜", "开源中国",
    "IT之家", "雷峰网", "爱范儿", "博客园", "量子位", "InfoQ",
]

# ---- 英文素材强制译成中文稿（2026-11-24 新增）----
# 海外源（HN / Product Hunt / 官方 RSS）的标题和摘要都是英文，直接丢给模型
# 会出现三种毛病：标题照抄英文、正文中英混排、专有名词生造译名。
# 打开后，判定为英文素材的条目会在提示词里追加一段翻译与本地化要求；
# 生成、重写优化、扩写、换标题四条链路都生效。
TRANSLATE_FOREIGN_ITEMS = os.getenv("TRANSLATE_FOREIGN_ITEMS", "1") == "1"

# ---- 同一事件跨源去重（2026-11-24 新增）----
# 实测问题：一条"黄仁勋回归英伟达"的新闻，知乎写中文、Hacker News 写英文，
# 两边都进了早报，5 条里 2 条讲同一件事。按 URL 去重抓不到跨语言同事件。
# 这里用实体别名组近似：同组任意一个词命中就算提了同一主体，
# 该主体当天已出现过就不再选第二条——早报名额有限，宁可换一条别的。
# 只查标题不查摘要：摘要里顺带提到的公司太多，会把去重做得过于激进。
TOPIC_ENTITY_ALIASES = [
    ("英伟达", "nvidia"),
    ("黄仁勋", "jensen huang"),
    ("openai", "chatgpt", "sam altman", "奥特曼"),
    ("anthropic", "claude"),
    ("google", "谷歌", "deepmind", "gemini"),
    ("meta", "llama", "扎克伯格"),
    ("microsoft", "微软", "copilot"),
    ("apple", "苹果"),
    ("华为", "huawei"),
    ("小米", "xiaomi"),
    ("字节", "bytedance", "豆包", "抖音"),
    ("阿里", "alibaba", "千问", "qwen", "通义"),
    ("腾讯", "tencent", "混元"),
    ("deepseek", "深度求索"),
    ("智谱", "glm", "zhipu"),
    ("月之暗面", "kimi", "moonshot"),
    ("百川", "baichuan"),
    ("零一万物", "lingyiwanwu"),
    ("阶跃", "stepfun"),
    ("minimax", "海螺"),
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
    # 注意：搜狗频控较严，fetch_wechat_search 只取前 3 个词（[:3]），
    # 所以要新增词必须放在前三位。"AI副业"与"AI 副业"仅空格差异、不重复占坑。
    for k in os.getenv("WECHAT_SEARCH_KEYWORDS", "AI 副业,AI 工具,被动收入").split(",")
    if k.strip()
]

# GitHub 热门项目作为兜底选题源（调官方 Search API）。
# 境外站点，服务器实测时通时不通，故只在国内源都不够时补位，不再优先。
USE_GITHUB_TOPICS = True

# ---- 内容栏目分类（2026-11-23 新增）----
# 三条内容线，对应审核页「GitHub精选 / AI早报 / AI副业」三个筛选项。
# 生成时给每篇稿打 column 字段：github / ai_news / ai_sidebiz。
# 老稿件没有 column 字段时，审核页按标题关键词兜底归类，保证筛选也能用。
COLUMN_LABELS = {
    "github": "GitHub精选",
    "ai_news": "AI早报",
    "ai_sidebiz": "AI副业",
}
# 副业栏目的判定词：命中标题/摘要即归到 AI副业，其余资讯稿归 AI早报。
# 与 KEYWORDS_WEAK 里的副业词保持一致，另补几个常见的变现说法。
SIDE_BIZ_KEYWORDS = [
    "副业", "搞钱", "兼职", "自由职业", "远程办公", "被动收入",
    "变现", "赚钱", "增收", "接单", "AI副业", "AI 副业",
]

# ---- 三条内容线的每日排期（2026-11-23 用户定档）----
# 审核页三个栏目 tab 对应三条内容线，各自节奏不同：
#   1. AI早报     ：每天 5 条（AI/科技资讯，含热榜与 RSS）
#   2. AI副业     ：每天 2 条（定向抓副业/变现/被动收入素材）
#   3. GitHub精选 ：每 3 天 1 条（一期讲透一个高星标项目）
# 每天自动生成 = 早报 5 + 副业 2，GitHub 到期当天再多 1 条。
# 全部生成后进 pending 待审队列，由用户逐条审核，不自动发布。
DAILY_NEWS_COUNT = max(0, int(os.getenv("DAILY_NEWS_COUNT", "5")))
DAILY_SIDE_BIZ_COUNT = max(0, int(os.getenv("DAILY_SIDE_BIZ_COUNT", "2")))
BATCH_SIZE = DAILY_NEWS_COUNT + DAILY_SIDE_BIZ_COUNT  # 每日默认总数（不含 GitHub 到期篇）

# ---- GitHub 深度专栏（2026-11-23 新增）----
# 与上面"GitHub 当热点线索写资讯"是两回事：这是独立栏目，一期讲透一个项目，
# 格式统一为"是什么 + 解决什么问题 + 怎么用（可复制命令）"。
# 节奏：每 GITHUB_COLUMN_INTERVAL_DAYS 天出 1 篇（默认 3 天），到期当天自动多生成 1 篇。
# 上次生成日期单独记在 COLUMN_STATE_FILE，不受待审稿 3 天清理影响——
# 否则稿一被清掉就会误判成"从没出过"，变成天天生成。
USE_GITHUB_COLUMN = os.getenv("USE_GITHUB_COLUMN", "1") == "1"
DAILY_GITHUB_COLUMN = max(0, int(os.getenv("DAILY_GITHUB_COLUMN", "1")))
GITHUB_COLUMN_INTERVAL_DAYS = max(1, int(os.getenv("GITHUB_COLUMN_INTERVAL_DAYS", "3")))
COLUMN_STATE_FILE = "data/column_state.json"
# 选项目看"近期涨得快"：主源 Trending 周榜（本周新增星标），
# 不足时兜底找"近 N 天新建且已过星标门槛"的项目（新建就涨到这个数=涨得快）。
# 不再按历史累计总星排——那只会选出攒了几年的老项目。
GITHUB_COLUMN_TREND_DAYS = max(7, int(os.getenv("GITHUB_COLUMN_TREND_DAYS", "30")))
# 专栏最少 star 门槛：保证"星标较多"，低于此数不进候选
GITHUB_COLUMN_MIN_STARS = max(100, int(os.getenv("GITHUB_COLUMN_MIN_STARS", "1000")))

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

# 副业栏目的兜底选题：热榜和公众号搜索都抓不到副业素材时，让 AI 基于这些题自己写。
# 每条都带副业词，保证 classify_column 会归到 ai_sidebiz 栏。
# 数量做够（>14 条），配合按日期轮换 + 队列去重，避免天天出同样的题。
SIDE_BIZ_FALLBACK_TOPICS: list[str] = [
    "普通人用 AI 做副业，哪些副业是真的能落地赚到钱的",
    "AI 工具做内容变现的被动收入玩法，门槛和收益实测",
    "下班后 1-2 小时能做的 AI 兼职接单，新手从哪入手",
    "用 AI 提高副业效率的三个真实案例，别再只当玩具",
    "AI 写作接单副业：从注册平台到拿到第一单的完整路径",
    "用 AI 做自媒体内容变现，普通人最现实的三种方式",
    "AI 代做 PPT / 设计接单：副业报价和获客的真实经验",
    "被动收入新玩法：AI 生成数字产品挂机售卖的实操拆解",
    "AI 短视频副业怎么做，剪辑自动化能省多少时间",
    "Prompt 也能卖钱？AI 提示词变现的真实市场与门槛",
    "AI 做知识付费副业：从选题到成交的最小可行路径",
    "普通人做 AI 副业最容易踩的坑，先看这五个再上手",
    "AI 工具组合拳：一个人怎么撑起一个小副业的全流程",
    "从零开始用 AI 做电商内容，副业起步要准备什么",
    "AI 翻译 / 校对接单副业：门槛、单价和接单渠道",
    "用 AI 做本地生活服务副业，低成本试水的几个方向",
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

# 正文末尾是否保留「信息来源：xxx」出处标注。
# 默认关闭：成品里挂别人的公众号名会把读者引走（用户 2026-11-23 反馈）；
# 确有合规需要时，在 .env 里写 SHOW_SOURCE_NOTE=1 即可恢复。
SHOW_SOURCE_NOTE = os.getenv("SHOW_SOURCE_NOTE", "0") == "1"

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

# ---- 头条号自动存草稿（可选，2026-11-23 新增）----
# 原理：Playwright 持久化 profile 记住登录态，页面上下文调头条内部接口存草稿。
# 头条无个人可用的官方发布 API，最后「发表」必须人进后台点，本系统只做存草稿。
# 服务器前置：pip install playwright，再装一个能跑的 Chromium：
#   - Debian/Ubuntu：playwright install chromium && playwright install-deps chromium
#   - CentOS/Rocky：先试 dnf install -y chromium（装到就用系统包，自带全部依赖）
#   - Alibaba Cloud Linux：官方源无 chromium 包，用 playwright install chromium，
#     再按 DEPLOYED.md 第49节用 dnf 手动补运行库（install-deps 只找 apt-get，别用它）
# 首次在审核页点「头条登录」扫码，登录态写进 data/toutiao_profile，之后免扫。
# 浏览器可执行文件：默认自动探测系统 Chromium，探测不到再回退 Playwright 自带包。
# 需要强制指定时在 .env 写绝对路径：BROWSER_EXECUTABLE_PATH=/usr/bin/chromium
BROWSER_EXECUTABLE_PATH = os.getenv("BROWSER_EXECUTABLE_PATH", "").strip()
if not BROWSER_EXECUTABLE_PATH:
    for _chrome_path in (
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
    ):
        if os.path.isfile(_chrome_path):
            BROWSER_EXECUTABLE_PATH = _chrome_path
            break
TOUTIAO_HEADLESS = os.getenv("TOUTIAO_HEADLESS", "1") == "1"  # 服务器无头运行
# 头条/百家号「自动存草稿」总开关（两家共用）。
# 2026-11-24 起默认关闭：两家的登录风控（异地/滑块）和发布签名（msToken/a_bogus）
# 变化频繁，自动维护成本远高于收益，改为审核页「复制成品」手动粘贴发布。
# 要恢复自动存草稿：设 TOUTIAO_ENABLED=1，并把 local_login.py 导出的
# data/toutiao_cookies.json、data/baijia_cookies.json 放到服务器 data/ 下。
TOUTIAO_ENABLED = os.getenv("TOUTIAO_ENABLED", "0") == "1"
