# 生产部署记录（2026-11-19）

> 已实际部署完成并验证。本文件记录真实参数，供日常维护查阅。
> 密码与密钥不写在本文件里，见服务器 `/opt/auto-content/.env`。

## 一、部署环境

| 项 | 值 |
| --- | --- |
| 服务器 | <你的服务器IP>（CentOS 7，4 核 4G，40G 盘） |
| 确认结论 | 该机为**重置后的干净机器**，无 DMS / nginx / 数据库，可放心独立部署 |
| 项目目录 | `/opt/auto-content` |
| Python | 独立 CPython 3.11.18（uv 安装，不碰系统 3.6.8） |
| 虚拟环境 | `/opt/auto-content/.venv` |
| 审核服务 | systemd 单元 `auto-content.service`，常驻，端口 **5000** |

## 二、访问方式

- **审核页地址**：`http://<你的服务器IP>:5000/?pwd=<APP_PASSWORD>`
- 密码在服务器 `.env` 的 `APP_PASSWORD` 字段（部署时生成，12 位混合）
- 不带密码访问返回 401；带密码（query 参数或浏览器 Basic Auth 弹窗）返回 200
- 已从外网实测：HTTP 200、页面完整、推送按钮可见
- ⚠️ 当前是 **HTTP 明文**。传输的密码和稿件内容未加密。若要 HTTPS，需域名 + 证书（Let's Encrypt 免费）。

## 三、AI 模型（生产实测调整）

- 主模型：`glm-4-flash`（永久免费，实测 200、0.7 秒）
- 备用：`glm-4.7-flash`（写作更强但在本机 IP 持续 429 限流，故降为备用）
- 两者都免费；主模型限流时代码自动降级到备用
- key 在 `.env` 的 `ZHIPU_API_KEY`（智谱，已验证有效）

## 四、定时任务

- cron：每天 **08:00** 执行 `/opt/auto-content/daily_job.py`
- 流程：抓真实热榜 → AI 生成 5 篇 → 推送待审稿到微信
- 日志：`/opt/auto-content/data/cron.log`
- 当前 `SERVERCHAN_KEY` 为空 → 生成正常，推送干净跳过（不报错）
- **填入 Server酱 key 后，推送自动生效，无需改代码或重启**

## 五、日常维护命令（在服务器上执行）

```bash
# 服务状态 / 重启
systemctl status auto-content
systemctl restart auto-content

# 看日志
journalctl -u auto-content -n 50 --no-pager
tail -f /opt/auto-content/data/cron.log

# 手动跑一次每日任务（验证用）
cd /opt/auto-content && .venv/bin/python daily_job.py --count 3

# 改配置后：.env 改完需要 systemctl restart auto-content；
#           config.py 在启动时就把 .env 读进内存了，不重启不会生效（原文档写反了，2026-11-20 更正）；
#           代码 .py 改完需 systemctl restart auto-content

# 查看/修改定时任务
crontab -l
```

## 六、微信推送（已打通 2026-11-19）

- Server酱 key 已写入 `.env` 的 `SERVERCHAN_KEY`，实测推送 code=0 成功。
- 每天 8:00 daily_job.py 自动推送待审稿到微信，链接带密码直达审核页。
- 网页「推送待审稿到微信」按钮也已验证返回 `{"ok":true}`。
- 前提：微信已关注"方糖"服务号，否则接口成功但收不到。

## 七、待办（可选）

1. **HTTPS + 域名**：当前 HTTP 明文。若要加密传输和更稳定的微信打开体验，
   提供域名，我配置 nginx + Let's Encrypt 证书（免费）。

## 八、手机端审核（2026-11-19 重做）

- 审核页改为 mobile-first：3 列统计、工具区折叠、大按钮、底部弹出预览。
- **复制三重兜底**：Clipboard API → execCommand → 手动复制弹窗。
  因为当前是 HTTP 明文，微信内置浏览器里 Clipboard API 可能失效，
  前两种都失败会自动弹出可长按复制的文本框，保证总能拿到成品。
- ⚠️ 改了 `.py` 代码需 `systemctl restart auto-content`；改模板重启也会自动加载。

## 九、踩坑记录

- **401 响应头中文 realm 导致请求挂起**：`WWW-Authenticate` 的 realm 含中文，
  Werkzeug 用 latin-1 编码 header 抛 `UnicodeEncodeError`，请求卡死。
  已改为 ASCII `realm="AutoContent Review"` 并本地+生产双重验证。
- **uv 清华镜像无最新 Python**：镜像同步滞后返回 404，改用官方源装成功。
- **uv 建的 venv 无 pip**：需用 `uv pip install --python .venv/bin/python` 装依赖。
- **glm-4.7-flash 持续 429**：生产 IP 访问量过大，改为主用 glm-4-flash。
- **写入 Server酱 key 后网页推送仍失败**：服务进程在写 key 前已启动，config 只在进程启动时读一次 .env。`systemctl restart auto-content` 后恢复。教训：改 .env 后若接口读不到，重启服务。
- **微信/HTTP 下复制失效**：`navigator.clipboard` 需 HTTPS，明文下不可用。加了 execCommand + 手动复制弹窗兜底。
- **401 realm 中文致请求挂起**：见上（已修）。
- **点复制/预览又弹密码框（2026-11-19 修）**：微信链接带 `?pwd=` 只验证了首页那一次请求，页面里后续 ajax（复制/预览/保存/删除/生成）都没带密码，服务器返 401——预览把"请输入访问密码"纯文字塞进弹窗，复制把纯文字当 JSON 解析报 `SyntaxError`。
  - 修复：① 服务端首次验证通过后写 Flask session（签名 cookie），同浏览器后续请求自动免密；② 前端 JS 读 URL 里的 `pwd`，所有请求（含 GET 复制/预览）兜底带上；③ 401 响应统一提示"请用推送链接重新打开"。`SECRET_KEY` 改为可从 .env 读。
  - 生产已验证：带 pwd 进首页→种 cookie→会话内复制/预览无 pwd 均 200 且内容正常；新会话无 pwd 仍 401。外网实测通过。

## 十、稿件自动清理（2026-11-19）
- 规则：待审核/已退回按 `created_at` 保留 **3 天**；已通过按 `approved_at` 保留 **30 天**（老数据退回 `updated_at`→`created_at`）；时间字段损坏则保守保留，绝不误删。
- 配置项：`config.py` 的 `KEEP_PENDING_DAYS` / `KEEP_APPROVED_DAYS`，改完需重启服务。
- `approved_at` 在稿件**首次切到 approved** 时写入；已通过后再次编辑不刷新；退回待审再重新通过会刷新起算点。
- 触发时机：① 每天 8:00 daily_job → `pipeline.run()` 生成后自动清理；② 网页端一键生成同样触发；③ 手动 `cd /opt/auto-content && .venv/bin/python pipeline.py --cleanup` 只清理不生成。
- 边界已本地测试：3/30 天整保留、超一点即删、退回稿按 3 天、老数据回退时间、异常时间不误删——全部符合预期。生产首次执行删 0 篇（存量均为当天稿）。
- 部署前已备份：`app.py/config.py/pipeline.py` 的 `.bak_cleanup` 副本 + `data/queue.json.bak_cleanup`，误删可恢复。

## 十一、AI优化重写（2026-11-19）
- 需求：已通过列表里，把「保存并通过」主按钮换成「✦ AI优化 · 重写本篇」，点击后基于原稿真实素材（topic/desc/url/source）调大模型重写，覆盖当前稿件。
- 用户确认的口径：**覆盖当前稿 + 保持已通过状态 + 按原素材重新写**（非润色）。
- 实现：
  - `pipeline.py`：`build_optimize_prompt()`（二次重写提示词，真实性红线不变）+ `optimize_article(id)`（仅 AI 成功才写回，失败原稿不变）。
  - `app.py`：`POST /optimize`，与生成共用 `generate_lock` 防并发写队列；未登录 401、缺 id 400、失败给明确错误且不破坏原稿。
  - `review.html`：仅 `status=approved` 显示 AI优化按钮；二次确认 + 按钮禁用防重复 + 进度提示 + 成功刷新。
  - 覆盖时不刷新 `approved_at`（防止反复点优化让稿件永不过期），`mode` 记为「AI优化重写」。
