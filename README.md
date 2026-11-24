# 舞萌 QQ Bot

一个面向本机长期运行的可扩展 bot 骨架：

- 通过腾讯维护的 `qqbot-agent-sdk` 接入 QQ 官方开放平台，不登录个人 QQ 客户端；
- 通过中科大大模型公共服务平台的 OpenAI 兼容 API 提供多轮对话；
- 通过 Diving-Fish（水鱼）OAuth 读取用户主动授权的舞萌 DX 成绩；
- 用 systemd 用户服务实现开机启动、异常重启和日志管理。

当前提供曲目检索、随机谱面、B50/AP50、个人成绩筛选、等级与牌子进度、Rating/分数线工具、猜歌、外观设置和大模型对话。机器人启动时会通过 QQ 官方接口创建或更新全局群指令面板；群内 `@机器人` 后可点选十个常用入口，其余命令仍可手动输入并完整列在 `/help` 中。只发送空白 `@` 时也会回复文字版命令列表，兼容尚未展示面板的旧客户端。直接对机器人发消息，或在群里 `@机器人` 后输入普通文字，会进入对话功能。

## 可选的落雪舞萌账号

玩家发送 `绑定账号` 可查看当前数据源及可用的绑定方式。默认使用水鱼，`/bind` 直接进入水鱼授权。落雪与水鱼分别绑定，各自维护查分授权、切换状态及底分趋势。柚子聊天时仍读取已授权的水鱼成绩，B50 的外观使用 QQ 头像与玩家自己的收藏品设置。

