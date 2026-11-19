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

# 改配置后：.env 改完无需重启（每次请求实时读）；
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