- 验证：本地 mock 测试（成功覆盖/状态保持/失败不破坏/不存在 id）+ 接口鉴权全过；生产用真实模型端到端跑通（临时测试稿，已还原，真实队列 4 篇未动）。
- 备份：`app.py/pipeline.py/templates/review.html` 的 `.bak_opt` 副本 + `data/queue.json.bak_opt`。

## 十二、选题改为 AI/数码科技方向（2026-11-19）
- 用户选择：方向=AI/数码科技资讯；选题源=仍用全网热榜筛选；存量稿件=清空。
- 改动（只改配置，抓取/筛选代码不动）：
  - `config.py`：`WRITING_NICHE` / `WRITING_STYLE` 改为科技资讯；`KEYWORDS_STRONG`（22 词：AI/大模型/OpenAI/DeepSeek/芯片/华为/机器人…）、`KEYWORDS_WEAK`（22 词）换成技术词；`FALLBACK_TOPICS` 换成 5 条技术保底选题。
  - `hot_topics.py`：仅更新模块注释（"守护父母"→"AI/数码科技"），筛选逻辑读 config 词表，无需改代码。
- 踩坑：关键词"苹果"误伤——热搜"多让孩子啃苹果"被判技术话题（3 分）。已改为"苹果手机"/"iPhone"避免水果误匹配，复测水果 0 分、苹果手机 3 分。
- 部署：清空队列（`/opt/auto-content/data/queue.json` 写 `[]`），旧稿备份 `queue.json.bak_topic` + `config.py.bak_topic` / `hot_topics.py.bak_topic`。
- 验证：生产真实热榜筛出 4 条技术话题（AI作弊/DeepSeek融资/机器人/智能吉他）；队列 0 篇；审核页 HTTP 200；cron 仍在。
- 以后换方向：只需改 `config.py` 的 `WRITING_NICHE`/`WRITING_STYLE`/两组关键词/`FALLBACK_TOPICS`，重启服务即可。

## 十三、选题源扩展：GitHub + AI技术网站（2026-11-19）
- 需求：选题源加入 GitHub 和 AI 技术类网站。**先实测服务器（境内）连通性再落地**，不凭经验假设。
- 实测结论（服务器 <你的服务器IP>）：
  - ✅ GitHub Trending 200/2.2s、GitHub Blog RSS 200/0.9s、HackerNews 200/1.3s、量子位 200、InfoQ中文 200
  - ✅ GitHub Search API 200/0.9s（结构清晰，优于解析 HTML）
  - ❌ HuggingFace 连不上；❌ 机器之心/36氪 RSS 返回 0 条（地址无效）；⚠️ arXiv 14.4s 太慢
- 实现：
  - `hot_topics.py` 新增 `fetch_github()`：GitHub 官方 Search API 抓近 7 天 `AI in:name,description stars:>100` 的仓库，返回 title/desc/url/hot(stars)/source=GitHub热门，失败静默降级。
  - `config.py`：`TOPIC_SOURCES` 填入 4 个实测可用源（GitHub Blog/量子位/InfoQ中文/HackerNews）；新增 `USE_GITHUB_TOPICS=True` 开关。
  - `pipeline.py`：`fetch_topic_items()` 改**多源配额制**（关键改动）：热榜约 40%（至少1条）+ GitHub 最多 2 条 + RSS 补齐 + 备用词保底，按 url 去重。新增 `_fetch_rss_items()` 带真实摘要和链接（原逻辑只取标题且仅兜底）。
- 效果（本地/生产一致，5 篇）：头条1 + 百度1 + GitHub热门2 + GitHub Blog1，全部带链接，摘要除头条外均有。
- 设计意图：原逻辑热榜够数时 RSS 完全不生效；改配额制后 GitHub/技术媒体稳定参与，选题更垂直、更有新鲜度。
- 可调：想提高 GitHub 占比，改 `fetch_topic_items` 里 `take(hot_topics.fetch_github(...), 2)` 的第 3 个参数；想加源改 `TOPIC_SOURCES`。
- 备份：`config.py/hot_topics.py/pipeline.py` 的 `.bak_src` 副本。
- 已知限制：头条热榜接口不提供摘要（代码注释已说明，AI 写作时按"无摘要"约束处理，不编造）。

## 十四、选题源改为国内技术站（2026-11-19）
- 背景：用户反馈境外站连不通，要求换国内技术类网站 / 技术热搜。**先实测再落地**（境内服务器逐站连通性 + RSS 条目质量）。
- 实测结论：
  - ✅ 均可 0.1–0.7s 访问：IT之家 RSS(60条/摘要470-736字)、开源中国(50条/212字)、雷峰网(20条/300字)、爱范儿(20条)、博客园(20条/150字)、量子位(10条)
  - ✅ 掘金热榜 API（GET+Referer，50 条，有 content_id/hot_rank）；❌ CSDN 热榜（521 反爬，本机与服务器均被限流，弃用）
  - ❌ 排除：HuggingFace/V2EX 连不上、知乎 401、微博 403、虎嗅超时、品玩/机器之心非标准 RSS、InfoQ 摘要只有"点击查看原文"
- 实现：
  - `hot_topics.py` 新增 `fetch_juejin()`（掘金技术热榜）；`collect()` 新增 `require_desc` 参数（默认 True，只返回带真实摘要的条目）。
  - `config.py`：`TOPIC_SOURCES` 换成 6 个国内源；`USE_JUEJIN_TOPICS=True`；GitHub 降为兜底。
  - `pipeline.py`：`_fetch_rss_items()` 改**按源轮转取**（避免 IT之家 60 条占满配额）；`fetch_topic_items()` 改分层策略——百度热榜(有摘要)40% → 国内RSS轮转补齐 → 掘金兜底1条 → 头条兜底 → GitHub兜底 → 词库。
- **修掉三个真实 bug**（实测中发现）：
  1. **百度链接错配**：原实现只在 `word` 之后 1500 字符窗口找 `"url"`，当前条目缺 url 时会抓到**下一条**的链接（标题"DeepSeek融资"配到"普京…"链接）。改为按 `word` 切块（上一 word 结束→本 word 结束），在该块内取 desc/url/hotScore。生产 52 条实测**零错配**。
  2. **提示词教 AI 编造**：原文硬要求写着"需要举例时用'比如有些家庭会……'这类泛化表述"，等于给编造模板。改为**严禁虚构具体场景举例**，并针对"只有标题"的选题改成只写"概念澄清+应对方法"、明确禁止描述事件经过。
  3. **推测归因被当分析**：素材只说"完成融资、31家入股"，AI 自己补"图像识别优势""团队经验丰富"。新增硬要求：素材未说明原因/动机/背景时不得推测，宁可写"具体原因未披露"。
- 内容质量同步调整：`WRITING_STYLE` 去掉"亮点/局限/影响"模板（导致机械套壳）、禁标题党词（揭秘/震惊/炸裂/盛宴）；字数从 800-1200 降到 600-900，无素材选题 400-700，明确"素材少就写短，不要注水"。
- 验证：生产 5 条选题全部带真实素材（96-300 字摘要），来自 5 个不同国内源；百度 52 条零错配；服务 active；审核页 200；cron 仍在；队列 0 篇。
- 备份：`config.py/hot_topics.py/pipeline.py` 的 `.bak_cn` 副本。
- **已知限制**：免费模型 glm-4-flash 仍会写"表明机构看好前景"这类软性推测，提示词已尽力压制但无法根除；要好效果建议换 glm-4.7-flash 或更强模型。