启用落雪时，管理员需在落雪开发者面板创建 OAuth 应用，申请 `read_player` 权限，并登记一个玩家浏览器能访问的 HTTPS 回调地址。在服务环境中设置 `LXNS_CLIENT_ID` 和完全相同的 `LXNS_REDIRECT_URI`（例如 `https://bot.example.com/authorize/lxns`）。默认使用 PKCE，机密客户端还可设置 `LXNS_CLIENT_SECRET`。将该路径精确反向代理到 `http://127.0.0.1:8632` 的同一路径，保留查询参数，关闭回调路径的访问日志以免记录一次性授权码。本地监听端口可由 `LXNS_CALLBACK_PORT` 指定。开发时允许注册本机 HTTP 回调。服务本地将授权保存在私有的 `lxns.sqlite3`，与现有持久数据同目录。未配置应用时，水鱼仍可单独使用。[落雪 OAuth 接入指南](https://maimai.lxns.net/docs/oauth-guide)

1. 私聊机器人发送 `/lxns`，在落雪官网同意只读授权，再发送 `/source 落雪` 以该身份查分。
2. 在某个群内启用时，本人先在该群发送 `/lxns`，将机器人生成的关联编号按提示发给机器人私聊，再回原群确认自己的游戏昵称。每个群的关联独立，编号有效期 10 分钟。
3. `/舞萌数据源 水鱼` 切回水鱼。`/解绑落雪` 在群里解除该群身份的关联，在私聊中解除该落雪授权及其全部群关联。已选择落雪的会话在解除绑定、授权过期或私聊换绑后仍保留该选择。此时查询会提示重新授权或手动切回水鱼。撤销官网授权可在落雪的已授权应用页面操作。

## 藤泽柚子对话与记忆

对话人设固定为《音击》的藤泽柚子：以官方公开的“我行我素、天真烂漫、喜欢糖果”的性格为基准，用自然中文交流，并能讨论舞萌 DX、音击、CHUNITHM、SDVX、Arcaea、Phigros 等音游。代码内置的人设优先级高于旧 `.env` 中的附加提示，因此不需要修改现有密钥或环境文件。

聊天会把最近最多 60 轮对话持久化到仅本机可读的 `data/chat_memory.json`，服务重启后仍可恢复。记忆按会话隔离，避免把私聊内容带到群聊或另一个群；模型会收到近期用户消息及其时间，可自然联系前情。`/memory` 显示当前记忆数量，`/memory clear` 或 `/reset` 会清除当前会话的长期记忆。

聊天时会自动读取该 QQ 用户已经授权给机器人的水鱼数据，并缓存五分钟：包括完整 B50 明细、B35/B15 合计和底分、全量成绩数量、FC/AP 统计，以及与当前问题中曲名相匹配的个人成绩。因此可以直接询问“我的 B50 短板是什么”“我这首歌还差多少”“推荐我练什么”，无需先手动发送 `/b50`。未绑定用户不会获得他人的成绩；水鱼不可用时，模型会明确知道本次没有可靠数据。

聊天支持按需联网检索。明确说“联网搜索/上网查一下”，或询问最新消息、新闻、天气、汇率等时效内容时，机器人会先搜索网页，再把结果作为不可信参考资料交给模型，并在回复末尾附上实际来源链接；普通聊天不会产生额外搜索请求。也可以用 `/web 要查询的问题` 强制联网。检索使用 Brave 公共搜索页，失败时自动回退到 Bing RSS，结果缓存十分钟；两个入口均失败时模型会被明确告知本次无法核实，而不会假装已经联网。此功能不需要新增密钥，也不会修改 `.env`。

`/b50` 默认生成 1600×1900 PNG：五列 B35 + 五列 B15，歌曲封面使用统一圆角和舒适留白。每张卡片固定显示达成率等级、Full Combo、Full Sync 三个素材槽，分别读取水鱼的 `rate`、`fc`、`fs`；SD/DX 类型也使用日服原版样式素材。成绩严格按 `ra` 降序、谱面定数 `ds` 降序、完成率降序、歌曲 ID 升序排列。头部使用日服 DX Rating 面板与数字素材，右侧分别显示 B35、B15 的 Rating 合计。`PC`（游玩次数）当前按需求关闭，不在图片中显示。

水鱼成绩接口不提供当前装备的头像和姓名框 ID，因此收藏品与水鱼绑定完全解耦。头像默认使用 QQ 头像；姓名框优先按水鱼返回的牌子文字匹配原游戏素材，水鱼没有牌子时使用游戏默认姓名框。用户可用 `/cosmetic` 从 LXNS 公开的完整舞萌头像、姓名框目录中搜索并选择，选择保存在 `data/cosmetics.json`，不会改变水鱼成绩来源或授权状态。

```text
/cosmetic
/cosmetic 搜索 头像 初音
/cosmetic 搜索 姓名框 橙将
/cosmetic 头像 101
/cosmetic 姓名框 6113
/cosmetic 头像 QQ
/cosmetic 姓名框 自动
```

成绩徽章来自 MIT 许可的 [`@mai-kit/assets`](https://www.npmjs.com/package/@mai-kit/assets)，许可证副本保存在 `assets/badges/LICENSE.mai-kit`。SD/DX 类型素材来自 MIT 许可的 [`maidraw`](https://github.com/saltcute/maidraw)，许可证副本保存在 `assets/types/LICENSE.maidraw`。

不需要绑定即可使用的曲目工具：

```text
/song 曲名或歌曲ID
/识图                  # 然后发送一张成绩图；也可把指令与图片一起发送
/random 14+ dx master
/random 当前版本 expert
/constant 14.7
/constant 14.7 14.9
/ra 14.7 100.5
/daily
```

`/song` 同时支持正式曲名、歌曲 ID、艺术家和 LXNS 公开别名。`/random` 可组合 SD/DX、绿黄红紫白（或 BASIC/ADVANCED/EXPERT/MASTER/Re:MASTER）、等级/定数、`当前版本` 和曲名/分类关键词。曲目、谱面、别名和定数来自 LXNS 的公开目录，缓存六小时；这只是静态曲库来源，不会创建 LXNS 绑定，也不会改变水鱼成绩来源。

`/song`（或“查歌”）只有一个候选时会先返回谱面详情图，再单独发送该曲所有紫谱与白谱（区分 SD/DX）的 B 站具体 BV 直链；多个候选仍先返回文字 ID 列表，避免误选。直链来自随项目部署的审核元数据库，按曲名、SD/DX 和 MASTER/Re:MASTER 精确匹配，用户查询时不会调用 B 站搜索接口，因此不会因反复搜索触发 HTTP 412 风控。没有可靠匹配时会明确标注“本地审核索引暂未收录”，不会用泛搜索结果冒充。QQ 客户端若能自动解析直链会显示视频预览，否则保留普通可点击链接。封面或图片生成暂时失败时自动回退到文字详情并继续附上谱面确认链接。

离线直链索引来源于 MIT 许可的 [`mai-gen-videob50`](https://github.com/Nick-bit233/mai-gen-videob50) 审核视频元数据，许可证保存在 `data/LICENSE.mai-gen-videob50`。需要更新索引时由维护者手动执行：

```bash
.venv/bin/python scripts/update_bilibili_video_index.py
```

更新脚本只从 GitHub 下载元数据、校验 BVID 与规范链接并原子替换本地文件，不访问 B 站搜索接口；日常 `/song` 查询完全离线读取该索引。

成绩识图：发送 `/识图`（别名 `/recognize`、`成绩识图`），3 分钟内发送同一会话中的下一张成绩图；支持指令和图片在同一条消息中发送。等待状态按群和发送者隔离，其他人的图片不会消耗你的请求，可用 `/识图 取消` 取消。群未开放接收全部消息时，图片消息仍需 @机器人。每次仅识别第一张图片，建议发送单曲成绩原图；最多 12 MB、2000 万像素。识别成功复用 `/song` 的歌曲详情图、谱面确认链接与预览按钮，存在歧义则返回候选 ID。

识图使用本机 RapidOCR（CPU、随 Python 包附带模型）读取曲名，并用 OpenCV SIFT 特征及几何校验匹配公开曲绘，不依赖大模型 API。首次启动后台以 4 个并发建立曲绘库，未完成时文字识别可先使用；新曲素材缺失会在后续更新时重试。只缓存公开曲绘，用户图片仅在内存中处理。云端曲绘缓存写入 `/var/lib/yuzu-bot/song_recognition`，本地为 `data/song_recognition`，均不提交到 GitHub。Linux 部署需要 OpenCV 系统库（Debian/Ubuntu：`libgl1 libglib2.0-0`），云端现有环境已具备。`/help 曲目` 和菜单图会自动显示新指令。

`/daily`（也可发送“每日推歌”）会读取已绑定用户的水鱼成绩，以当前 B50 单谱平均 Rating 反推能力定数，再向下偏移约 0.25，生成当天固定的 10 首个性化练习谱面。困难谱以 100.0000%（SSS）为主要目标，较容易谱面以 100.5000%（SSS+）为目标；困难谱已经接近 SSS+ 且仍有收益时也可继续推荐 SSS+。推荐排序同时考虑官定、水鱼 `/chart_stats` 的拟合定数、当前达成率以及进入 B35/B15 后的真实净增量；同官定下优先选择拟合定数较低的谱面，高定数但接近吃分的谱面也可能入选。图片显示动态目标、官定/拟合定数、目标单谱 RA，以及替换当前 B35/B15 底分或提升榜内成绩后的预计总 Rating 增量。

绑定水鱼后还可使用 `/analyze` 查看 B35/B15 底分、平均达成率、SSS+/FC/AP 数量；`/threshold 14.7` 可分别计算该定数稳定进入当前 B35/B15 所需的最低达成率。

常用的兼容指令（开头的 `/` 可省略）：

```text
查歌 潘                  info 潘
紫潘 / 紫id 834          潘有什么别名
11451是什么歌            定数 14.0 14.4
随个 DX 紫 14            随个 橙代 13+
b50 / ap50               minfo 潘
成绩 14+ 紫 AP           14完成表 / 14ap / 14fc / 14sss+
14分表 / 13+分表          14.7分表
橙将 / 橙将进度          牌子条件
ra 14.7 100.5            分数线 紫潘 100.5
猜歌 / 曲绘猜歌          谱面猜歌 / note猜歌 / 开字母
```

`14分表` 会从该用户的水鱼全量成绩中选出 Lv.14 谱面，按达成率降序、定数降序、歌曲 ID 升序排列并生成最多 100 张卡片的 B50 风格长图。`13+分表` 同理；带小数的 `14.7分表` 表示查询精确定数 14.7，而不是等级文本。分表沿用用户当前的 QQ/自选头像、姓名框、背景、圆角歌曲卡片和成绩徽章。

`橙将进度`、`镜将进度`、`彩将进度` 等命令会生成牌子进度图。顶部显示完成百分比，中间按谱面定数降序展示最难的 8 张未完成谱面，底部分别展示玩家姓名框和目标牌子的游戏原图。版本牌素材随 Bot 发布，支持简繁日文写法，例如 `镜将` 与 `鏡將`。未游玩的谱面按 0.0000% 纳入统计。

发送 `/pc50` 可生成个人游玩次数最高的 50 张谱面图。SD、DX 和不同难度分别排名，按游玩次数降序排列，同次数按标准曲目编号、谱面类型和难度固定排序。只有实际游玩过且次数已知的谱面会入榜。B50 的显示方式独立于这张榜。

水鱼和落雪常规成绩缺少逐谱累计次数时，可以发送 `/pc50 导入`，在 3 分钟内把本人的游戏成绩备份以 JSON 文件发到同一会话。也可以直接发送 `/pc50 导入 [{"song_id":417,"type":"SD","level_index":3,"play_count":174}]`。这个例子表示编号 417 的标准紫谱游玩了 174 次。游戏备份的 `userMusicList`、`userMusicDetailList` 以及通用的 `records` 数组均可使用。导入会替换该身份上一次的快照并立即返图；仅保存曲目编号、谱面类型、难度和累计次数。同谱面重复出现取最高值。以后使用 `/pc50` 可直接查看，`/pc50 清除` 可删除导入的数据。

`r50` 需要数据源提供最近游玩时间；落雪可读取已同步的最近成绩。`ap50` 从全量成绩中过滤 AP/AP+。听歌猜曲需要可以合法分发的试听音源；当前公共曲库没有提供，因此其余猜歌模式可用，音频模式只说明限制。

## 校园互动、音游查询与自制表情

| 在 QQ 里发送 | 得到的结果 |
| --- | --- |
| `/表情包`、`/表情包 纱露朵/哭哭 就差一个粉！` | 浏览底图库，或为指定表情配字。百合咲和艾希德使用游戏头像，QQ 商城的《小勺薄盐》《蓝莓布丁》《香芋甜筒》各有 16 张。 |
| `/签到`、`/运势`、`/背包`、`/送礼`、`/好感度` | 每日和柚子签到领一件礼物并增加 1 点好感，连续每满七天领两件。用背包里的礼物提升好感，或抽取当天的幸运曲。每日按北京时间零点结算，群内与私聊分别记录。 |
| `摸头`、`/状态`、`/送礼 1*2` | 每三小时可以摸头一次，获得 1 点好感。状态面板列出伙伴称号、冷却和背包。单发 `/送礼` 查看全部礼物与持有数。 |
| `/合成`、`/合成 1+2`、`/一键合成`、`/合成状态` | 查看配方、用两种材料排队制作，或自动配对所有基础材料。每件耗时 10 至 30 分钟，做好后自动入包。服务重启后保留进度。 |
| `/b40`、`/trend`、`/sjcj` | 以现有成绩折算旧制 B25 与 B15、查看已记录的 Rating 与进榜底分，或从自己的成绩中随机抽取。趋势从第一次查询 B50 或趋势图开始记录，每天保留最新一次。 |
| `/search amazing`、`/id z317`、`/chub30`、`/chun20` | 跨舞萌与中二查歌、查看中二 Best 30 与 New 20。水鱼中二单独通过 `/chubind` 授权或 `/chubind 解绑` 解除。落雪玩家通过 `/lxns` 授权后，发送 `/中二数据源 落雪` 切换。 |
| `/舞萌定数表 日服 14+`、`/tsearch muse`、`/maijacket 834` | 查日服等级表、查询称号或提取歌曲封面。 |
| `/wordle`、`/猜词 简单`、`#hardle` | 开一局五字母猜词、带线索猜词，或查看本群 Hardle 排行。 |
| `/tk idol`、`/秋什么`、`/邦什么`、`/烤什么` | 查询太鼓之达人乐曲，或从中二、BanG Dream!、世界计划曲库推荐一首。 |

成绩同步可发送 `/上传成绩 水鱼` 或 `/上传成绩 落雪`，在查分器自己的页面操作。群聊保留可点击的入口。命名和交互参考[提比使用手册](https://tippy.anemoi.icu/docs/manual.html)。

## 让柚子参与群聊

群主先在 QQ 的群机器人设置中打开「接收所有消息」，再由群主或管理员在该群发送 `/群聊设置 开启`。这会一起开启偶尔接话和群聊记忆。柚子只在收到本群的新消息时接话，默认每小时最多 3 次，两次至少相隔 3 分钟。直接 @ 的对话与命令另行处理。[QQ 官方全量群消息文档](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html)

| 在群里发送 | 作用 |
| --- | --- |
| `/群记忆`、`/我的画像` | 查看大家常聊的话题和共同约定，或看看自己提过的事。 |
| `/我的画像 退出` | 删除本人在这个群的便签和最近发言片段，停止参与群聊采样。用 `恢复` 可重新加入，用 `清除` 可只删除现有记录。 |
| `/发言频次 1`、`/群戳频次 20%` | 群管理员可调整每小时接话上限和附带文字戳戳的概率。发言频次 0 暂停偶尔接话。 |
| `/群记忆 关闭`、`/群记忆 清除` | 管理员可以暂停便签记录，或清掉本群所有人的群聊便签与画像。`/群聊设置 关闭` 同时暂停接话和记录。 |
| `/摸头`、`/状态`、`/合成`、`/合成状态` | 摸摸柚子的头、查看好感与冷却时间、用背包里的材料合成礼物。 |
| `/塔罗牌`、`/塔罗牌3`、`/点歌 Fragrance` | 翻开单张或三张塔罗看解读，也可以点一首歌看曲卡。 |

接话时最多参考本群近 15 分钟的 12 条短消息，由科大模型结合上下文决定是否回应。群便签保存在 Bot 本机，按群和成员隔离，仅使用最近 30 天的记录。公开群印象展示共同约定和话题，个人画像按发言人的群内身份查询。图片、转发、凭据和普通群聊原文不会写进运行日志。主动 @ 对话的历史可用 `/reset` 清除。

部署者可在私聊发送 `/更新日志 身份` 查看自己在此 Bot 的私聊 OpenID，在服务环境的 `BOT_OWNER_OPENIDS` 中登记后即可私聊使用 `/更新日志`。多个 ID 可用空格或英文逗号分隔。

## 架构

```text
QQ 官方 WebSocket / OpenAPI
          │
          ▼
  QQOfficialAdapter        只负责收发 QQ 消息
          │
          ▼
    CommandRouter          与平台无关的命令分发层
       ┌──┴─────────┬──────────────┐
       ▼            ▼              ▼
 ChatService   DivingFishService  WebSearchService
 中科大 LLM     水鱼 OAuth/成绩    按需联网检索
```

这样拆分的好处是：增加新功能不需要改 QQ 连接代码；以后更换查分器或聊天模型，也不需要重写命令层。

## 1. 准备三组凭据

### QQ 官方机器人

1. 登录 [QQ
 开
放平台](https://q.qq.com/)并创建机器人。
2. 在开发设置中取得 `AppID` 和 `AppSecret`。
3. 开发阶段先在沙箱配置中加入自己的 QQ、测试群或频道，并按控制台实际显示开启群聊、单聊等事件权限。
4. 如果控制台要求 IP 白名单，把家中当前公网出口 IP 加入。家庭宽带 IP 变化后需要同步更新；本项目本身无需端口转发，也无需公网回调地址。

平台最终允许哪些会话类型，以该机器人的审核状态和开放平台权限页面为准。SDK 使用 WebSocket 主动连接 QQ 网关，QQ 客户端不必保持在线。

### 中科大对话 API

在中科大大模型公共服务平台创建项目并申请 API Key。项目默认使用官方文档示例中的：

- Base URL：`https://api.llm.ustc.edu.cn/v1`
- 模型：代码会把旧配置名 `deepseek-v4-flash` 映射到当前实测可响应的 `deepseek-flash`

如果控制台给出的模型名或地址不同，以控制台为准。参考[中科大 API 使用文档](https://llm.ustc.edu.cn/guide/api-usage/)。

当前 Key 虽然会在 `/models` 中列出多个模型，但列出模型或一次短请求成功不代表持续可用。旧配置名 `deepseek-v4-flash` 映射到 `deepseek-flash`。聊天使用流式接收，只把最终正文发送到 QQ；连接连续 60 秒无数据或流中途断开时最多重试一次，单次流最多 120 秒，网络重试阶段总预算 150 秒。失败的残缺回复不会写入记忆。权限错误时仍读取 `/models` 选择后备模型。日志中的 `LLM start`、`LLM first-event`、`LLM complete` 分别记录请求、首次事件、完整正文的耗时，不记录正文或密钥。加载现有环境变量后运行 `python scripts/check_chat.py --prompt '你好啊'` 可验证完整人设聊天。

### 水鱼查分 OAuth

1. 用水鱼账号登录[开发者控制台](https://auth.diving-fish.com/console)。
2. 登记新应用，接入方式选择“设备码绑定”，部署形态选择“由你自己部署运行”。
3. 申请只读权限 `prober.records.read`；只读权限通常自动审核。
4. 生成并立即保存 `client_secret`。它只显示一次。

本项目使用水鱼推荐给“自己运行的 QQ 机器人、没有公网回调”的设备码方案。旧 `Developer-Token` 已停止签发，并将在 2026-10-01 停止服务，因此没有采用旧接口。参考[水鱼 OAuth 快速开始](https://maimai.diving-fish.com/manual/docs/developer/oauth-quickstart/)。

## 2. Arch Linux 本地安装

在项目目录执行：

```bash
sudo pacman -S --needed python python-pip
python -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
```

开发时需要测试工具则执行：

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

## 3. 配置并前台试运行

先在项目目录制作临时配置：

```bash
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，至少填 `QQ_APP_ID` 和 `QQ_APP_SECRET`；对话与查分凭据未填时，对应功能会给出“尚未配置”的提示，但 bot 仍能启动。

环境文件不是 Python 自动读取的 `.env` 魔法文件。前台测试时这样加载：

```bash
set -a
source .env
set +a
.venv/bin/maimai-bot
```

在 QQ 中依次测试：

```text
/ping
/help
你好
/bind
/b50
/song 系ぎて
/random 14+ dx master
/analyze
```

`/bind` 会返回水鱼官方链接。用户打开链接、登录并同意后，再发送 `/b50`。bot 不接收水鱼用户名和密码。

发送 `/unbind` 查看说明，再发送 `/unbind 确认`，会立即停用当前 QQ 身份的水鱼舞萌查分。机器人停用旧成绩缓存、会话上下文和底分趋势关联，重启后仍有效。重新绑定要再次在水鱼官网同意。落雪和中二的本地绑定单独管理。要在水鱼服务端撤销授权，请本人登录，核对应用及权限范围后操作。只在官网撤销时，水鱼已签发的短期代查令牌最长约 5 分钟后失效。[水鱼已授权应用](https://auth.diving-fish.com/apps) · [水鱼 OAuth 文档](https://maimai.diving-fish.com/manual/docs/developer/oauth-api-document/)

## 4. 开机自启动

把配置和服务安装到用户目录：

```bash
install -Dm600 .env "$HOME/.config/maimai-bot/env"
mkdir -p "$HOME/.local/share"
ln -sfn "$PWD" "$HOME/.local/share/maimai-bot"
install -Dm644 deploy/maimai-bot.service "$HOME/.config/systemd/user/maimai-bot.service"
systemctl --user daemon-reload
systemctl --user enable --now maimai-bot.service
```

用户服务默认在登录后启动。若希望“机器一开机、还没登录桌面就启动”，还需执行一次：

```bash
sudo loginctl enable-linger "$USER"
```

常用运维命令：

```bash
systemctl --user status maimai-bot
journalctl --user -u maimai-bot -f
systemctl --user restart maimai-bot
systemctl --user disable --now maimai-bot
```

关机时 systemd 会发送 `SIGTERM`，程序会停止 WebSocket 并退出；下次开机由 systemd 重新拉起。代码或配置改动后运行 `systemctl --user restart maimai-bot`。

> service 通过无空格路径 `%h/.local/share/maimai-bot` 的符号链接定位项目，以避免 systemd 对带空格目录解析不一致。如果移动项目，请重新建立该符号链接。

## 5. 添加新功能

命令的扩展接口在 `CommandRouter.command()`。处理函数接收 `CommandRequest`，返回文本或带图片及按钮的 `BotReply`。所有注册命令会自动进入 `/help`；QQ 面板有十项配额，常用入口在 `src/maimai_bot/qq_command_panel.py` 的 `preferred` 中维护。

校园互动、中二和设置的图片使用 `await academy_card(标题, [(字段名, 值)], section=分类)`，可传 `metric` 显示主数值、`followup` 发送可复制文本、`actions` 发送 QQ 按钮。该接口位于 `maimai_bot.services.result_card`。菜单图与主页共用 `help_catalog.py` 的导航，用户可发送 `帮助maimaiDX` 直达舞萌。糖果插画和三星图案由项目自行绘制，参考了藤泽柚子爱吃糖及 ASTERISM 三人组的设定。[藤沢柚子角色介绍](https://ongeki.sega.jp/character/1020/) · [オンゲキ世界观](https://ongeki.sega.jp/introduction/)

在 `src/maimai_bot/features.py` 的 `build_router()` 中加入：

```python
@router.command("dice", "掷一个六面骰子", aliases=("骰子",), category="娱乐")
async def dice_command(request: CommandRequest) -> str:
    import secrets

    return f"{request.context.user_id[:4]} 掷出了 {secrets.randbelow(6) + 1}"
```

然后重启服务即可。功能较大时建议：

1. 把外部 API 封装在 `src/maimai_bot/services/`；
2. 在 handler 中只做参数校验、调用 service、格式化回复；
3. 为 service 使用 `httpx.MockTransport` 写测试，避免测试依赖真实网络；
4. 密钥只新增到环境变量和 `.env.example`，不要写进源码。

## 云服务器自动部署

生产环境使用 `/opt/yuzu-bot/releases/<commit>` 保存相互隔离的发布版本，
`/opt/yuzu-bot/current` 原子切换到最新版本。每次部署都会创建独立虚拟环境、
运行完整测试并检查 systemd 服务；健康检查失败时自动切回上一个版本。

GitHub Actions 只持有一把专用 SSH 私钥。服务器端对应公钥使用
`restrict,command="/usr/local/sbin/deploy-yuzu-bot"` 限制，因此该密钥不能获得交互式
Shell，也不能执行任意远程命令。工作流需要以下 Actions Secrets：

- `DEPLOY_HOST`
- `DEPLOY_PORT`
- `DEPLOY_USER`
- `DEPLOY_SSH_KEY`
- `DEPLOY_KNOWN_HOSTS`

真实环境变量保存在服务器 `/etc/yuzu-bot/env`，聊天记忆和用户收藏品选择保存在
`/var/lib/yuzu-bot/`；这些文件不会进入 GitHub。推送 `main` 后，
`.github/workflows/deploy.yml` 会通过受限 SSH 入口执行服务器上的部署脚本。

如果将来要做图片版 B50，可在 `DivingFishService` 之上增加海报渲染 service，再调用官方 SDK 的图片上传接口；不需要改变 OAuth 和命令路由设计。

生产机现通过已有的校园网路由访问科大 API，使用 `deepseek-v4-flash-ascend`。2026-11-25 用服务账号、生产配置和上面的诊断脚本实测流式对话成功。若再次出现建连超时，请先从运行 Bot 的机器检查校园网连接。`curl --max-time 10 -o /dev/null -sS -w '%{http_code}\n' https://api.llm.ustc.edu.cn/v1/models` 正常应能收到 HTTP 响应，未携带密钥通常返回 401。再用诊断脚本验证有授权的模型确实返回正文。[校外 iWAN 接入说明](https://email.ustc.edu.cn/notice/iwan/)

## 安全与限制

- `.env`、真实密钥和运行数据已被 `.gitignore` 排除。
- LLM 会看到用户发给 bot 的对话文本；群内不要发送个人秘密。
- 对话上下文仅存内存，每个用户最多保留配置的轮数，服务重启即清空。
- 水鱼成绩不是直接从华立/世嘉官方读取，用户需要自行在查分器同步数据。
- QQ 官方机器人能否进入普通群、可回复频率和消息类型受开放平台审核、权限和限流约束。


更新命令或别名后，运行 `.venv/bin/python scripts/sync_help_site.py` 同步本地帮助网页。`.venv/bin/python scripts/sync_help_site.py --check` 可检查网页清单是否与机器人注册的命令一致。网页目录位于 `bot-homepage/dist/`。
