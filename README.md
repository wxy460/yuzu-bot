# 舞萌 QQ Bot

一个面向本机长期运行的可扩展 bot 骨架：

- 通过腾讯维护的 `qqbot-agent-sdk` 接入 QQ 官方开放平台，不登录个人 QQ 客户端；
- 通过中科大大模型公共服务平台的 OpenAI 兼容 API 提供多轮对话；
- 通过 Diving-Fish（水鱼）OAuth 读取用户主动授权的舞萌 DX 成绩；
- 用 systemd 用户服务实现开机启动、异常重启和日志管理。

当前提供曲目检索、随机谱面、B50/AP50、个人成绩筛选、等级与牌子进度、Rating/分数线工具、猜歌、外观设置和大模型对话。机器人启动时会通过 QQ 官方接口创建或更新全局群指令面板；群内 `@机器人` 后可点选十个常用入口，其余命令仍可手动输入并完整列在 `/help` 中。只发送空白 `@` 时也会回复文字版命令列表，兼容尚未展示面板的旧客户端。直接对机器人发消息，或在群里 `@机器人` 后输入普通文字，会进入对话功能。

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

`橙将`、`橙将进度` 等牌子进度查询会生成专门的进度图片：顶部是完成百分比，中间严格按谱面定数降序显示前 8 张未完成谱面，底部显示达成条件、总进度、各难度剩余数量、玩家头像和姓名框，并单独加载目标牌子的原游戏姓名框素材。未游玩的公共曲库谱面会按 0.0000% 纳入统计；图片失败时自动回退到文字列表。

`r50` 与 `pc50` 也已保留为稳定入口，但水鱼当前 OAuth records 不返回最近游玩时间和游玩次数，所以机器人会明确提示数据源限制，不会要求用户绑定落雪，也不会用错误字段伪造结果。`ap50` 则直接从水鱼全量成绩中过滤 AP/AP+，可以正常使用。听歌猜曲需要可以合法分发的试听音源；当前公共曲库没有提供，因此其余猜歌模式可用，音频模式只说明限制。

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

命令的扩展接口在 `CommandRouter.command()`。业务功能只接受 `CommandRequest` 并返回字符串，不依赖 QQ SDK。所有注册命令会自动进入 `/help`；QQ 面板有十项配额，常用入口在 `src/maimai_bot/qq_command_panel.py` 的 `preferred` 中维护。

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

如果将来要做图片版 B50，可在 `DivingFishService` 之上增加海报渲染 service，再调用官方 SDK 的图片上传接口；不需要改变 OAuth 和命令路由设计。

## 安全与限制

- `.env`、真实密钥和运行数据已被 `.gitignore` 排除。
- LLM 会看到用户发给 bot 的对话文本；群内不要发送个人秘密。
- 对话上下文仅存内存，每个用户最多保留配置的轮数，服务重启即清空。
- 水鱼成绩不是直接从华立/世嘉官方读取，用户需要自行在查分器同步数据。
- QQ 官方机器人能否进入普通群、可回复频率和消息类型受开放平台审核、权限和限流约束。