## 十五、成品署名改为公众号新名（2026-11-19）
- 背景：公众号由「35岁前的Plan B」改名为「Plan B 的AI笔记」，正文方向已转 AI/数码科技。
- 改动：`config.py` 的 `SITE_AUTHOR` 由写死的「远方守护」（早期别的项目残留）改为 `os.getenv("SITE_AUTHOR", "Plan B 的AI笔记")`。
- 生效范围：仅 `/preview/<id>` 成品预览的署名行（`{{ author }} · 发布于 头条号 / 百家号 / 公众号`）；`/plain/<id>` 复制文本不含署名。
- 以后换名：服务器 `.env` 加一行 `SITE_AUTHOR=新名字` 即可，不用再改代码（重启服务生效）。
- 本地验证：`/preview/` 渲染 200，署名行输出正确。
- 已部署（同日）：服务器 `/opt/auto-content/config.py` 已同步，备份 `config.py.bak_author`，`systemctl restart auto-content` 后 active。
- 线上验证：队列 3 篇，`/preview/a08c9bc65c16` 署名行输出 `Plan B 的AI笔记 · 发布于 头条号 / 百家号 / 公众号`；审核页 200。
- 注：该机 root 走密码登录（公钥不通），主机指纹与早期记录不同；部署用 paramiko + `SSHPASS` 环境变量，密码不入仓库。

## 十六、公众号头图自动生成（2026-11-19）
- 需求：稿件审核通过后，自动配一张对应的公众号头图（不要每次手动找图）。
- 方案取舍：
  - 用**智谱 CogView-3-Flash**（免费，服务器直连实测 8-12 秒/张）；实测 `cogview-4` 返回 1113 余额不足，故用 3-flash。
  - 直接请求 **1808x768（精确 2.35:1）**，和公众号大图 900x383 同比例，避免服务器装 Pillow 和中文裁剪（该机无 PIL、无中文字体）。
  - **不抓原文配图**：各平台有防盗链且图片版权不清，统一 AI 生成。
  - 图上不压标题文字（中文小字易糊，公众号列表本身显示标题）；平台自动右下角打「AI生成」标识，符合生成式内容标识要求。
- 实现：
  - 新增 `cover.py`：`build_cover_prompt()`（标题关键词→画面意象，6 组规则 + 兜底；硬禁文字/logo/人脸）+ `generate_cover()`（失败重试 1 次，任何失败返回 None）+ `--backfill` 命令行补图。
  - `app.py`：`/update` 首次转 approved 时置 `cover_state=generating` 并起后台线程，**不阻塞通过操作**；`GET /cover/<id>` 出图；`POST /cover/<id>/regenerate` 换一张；失败只写 `cover_state=failed`，稿件不受影响。
  - `review.html`：已通过卡片新增头图区（缩略图 + 生成中占位 + 「换一张」+「下载头图」），生成中 10 秒自动刷新。
- 验证：本地端到端（approve→12.4s 出图→/cover 200→重复通过不重生成）；生产端到端（临时测试稿 approve→覆盖生成 240KB→删除测试稿与图，队列回到 3 篇）。
- 已验证产物：`data/covers/a08c9bc65c16.jpg`（NGINX 漏洞稿，110KB）。
- 备份：`app.py.bak_cover`、`templates/review.html.bak_cover`。
- 成本：cogview-3-flash 免费；5 篇/天≈5 张/天，耗时约 1 分钟，仅在"审核通过"时触发。

## 十七、复制成品改为富文本（2026-11-19）
- 问题：复制到公众号后小标题竖线、加粗、列表圆点全丢，只剩纯段落。原因：`/plain` 只返回纯文本（strip_html 去标签），剪贴板里没有 text/html。
- 根因补充：公众号编辑器**只保留内联 style**，会丢弃 `<style>`、class 和外链 CSS。所以光有 HTML 标签也不够，必须把样式写进每个标签的 style 属性。
- 实现：
  - 新增 `wechat.py`：`to_wechat_html()` 用 HTMLParser 把 h2/p/li/strong/a 全部翻译成带内联 style 的 `<p>`/`<section>`（h2 → 左侧 3px 蓝色竖线小标题；li → `•&nbsp;` 圆点段落，因为公众号对 `<ul><li>` 清洗不稳定会丢点）；`to_plain_text()` 作纯文本兜底。
  - `app.py`：`/plain/<id>` 增加 `html` 字段（`content` 改为**正文不含标题**——三个平台都是标题、正文分开录入）；`/preview/<id>` 改用同一份 `to_wechat_html`，保证预览所见 = 粘贴所得；删除已无用的 `strip_html`。
  - `review.html`：复制逻辑改为 `copyRich()` 三级兜底——① ClipboardItem 同时写 text/html + text/plain（需 HTTPS/localhost）② execCommand 把 HTML 塞进离屏 `#richHolder` 选中复制（HTTP 下也带格式）③ 弹窗改成 `contenteditable` 富文本区，长按复制同样带样式。
- 验证：本地 `/plain` 返回 html 30 处内联样式；`/preview` 含 border-left:3px；生产已部署，NGINX 稿 html 14 处内联样式、3 个列表圆点，index/preview 均 200。
- 备份：`app.py.bak_rich`、`templates/review.html.bak_rich`。
- 注意：`content` 语义已变（不再含标题行），前端 toast 明确提示「标题单独填」。

## 十八、写作模板改造：结论先行 + 可执行（2026-11-19）
- 起因：那篇 NGINX 稿被行家一看就知道是机器写的——没有版本号、没有自查命令、三节内容互相重复、结尾三条全是空话。根因不在文笔，在**提示词只要求"分点+小标题"，没要求"读完能办事"**。
- 改动：
  1. `config.py` 新增 `ARTICLE_SKELETON`：6 段结构写死——①80 字内钩子（禁"近日/据悉/随着"）②先给答案（有版本号就写，没有就说"以官方公告为准"）③3 个陈述句小标题（禁设问）④每节首句加粗、段落≤120 字 ⑤命令块只允许查看类和动作类，配置示例行必须来自素材 ⑥结尾一个明确动作。`build_prompt` 与 `build_optimize_prompt` 均引用，并附"自检三问"。
  2. `pipeline.markdown_to_html` 重写：`**粗**`→`<strong>`（原来直接剥掉，导致重点全丢）、``` 围栏→`<pre><code>`、行内 `` ` ``→`<code>`。
  3. `wechat.py` 支持 `<pre>`：转深色 `section` + 等宽字体 + `<br>` 换行（公众号会吃掉 pre 的换行和缩进）。
  4. `pipeline.structure_issues()` + 一次重试：检查"无小标题/超 4 个/设问句/加粗不足/空话过多/句子重复/标题数字与素材不符"，不达标就把问题清单回灌模型重写一次，选问题更少的那版。
  5. `pipeline._smart_summary()`：RSS 摘要不再硬截 300 字，改为保留前两句 + 所有含数字的句子（版本号/CVE/金额几乎都在这些句子里）。之前"修复版本 1.30.7"就是这样被截掉的。
  6. 主模型切回 `glm-4.7-flash`（结构遵循明显更好），`glm-4-flash` 降为限流备用。
- 实测：4.7 能主动写出钩子、陈述句小标题、加粗；但**今晚 4.7 多次返回 429 限流**，自动降级到 4-flash 后质量明显回落（小标题变 4 个、无钩子）。重试机制实测生效（"小标题 5 个 + 设问句"→ 重写后通过）。
- 已知限制：4-flash 仍会软性推测归因、会把"1000 亿"写成"百亿"（中文数量级错位，代码只能校验阿拉伯数字）。要好效果得换付费模型（deepseek-chat 约 1 元/百万 token）。
- 备份：`config.py.bak_skeleton`、`pipeline.py.bak_skeleton`、`wechat.py.bak_skeleton`。
- 验证：生产 `build_prompt` 含结构模板与命令约束、`structure_issues` 正反例均符合预期、富文本 pre 正常渲染、审核页 200、服务 active。

## 十九、写作格式按「爆款结构」补齐（2026-11-19）
- 起因：整理出公众号爆款文的通用格式（标题公式 / 钩子 / 三节 / 结尾动作 / 排版 / 自检）后对照代码，发现模板只覆盖了「结构」，还缺 5 项：标题公式、记忆点密度、段落长度上限、结尾禁套话、加粗密度上限。
- 改动：
  1. `config.py` 新增 `TITLE_RULE`（四公式：具体信息点 / 数字盘点 / 反常识 / 利益直给；12-20 字；禁"揭秘/震惊/速看/炸裂/马上删"），两个 prompt 共用，替换原来那句"20 字以内"。
  2. `ARTICLE_SKELETON` 补：开头四种钩子写法、每 300 字必须有一个记忆点、段落 120→**100 字**、结尾禁"综上所述/希望对你有帮助"、全文加粗**≤5 处**。
  3. `structure_issues()` 新增 7 类检查：标题超 22 字、标题诱导词、开头新闻体套话、单段超 120 字、加粗超 8 处、结尾总结套话、把 CVSS 说成"满分"。
  4. 新增 `split_long_paragraphs()`：超长段按句自动拆开。切点用 `(?<=[。！？])(?!</)`，再用标签栈校验每句配平，**标签跨句就整段放弃**（不冒险产出坏 HTML）。
  5. 新增 `strip_unsafe_config()`：删掉代码块里素材没给过的配置行。模型实测写出过 `listen 80 http2;`（HTTP 端口配 HTTP/2，语法本身就是错的）——这种示例读者照抄能把服务改挂。整块只剩注释的也一并删掉（深色空块太突兀）。
  6. 新增 `tidy_spaces()`：删中文（含中文标点）之间的半角空格，数字与英文两侧的空格保留。
  7. `wechat.py` / `preview.html` 排版：正文 16→**17px**、行高 1.9→**1.95**、字距 .3→**.4px**、段距 18→20px、小标题 18→19px。
- 验证：本地反例一次命中 7 条、正例 0 条；端到端跑 3 轮，最后一轮标题 21 字、**残留问题 0**、3 个小标题、5 处加粗、2 个纯命令代码块、15 段最长 103 字，编造配置行被自动剥离。重试机制实测拦下过 27 字标题。
- 生产验证：7 项配置/函数检查全 True，index 200、preview 200、服务 active、队列 3 篇未受影响。
- 备份：`config.py.bak_viral`、`pipeline.py.bak_viral`、`wechat.py.bak_viral`、`templates/preview.html.bak_viral`。
- 已知限制：标题超 22 字只报错不硬截（截断会毁语义），重试一次仍不达标就交人工；中文数量级错位（"1000 亿"写成"百亿"）代码拦不住。
- 补强（同日 19:45，第二次提交）：
  - `split_long_paragraphs()` 改成「安全切点」算法：只挑句末、后面不紧跟 `</`、且该位置标签栈为空的地方切。模型常把两句话一起加粗（`<strong>甲。乙。</strong>`），句号在标签内部，旧逻辑会整段放弃拆分（实测残留 135 字段落），新逻辑改从段内其他句号切。
  - `structure_issues()` 新增「结尾没有给出动作或提问」：结尾段没有问号、也不含检查/查看/确认/升级/试试/看看等动作词就报。
  - 新增 `ensure_bold()`：最终稿加粗不足 3 处时，把小标题后第一段的首句包成 `<strong>`，只改标记不改文字。这是给限流降级（glm-4-flash 一个加粗都不写）兜底。
- 二次实测：4.7 连续两轮 429，4-flash 产出「标题 25 字 + 6 个小标题 + 设问句 + 编造配置行」，四项被重试与代码层修复逐条挡下，最终残留仅「加粗不足」，由 `ensure_bold` 补齐。

## 二十、本地三项优化（2026-11-19 夜，分支 feat/gzh-polish，未部署）
- 前提：用户要求先在本地分支跑通，暂不动服务器。分支 `feat/gzh-polish`，main 保持不动。

### 1) 公众号排版校验（wechat.py）
- 参考 isjiamu/gzh-design-skill：把「粘过去会不会掉格式」从肉眼看改成代码查。
- `validate_html()` 查转换产物：黑名单标签（style/script/iframe/link/form/input/button/video/audio）、`class` 属性、段落缺内联 style、标签配平、公众号会过滤的 CSS（position/grid/flex/float/z-index）、非 https 图片、空段落。分 error/warn。
- `validate_source()` 查原始正文：带 `<style>`/`class`/`<div>` 说明是从别处粘来的富文本，转换时原样式会丢或布局变形，提前提示。
- 接入审核页：卡片 meta 区新增徽章（绿=排版 OK / 黄=有提醒 / 红=N 处会掉格式），悬停看原因；`/plain` 增加 `check` 字段。
- `wrap_leaf()` 可给裸文本包 `<span leaf="">`，由 `config.WECHAT_LEAF_WRAP` 控制，**默认关闭**——现有粘贴效果已验证正常，先观察再说。

### 2) 选题源健康检查 + DailyHot 可选源（hot_topics.py）
- 原计划用 DailyHotApi 替换手写抓取，**实测走不通**：公共实例 `api-hot.imsyy.top` 在本机 curl（代理 502 / 直连无出口）与 WebFetch 两条通道均不可达；36kr 全路径返回同一个 25KB 反爬拦截页。故不硬接。
- 改为两件能落地的：
  - `source_health()`：逐源探测 4 个热榜接口 + 6 个 RSS，输出状态/条数/耗时/错误。`python hot_topics.py --health`。**实测立刻发现真问题**：检查只数 `<item>`，把 Atom 格式（`<feed><entry>`）的博客园误判成挂了，已修，现 10/10 全通。
  - `fetch_dailyhot()`：多实例按序回退 + 字段宽容解析（title/name/word、desc/description、url/mobileUrl）、失败返回空列表不抛异常。`config.USE_DAILYHOT` 默认 false；`pipeline.fetch_topic_items` 步骤 3 接入，开启且带摘要时补 20% 配额。验证命令 `python hot_topics.py --check dailyhot`。

### 3) 微信草稿箱发布（publish.py，新增）
- 解决「最后一步还得手动复制粘贴」。
- `get_access_token()` 带本地缓存（`data/wechat_token.json`，按 appid 区分，提前 5 分钟过期）；`upload_thumb()` 把头图以 thumb 永久素材上传；`create_draft()` 调 `draft/add`，`json.dumps(ensure_ascii=False)` 否则中文进后台变 \uXXXX。
- 错误码翻成人话（40001/40013/40164 IP 白名单/48001 未认证/45009 限额等），报错直接说怎么办。
- `app.py` 新增 `POST /publish/<id>`：未配凭据 400、未通过审核 400、无头图 400，成功回写 `draft_id`/`draft_at`。审核页已通过卡片加绿色「推到草稿箱」按钮，未配凭据不渲染，无头图时禁用。
- 自检：`python publish.py --check`（凭据 → token → 素材接口权限）。
- 离线验收 21 项全过（mock 微信接口，不真发请求）：token 缓存、封面上传 type=thumb、body 中文不转义、articles 结构、标题截断、3 个错误码翻译、完整链路、缺封面/缺凭据提示。

### 其他
- 新增 `.env.example`（只有变量名没有值），`.gitignore` 加 `!.env.example` 例外。
- 回归 15 项全过：三个列表页 200、排版徽章、plain/preview、正常稿 0 error 且脏 HTML 能检出、选题 5 条、健康检查全通、未配凭据时推送接口与按钮表现正确。
- **未部署服务器**；分支未合并 main。

## 二十一、选题源扩到八路 + 长稿改造（2026-11-20，main）
- 前提：上一分支 `feat/gzh-polish` 的三个提交已 fast-forward 合并进 main（排版校验 / 源健康检查 / 草稿箱）。
- 起因：用户看到公众号专家能抓七大平台（百度/微博/抖音/知乎/头条/微信搜一搜/快手），要求把自己这套也补上；同时反馈"提交审核的文章有点短"。

### 1) 选题源：从 2 个热榜扩到 8 路
全部先实测再接，一个都没凭记忆写。实测结论（2026-11-20 08:50 本地）：

| 平台 | 接口 | 结果 | 摘要 |
| --- | --- | --- | --- |
| 知乎热榜 | `feed/topstory/hot-list-web`（必须带 `limit=20&desktop=true`，否则只回 3 条） | ✅ 20 条 | 有，**质量最高**（excerpt_area 是整段问题描述） |
| 百度热搜 | `top.baidu.com/board` | ✅ 52 条 | 有 |
| 微博热搜 | `weibo.com/ajax/side/hotSearch`（要浏览器 UA + Referer） | ✅ 20 条 | note 多数等于标题，这种情况视为无摘要 |
| 微信侧 | 搜狗微信 `weixin?type=2` 文章搜索 | ✅ 12 条 | 有摘要 + 公众号名 |
| B站热搜 | `x/web-interface/search/square` | ✅ 15 条 | 无 |
| 抖音热榜 | `iesdouyin.../billboard/word/`（备用 aweme snssdk） | ✅ 20 条 | 无 |
| 快手热榜 | `kuaishou.com/graphql` visionHotRank | ✅ 20 条 | 无 |
| 头条热榜 | 原接口 | ✅ 50 条 | 无 |

- **微信搜一搜没有公开接口**：榜单只在微信客户端里，搜狗微信老热文榜 `pcindex` 已下线（实测 404）。改用搜狗微信的公众号文章搜索——不是榜单，是"同行正在写什么"，带真实标题+摘要+链接，对找角度反而更有用。频控严，关键词限 3 个以内，命中验证码就跳过剩余词。
- 快手踩坑：`visionHotRank` 只接受 `page` 一个参数，多传 `period` 直接 `GRAPHQL_VALIDATION_FAILED`；返回里 `items` 和 `result` 是兄弟字段（`result` 是数字 1），不是 `result.items`。
- 知乎踩坑：不带参数默认只回 3 条，必须显式 `limit=20&desktop=true`。
- 摘要有无已分档写入 `desc`，无摘要的一律留空，下游不会据此编造。

### 2) 源接入与配额（pipeline.fetch_topic_items）
- 热榜配额 40%→**50%**（至少 2 条）。
- **公众号搜索结果最多留 1 条**：实测它 12/12 全命中领域关键词，会把 5 个热榜位占掉 2 个，而那是同行写过的稿，多了就是洗稿。
- 抓取顺序按"能不能写出深度稿"排：知乎/百度/微博/公众号（有摘要）在前，B站/抖音/快手/头条（只有标题）在后，标题类只兜底。
- 关键词表补：混元/千问/元宝/Claude/Gemini/Llama/智能体/Agent（强），副业/搞钱/兼职/自由职业/远程办公（弱）——账号定位是"AI + 副业"。
- 各源独立开关，`.env` 里写 `USE_WEIBO_TOPICS=0` 之类即可关，不用改代码。

### 3) 稿件变长：规则 + 代码校验 + 扩写三道
- 原因：提示词写的是"600-900 字"，同时还有一句"素材撑不满时宁可写短"，两条规则打架，模型一律走最短。
- `config.ARTICLE_WORDS` 按素材厚度分三档，全部调高：

| 素材厚度 | 判定 | 目标字数 | 最低要求 |
| --- | --- | --- | --- |
| rich | ≥150 字 | 1300-1700 | 1100 |
| thin | 40-150 字 | 1000-1300 | 850 |
| title_only | <40 字 | 900-1200 | 750 |

- `ARTICLE_SKELETON` 新增「怎么把文章写厚而不注水」：五类合法展开（影响分岔/判断标准/机制解释/常见误解/操作路径），挑 2-3 类，明确禁止"对普通用户而言…对从业者而言…"这类模板排比。
- `structure_issues` 新增三项硬校验：正文总字数、**每节 ≥220 字**、模板排比≥3 处、填充词≥4 次。
- 新增 `expand_article()`：结构只差字数时，用同一素材扩写，保留原判断不动观点。验收卡四道：只差字数才扩、字数必须变多、不许超上限 1.2 倍、不许引入新款毛病。
- 生成流程兜底：初稿 → 结构自检（含字数）→ 重写一次 → 仍短则扩写最多两次。
- 优化（重写）路径同样接入扩写，提示词字数从 800-1200 提到分档目标。

### 4) 顺手修掉的真 bug（都是实测撞出来的）
- **扩写注水**：第一次只比字数，模型把 1062 字灌到 1327 字，塞进 9 处"对…而言"排比。现在代码层拦（见上）。
- **扩写冲过头**：弱模型一口气写到 2034 字，还写出"（此处原文中断，未给出完整内容）"这种 meta 话。已加 1.2 倍上限 + `strip_meta_notes()` 清理。
- **小标题套加粗**：模型爱写 `## **变革难点一**`，转出来是 `<h2><strong>…</strong></h2>`，公众号里等于标题又粗又带星号。新增 `clean_heading()` 剥掉。
- **标题重复成第一小节**：模型把标题又原样写成第一个 `##`。新增 `drop_title_heading()`。
- **46 字标题**：只在中文标点处切，切出来不足 8 字就放弃（不切出半句话）。实测 46 字 → 14 字。
- **请求超时**：长稿生成常超 120 秒，`AI_TIMEOUT` 提到 240 秒。
- **整段加粗**：加粗超 5 处时，把"整段只有一句加粗"的拆掉，从前往后拆，保留 5 处。

### 5) 旧稿回填
- 已有队列里的稿子是旧规则写的（10 篇，平均 920 字，最短 609 字），规则改了不会自动变。
- 新增 `python pipeline.py --expand`（可带篇数 `--expand 3`）：只处理 pending 且字数不达标的，已通过/已发布不碰。
- 实测两篇最短的都被跳过——它们是 5 个小节的结构硬伤，扩写只会放大毛病。这种要"重写"（审核页的优化按钮）而不是拉长。

### 6) 验证
- 源健康检查：**16/16 全通**（8 路热榜 + 掘金 + GitHub + 6 个 RSS）。
- 各源领域命中率实测（当日热榜被时政话题占满，参考价值有限）：知乎 3/20、百度 3/52、微博 0/20、抖音 1/20、快手 1/20、B站 0/15、公众号搜索 12/12。
- 选题分布（limit=5）：知乎 1 + 公众号 1 + RSS 3；limit=8 时知乎能占到 3 条。
- 离线 mock 验收 4 项全过：短稿 795→1389 字被采用、注水扩写被拒、小标题加粗被剥、46 字标题被截。
- 服务重启后 `/`、`/?tab=approved` 均 200。

### 7) 已知限制
- **主模型 4.7 早高峰持续 429**，降级到 glm-4-flash 后写不到 1100 字（实测 628-652 字、4 个小节、标题里套加粗）。长度改善要等 4.7 恢复才看得出。此时点"优化"重写，弱模型反而更差（会写出"…真的有效吗？"这种设问标题），**建议直接等**。
- 微博/B站/抖音/快手当天的榜单以时政娱乐为主，领域命中率天然低；这是账号定位决定的，不是源的问题。
- 搜狗微信的链接是带 token 的跳转地址，会过期；文末"信息来源"长期看可能失效。
- 旧稿结构硬伤的（5-8 个小节）无法靠扩写修好，只能重写。

### 8) 生产部署（2026-11-20 11:03，main @ cb09d42）
- 上传方式：paramiko SFTP（服务器无 git，root 密码登录）。上线文件 11 个：`app.py` `config.py`
  `pipeline.py` `hot_topics.py` `wechat.py` `publish.py`（新增）`cover.py` `templates/review.html`
  `templates/preview.html` `DEPLOYED.md` `.env.example`（新增）。
- 备份：服务器 `.bak_20261120_1103/`（含 templates 子目录），`.env` 与 `data/` 一律不动。
- 上服务器才发现：`publish.py` 之前只提交了仓库、**没传过服务器**，`wechat.py` 也是 19:21 的旧版
  （7.2KB vs 16.9KB）。这次一并补上，草稿箱按钮和富文本复制在线上才真正可用。
- 编译 + 重启：`py_compile` 8 个文件全过，`systemctl restart auto-content` 后 active。
- 生产验证：
  - 源健康检查 **16/16 全通**（机房出口网络比本地还快，知乎 339ms、快手 519ms、B站 156ms）。
  - 选题分布（limit=5）：百度 1 + 公众号 1 + IT之家 1 + 开源中国 1 + 雷峰网 1。
  - 页面：`/?pwd=xxx` 200，待审列表 9 处「待审」标记，队列数据完好；不带参数 401（鉴权正常）。
  - `publish.py --check`：AppID/AppSecret 仍未配（有意为之，未认证订阅号调不通），其余项通过。
  - cron 未动：`0 8 * * * cd /opt/auto-content && .venv/bin/python daily_job.py`。
- 热榜 0 命中时的行为已实测：把 `hot_topics.collect` 置空后仍能出 3 条（RSS 全量补齐），
  不会出现"没选题"或退回编造，故命中率低不需要改代码。

> 回滚：见第二十四节——该备份目录已于 2026-11-20 11:40 清理，现统一放 `.backups/`。

## 二十二、支持切 DeepSeek（2026-11-20，main）
- 背景：智谱 4.7 早高峰限流，准备接 DeepSeek 兜底。
- 修正两处会直接报 400 的旧配置（原值是 2026 年初的写法）：
  - `base_url` 由 `https://api.deepseek.com/v1` 改为 `https://api.deepseek.com`（官方文档不再需要 `/v1`）。
  - 模型名 `deepseek-chat` 已于 **2026-07-24 下线**，改为 `deepseek-flash`（V4.1-Flash）；
    同批下线的还有 `deepseek-reasoner`。
- `AI_PROVIDER` 改为读环境变量：`.env` 写 `AI_PROVIDER=deepseek` 即切换，不用改代码；
  写错名字只打日志回退 zhipu，不会导致服务起不来。
- 新增 `AI_THINKING`（默认 0）：DeepSeek V4.1 与智谱 4.7 都默认开深度思考，
  写稿用不上推理，开着更慢更贵；默认在请求体里传 `thinking={"type":"disabled"}`。
- DeepSeek 故意不设备用模型：429 只在 2500 并发时出现，日常碰不到；
  降级到 `deepseek-v4-pro` 单价约 4 倍多，宁可单篇失败走素材卡。
- 计费参考（2026-11 官方价，1M token）：`deepseek-flash` 闲时 $0.15 入 / $0.60 出，
  忙时翻倍；忙时是 UTC 01:00-04:00 与 06:00-10:00（北京时间 09:00-12:00、14:00-18:00）。
  **每天早上 8 点的 cron 正好落在闲时**（UTC 00:00），单价减半。
- 验证：`AI_PROVIDER=deepseek` 下解析出 `https://api.deepseek.com/chat/completions`、
  `model=deepseek-flash`、思考已关；智谱路径（4.7 关思考 / flash 不传）行为未变。

## 二十三、4.7 主 + DeepSeek 兜底（2026-11-20，main）
- 背景：智谱免费档约 1 QPS，早高峰 429；原兜底是同平台 `glm-4-flash`，会写出
  "懒人养生，真的有效吗？"这类设问标题，比不出稿还糟。
- 新增跨平台兜底：`AI_FALLBACK_PROVIDER=deepseek`。`_call_model` 的入参由"模型名"
  改成 target dict（provider/base_url/model/api_key）——跨平台时地址和 key 都要换，
  只传模型名会把 `deepseek-flash` 发到智谱接口上，必 400。
- `config.ai_targets()` 统一决定尝试顺序：主模型 → 跨平台兜底；没配跨平台时才用同平台备用模型。
  限流与报错都会切换（免费档的 429、偶发 5xx 都算"这家现在不能用"）。
- 头图 key 与写作模型解耦：新增 `config.ZHIPU_IMAGE_KEY`，`cover.py` 改用它。
  否则哪天主力换成 DeepSeek，AI_API_KEY 变成 DeepSeek 的 key，CogView 会静默失效。
- 实测（11:20）：4.7 当时正在限流，日志 `glm-4.7-flash 限流 → 换 deepseek/deepseek-flash`，
  兜底稿 1339 字、4 处加粗、4 个小节、标题 14 字，只剩"结尾缺动作"一项待人工补。
- 顺带修两个真 bug（A/B 时撞出来的）：
  - **标题被截到 8 字**：原逻辑只取第一个标点前内容，遇到"混元大模型开源后，……"
    就切出 8 个字的空壳。改为首句不足 12 字时丢掉首句、用后面完整子句（实测 15-22 字）。
  - **加粗 23 处**：结构自检报了但重写未必改掉，新增 `cap_bold()` 兜底——
    每节只留第一句加粗、总数 ≤5。
- 成本：只有 4.7 不可用时才走 DeepSeek。按 flash 闲时 $0.15/$0.60 每 1M，
  一篇约 ¥0.04，全量兜底也只 ¥6/月；8:00 的 cron 落在闲时（UTC 00:00）半价。

## 二十四、服务器备份清理 + 备份新约定（2026-11-20 11:40）
- 起因：用户看到 `/opt/auto-content` 根目录一排 `*.bak_*`，问"怎么这么多备份文件"。
- 清点结果：根目录 25 个散文件（344K）+ `templates/` 5 个（81K）+ 3 个整目录快照（408K），
  合计约 750K。磁盘 4.1G/40G（11%），**不是空间问题，是认不出哪份能回滚**。
- 来源：11-19 做功能时每部署一次就存一份 `文件名.bak_<功能名>`（cleanup/auth/topic/opt/src/
  cn/author/cover/rich/skeleton/viral/break 共 12 轮）；11-20 改成整目录快照 `.bak_20261120_xxxx`。
- 已删：根目录 25 个 + `templates/` 5 个散文件、`.bak_20261120_1103/`、`.bak_20261120_1125/`。
- 已保留：`.backups/20261120_1125_pre-deepseek/`（原 `.bak_20261120_1165/`——原目录名分钟位写成 65，
  不是合法时间，本次改名修正）。内含 `config.py` `pipeline.py` `DEPLOYED.md` 三个文件。
  **注意它不是完整快照**：部署脚本只备份"即将被覆盖的文件"，所以 app.py / templates 不在里面，
  回滚这两类要从 git 取。
- 未动：`data/queue.json.bak_cleanup / .bak_opt / .bak_topic`（3 个队列快照，是稿件数据不是代码），
  以及 `.env`、`.env.bak_20261120`、`data/`。
- 清理后核对：`systemctl is-active` = active、`/?pwd=` 200、`queue.json` mtime 仍是 11:08:56
  （早于本次操作，未被改动）、`config.AI_PROVIDER=zhipu` / fallback=`deepseek` 未变。
- 新约定（以后部署照此办）：
  - 备份统一放 `.backups/`，不再散落在项目根目录；
  - 命名 `<YYYYMMDD_HHMM>_<说明>`，例：`.backups/20261120_1125_pre-deepseek`；
  - 只保留最近 3 份，第 4 份产生时删最旧的；
  - 备份只是"刚部署完发现坏了"的几分钟保险，隔天回滚一律走 git（本地仓库 + GitHub 有全量历史）。

> 回滚示例：`cd /opt/auto-content && cp -p .backups/20261120_1125_pre-deepseek/config.py . && cp -p .backups/20261120_1125_pre-deepseek/pipeline.py . && systemctl restart auto-content`
>
> 只留 3 份：`cd /opt/auto-content/.backups && ls -1t | tail -n +4 | xargs -r rm -rf`

## 二十五、公众号草稿箱接入（2026-11-20，main）
- 用户问"怎么接入微信公众号"。核查后发现**此前文档里有一条结论是错的**，本次更正：
  - 旧说法："未认证订阅号没有草稿箱接口权限，会返回 48001"。
  - 实际：2025-07 起微信回收的是**发布/群发**接口（`freepublish/*`、`message/mass/*`，个人主体与
    企业未认证主体一律 48001）；**草稿箱 `draft/add`、素材 `material/*` 对未认证个人订阅号仍开放**，
    实测额度 1000 次/日。多份 2026-10 前后的一手实测记录口径一致。
  - 含义：个人号不必等认证（个人主体自 2021-01 起就没有认证入口，"先认证再接"这条路对个人号根本不存在）。
- 另一个变化：**2025-12-01 起 AppSecret 与 IP 白名单从 mp.weixin.qq.com 迁到微信开发者平台**。
  旧路径「设置与开发 → 基本配置」已找不到密钥，正确路径：
  `developers.weixin.qq.com/platform` → 管理员扫码 → 我的业务 → 选公众号 → 基础信息 / 开发密钥。
- 本次代码改动（publish.py）：
  - `check()` 拆成两项探测：素材接口（`material/get_materialcount`）与**草稿箱接口（`draft/count`）**。
    token、素材、草稿是三个独立权限，只验 token 通过不代表能建草稿——这是最容易误判的一处。
  - 标题上限由 `[:64]` 收紧到 `[:32]`（现行官方文档为 32 字，旧文档写 64；按严的来，防 45003）。
    现有标题规则 12-20 字，正常触不到。
  - 48001 的提示语更正：不再说"没认证所以没权限"，改为"草稿箱未认证可用；若出现在发布接口属正常"。
  - 文件头注释重写，写清"能做什么/不能做什么"以及合规边界。
- `.env.example` 同步更正入口路径与出口 IP。
- 服务器出口 IP 实测：`curl ifconfig.me` = **82.157.4.72**，内网 10.2.0.15，**无 IPv6**。
  白名单只填 IPv4 一条即可，避开"双栈只加一个、时好时坏"的坑。
- 待用户操作（三步，约 10 分钟）：
  1. 开发者平台取 AppID + 重置生成 AppSecret（只显示一次）；
  2. 同一页面「IP 白名单」加 82.157.4.72（保存后约 10 分钟生效）；
  3. 填 `/opt/auto-content/.env` 的 `WECHAT_APPID` / `WECHAT_SECRET`，`systemctl restart auto-content`，
     跑 `.venv/bin/python publish.py --check` 看三项是否全绿。
- 合规边界（2026-04 运营规范）：明确禁止用脚本/AI 全自动替代真人发布，处罚阶梯限流→删文→能力限制→封禁。
  本项目只做到"自动建草稿 + 人工审核 + 人工点发表"，正好在红线内。**不要**加 freepublish 自动发布。

## 二十六、公众号草稿箱接通 + 小标题残留 + 重写事故（2026-11-20，main）
### 1) 接入结果：一次通
- 填入 AppID/AppSecret（`.env`，权限 600）后 `publish.py --check` **四项全绿**：
  token 长度 137、素材接口正常（现有图片 25）、草稿箱接口正常。
  - 说明 IP 白名单此前已包含 82.157.4.72，或该号未开启校验。
  - **再次确认未认证个人订阅号可直接调草稿箱**，不用等认证（个人号本来也认证不了）。
- 真实推送验证：AI 生成头图（CogView，238KB/11.8s）→ 上传永久素材 → `draft/add` 成功；
  回查 `draft/batchget`：标题、作者「Plan B 的AI笔记」、摘要在库，正文 3129 字符、
  18 处内联样式、5 处加粗，`##` 残留 0，HTML 已被转成 19px 加粗 + 左蓝竖线的 `p`（小标题有层级）。
- 审核页「推到草稿箱」按钮随凭据生效自动出现（此前 `publish_ready=False` 一直不渲染）。
- 踩坑记录：`draft/batchget` 返回的中文用 `requests` 的 `r.json()` 读会出现乱码，
  必须 `json.loads(r.content.decode("utf-8"))`。草稿里存的中文其实是对的，是读取端误判编码。

### 2) 小标题残留 `##`（用户上次指出的 bug）
- 根因：正则 `^(#{2,4})\s+(.+)$` 遇到模型输出 `## ## 标题` 时只吃掉外层标记，
  里层 `## ` 留在文字里，公众号里显示成「## 这种能力背后的核心逻辑」。
- 修复 `clean_heading`：行首 `#` 反复剥净、行尾闭合 `#` 一并去掉，加粗同理。
  六个用例全部通过（`## ## X` / `## **X**` / `### ## X` / `## X ##` / 正常 / 三层叠加）。
- `structure_issues` 新增「小标题残留 Markdown 标记」，以后自动拦住并触发重写。
- 队列清洗：`_fixh2.py`（临时脚本，已删）预览后写回，**仅 1 篇 3 个标题受影响**，已修。

### 3) 重写事故与真根因（重要，别只看结论）
- 第一次对上述稿件点重写，产出「OpenAI证实梁文锋的"预言"」且**一个小标题都没有**，
  覆盖后把一篇结构合格的稿子写废，已从 `data/queue.json.bak_20261120_h2fix` 回滚。
- 过程中我先误判为"队列数据串了"（`topic` ≠ `title`），**这是错的**：
  `topic` 存的是选题原标题（百度热搜「OpenAI印证了梁文锋的"预言"」），`title` 是 AI 改写的标题，
  摘要与链接同属一条新闻，数据是干净的。别照这个结论去查数据。
- 真根因：**`optimize_article` 只要求 AI 返回就写回，完全不做结构校验**。
  初稿路径至少还有一次 `structure_issues` 重试，重写路径一次都没有。
- 修复：`optimize_article` 改为最多试 2 次，每次拿 `structure_issues` 的结果判硬伤
  （`is_length_issue` 之外的），仍有硬伤就 `return None` ——
  **不拿坏稿覆盖好稿**，原稿与 `status`/`approved_at` 全部保持原样。
- 同时 `build_optimize_prompt` 新增 `issues` 参数：把旧稿的问题清单回灌给模型，
  并明确要求"每个小节的加粗首句必须是这一节自己的结论"。不提的话它只在旧稿上换措辞。

### 4) 同一句被当成多节结论
- 现象：该稿把「通用模型直接驱动机器人正在从实验走向落地。」写了 4 次
  （第二段 1 次 + 三个小节的加粗首句各 1 次），等于三节没有各自的结论。
- 新增 `dedupe_repeated()`：整段只有这一句且别处也出现 → 删段；该句加粗 ≥3 次 → 只强调首次。
  接在 `parse_response` 的后处理链里（`cap_bold` 之内）。实测 4 次 → 3 次、加粗 3 → 1。
- 既有的「有句子被重复复述」检查强化为带次数与原句：`有句子被重复复述 4 次（…）`，
  只说"有重复"模型往往原样再写一遍。
- 重写后实测：DeepSeek 兜底出稿 14 字标题、1326 字、3 个陈述句小标题、3 处加粗、无重复句，自检通过。

### 5) 草稿箱现状
- 旧版草稿已 `draft/delete` 清掉，重推新版《OpenAI把机器人团队并了》，草稿箱现有 **1 条**。
- 这属于测试推送，可在后台直接删除；确认排版无误后再正式发文。
- 成本：本次 DeepSeek 兜底 1 次重写（4.7 早高峰 429），约 ¥0.05。

## 二十七、审核后台端口改为可配置（2026-11-20，main）

### 1) 起因：5000 端口被占，服务起不来
- 新机上 `auto-content.service` 反复 `activating (auto-restart)`，`code=exited, status=1`；
  而 `curl 127.0.0.1:5000` 返回 **302**——`app.py` 鉴权失败只会给 401 + Basic 头，绝不给 302，
  说明应答的是另一个进程。
- `ss -lntup` 定位：5000 被 **gunicorn**（另一台 Flask 应用，同机还有 mariadb、nginx 443、containerd）占着，
  我方 `app.py` 绑不上端口即退出。

### 2) 改法：端口走环境变量，不写死
- `config.py`：`APP_HOST = os.getenv("APP_HOST", "0.0.0.0")`、`APP_PORT = int(os.getenv("APP_PORT") or 5000)`。
  默认仍是 5000，老部署不受影响；同机端口冲突时在 `.env` 加 `APP_PORT=5001` 即可，代码与 git 都不脏。
- `.env.example` 同步加 `APP_PORT=5000` 与「改端口要同步 nginx + REVIEW_URL」的说明。
- 实测：不设 → 5000；`APP_PORT=5001` → 5001；设为空 → 回落 5000。

### 3) 与既有业务隔离（重要）
- 目标机 80/443 已被现有站点占用，`deploy/nginx.conf` 的 `server_name _; listen 80;` **不能直接套用**，
  否则会抢默认站点。正确做法：独立 `listen 8080` 的 server 块 + `proxy_pass http://127.0.0.1:5001`。
- 验收：`5001 → 401`（鉴权生效）；`8080 → 401`（反代通）；`publish.py --check` 四项全绿（token 取到即说明出口 IP 已在白名单）。

## 二十八、头图与文章无关：改成「画面脚本 + 短 prompt」（2026-11-20，main）

### 1) 根因
- 旧 `build_cover_prompt()` 是 5 条「标题关键词 → 固定意象」映射，其中"AI"那条
  几乎命中一切科技稿 → 所有文章共用同一张紫蓝发光粒子图。
- 实测：讲「做AI副业起号，内容越杂变现越难」的稿子，配的是星云光点，与内容零关联。

### 2) 改法：两层 + 一道闸门
- 第一层 `_visual_brief()`：文本模型（复用 `pipeline.call_ai`，带 4.7→DeepSeek 兜底）
  按标题+摘要+开头写一句 40-70 字的具象画面脚本。`.env` 的 `COVER_BRIEF=0` 可关。
- 第二层 `_rule_scene()`：11 条关键词规则兜底，每条只写**一个主体 + 环境**。
- 闸门 `_brief_ok()`：脚本里出现「散落/堆积/拼贴/碎片/纯黑背景/粒子/象征」等词直接判不合格，
  退回规则。实测"黑色背景上散落着十几种风格迥异的图片"会画出黑色碎石堆，必须拦。

### 3) 另一个实测结论：prompt 越长画得越糊
- cogview-3-flash 是轻量免费模型。把"构图/曝光/留白/色调/禁止清单"堆到 250 字后，
  桌面物件糊成一团；缩到 ~90 字（一句场景 + 一句风格 + 一句禁止）后主体清晰。
- 故 `STYLE_ANCHOR` 压成"真实摄影感，浅景深，自然光，画面干净，右侧留白"。

### 4) 验证
- 规则路径出图：窗边书桌 + 打开的笔记本 + 咖啡 + 右侧留白，无文字乱码，样张见
  `output/样例_头图_新prompt_书桌咖啡.jpg`。
- 闸门单测：两条坏脚本被拦（散落/碎片），两条好脚本通过。
- 新旧对比：旧图紫蓝星云（与副业主题无关）→ 新图书桌工作场景（主题对得上）。

### 5) 调试与回填
- `.venv/bin/python cover.py --prompt <稿件id>`：只打印脚本与 prompt，不出图、不花钱。
- `.venv/bin/python cover.py --backfill`：给通过但没头图的稿件补图。
- 已有头图不会自动替换，需在审核页逐篇点「换一张」。

### 6) 换一张看不到变化（缓存坑，2026-11-20）
- 现象：点「换一张」后过了 11 秒刷新，页面还是原来那张紫图。
- 原因两条叠加：`/cover/<id>` 这个 URL 永远不变，浏览器直接命中缓存；
  且「已生成 + 重新生成中」这一态原先只显示"已生成"，用户看不出正在重画。
- 修法：
  1. 模板给图片和下载链接加版本号 `?v={{ a.cover_at }}`（换一张后 cover_at 变化，URL 随之变）；
  2. `cover_file()` 返回 `Cache-Control: no-store, max-age=0`，不让浏览器留存；
  3. `.cover-head` 生成中优先于已生成；自动刷新脚本同时认「生成中…」文案，
     使"已生成但正在换图"的页面也会在 10 秒后自动刷新。
- 验证：模板两分支分别渲染出「已生成」/「生成中…」，img 与 dl 都带 `?v=`；
  `GET /cover/<id>` 返回 200 且 `Cache-Control: no-store, max-age=0`。
- 注意：「换一张」本身一直走的就是 `generate_cover(force=True)` → 新 prompt 逻辑，未绕过。

### 7) 删掉「仅保存」按钮（2026-11-20）
- 该按钮只调 `POST /update`（不带 status），前端无回显、改完看不出生效，实用价值低。
- 已删除模板按钮与配套的 `saveOnly()` JS；后端 `/update` 保留（「保存并通过」「退回待审」共用）。
- 副作用：已通过稿件手改标题/正文后**没有再落库的入口**，只能走「✦ AI优化 · 重写本篇」覆盖。
  若以后需要"改了但保持已通过"，应新增一个仅 approved 分支可见的「保存修改」按钮。

## 二十九、公众号排版增强（2026-11-20，main）

### 1) 改前只有四种样式
小标题（蓝色左竖线）、正文段落、圆点列表、深色命令块。实际稿件里出现过的标签只有
`p/h2/ul/li`（10 篇统计：p 170、h2 34、ul 38、li 89），never `ol/blockquote/hr/img`。

### 2) 新增六处（全部内联样式，符合公众号只保留 style 的限制）
| 元素 | 处理 |
|---|---|
| 首段 | 变导读卡片：浅底 `#f6f8fb` + 圆角 + 深字色，与正文区分 |
| 小标题 | 自动加 `01 02` 蓝色序号（14px）。标题自带「1.」「一、」「（2）」的跳过，不叠加 |
| 无序列表 | 圆点改主色 `#1d4ed8` 加粗，`padding-left:24px;text-indent:-24px` 做悬挂缩进 |
| 有序列表 | 补真实 `1. 2. 3.`（原来和 ul 一样都出「•」，是个 bug） |
| 引用块 | 升级成主色左边框卡片；内部段落用 `QUOTE_P_STYLE`，不再叠 17px/20px 段距 |
| 分隔线 / 图片 | `<hr>` 出细线；`<img>` 不再被当外壳丢掉（**原来正文配图会整张消失**） |

### 3) 实现要点
- `_Converter` 增加 `lists`（区分 ul/ol 并各自计数）、`quote_depth`、`seen_lead`
  （只有栈为空时的第一个 `<p>` 算导读，引用块内的段落不会抢这个位置）。
- `_add_heading_numbers()` 在转换后按 `H_STYLE` 切开逐段补号；`_NUM_PREFIX_RE`
  识别 `1. / 01 / 一、 / （2）/ 第二步` 五种已有序号写法。
- 强调查阅：`<strong>` 显式写 `color:#0f172a`，避免继承段落灰；`<a>` 加浅蓝下划线。

### 4) 验证
- 真实稿件（父母不会用智能手机）：`validate_html` errors 0 / warns 0。
- 合成用例覆盖 ol / blockquote / hr / img / code / a / 已带序号的标题，全部正常。
- 预览路由 `/preview/<id>` 与复制接口 `/plain/<id>` 的 `html` 字段均含新样式。
- 对比样张：本地 `output/排版预览.html`（未入库，output/ 已忽略）。
