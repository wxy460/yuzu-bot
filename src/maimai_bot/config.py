from __future__ import annotations

import os
from dataclasses import dataclass

FUJISAWA_YUZU_PERSONA = """你在这段对话中扮演《音击（オンゲキ）》的藤泽柚子，与用户自然地长期相处。

角色基准：你是奏坂高中二年级学生、ASTERISM 成员。你我行我素、天真烂漫，带一点孩子气，偶尔会冒出让人意外但可爱的跳脱想法。你非常喜欢零食，尤其喜欢随身带着糖果、兴致勃勃地分享新奇甚至有点古怪的口味。你亲切、乐观，会真诚关心用户的休息、吃饭、心情和当天发生的事情。

说话方式：主要使用自然中文，语气轻快亲近，可以偶尔使用“柚子”“诶嘿嘿”“要不要来颗糖”一类符合性格的表达，但不要机械重复口癖，不要堆砌颜文字，不要把每段话都写成舞台台词。可以适度提到音击、ASTERISM、糖果和练习，但不要编造与官方设定冲突的经历；无法确认的角色设定或音游事实要坦白说明。

能力与相处方式：你熟悉舞萌DX、音击、CHUNITHM、SDVX、Arcaea、Phigros 等音游，可以聊歌曲、谱面、定数、练习方法、手法、设备与游戏文化。系统可能提供用户已授权的水鱼成绩摘要；这些数据对你可见，你应直接结合具体曲名、定数、达成率、单谱 Rating、FC/AP 情况分析，不要让用户重复发送已有数据，也不要虚构摘要中不存在的成绩。数据可能有同步延迟，涉及当前成绩时应提醒这一点。

记忆与关怀：系统会提供用户过去谈过的内容。自然地记住并在合适时联系前情，例如用户早上说过做了什么，之后可以主动问进展或感受；不要生硬复述“记忆记录”，不要声称记得系统没有提供的事情。对敏感内容保持克制，不向其他用户泄露记忆或成绩。

回答原则：先回应用户真正关心的内容，再补充建议；日常聊天通常简短自然，谱面分析可以更具体。你是角色化的 AI 聊天伙伴，不要谎称自己在现实中拥有身体、亲自玩过某局游戏，或能访问系统没有提供的数据。"""

DEFAULT_CHAT_SYSTEM_PROMPT = "请保持回答自然、友善，并优先使用中文。"


class ConfigError(RuntimeError):
    pass


def _positive_int(name: str, default: int) -> int:
    value = os.getenv(name, str(default))
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数") from exc
    if parsed < 1:
        raise ConfigError(f"{name} 必须大于 0")
    return parsed


@dataclass(frozen=True, slots=True)
class Settings:
    qq_app_id: str
    qq_app_secret: str
    llm_api_key: str = ""
    llm_base_url: str = "https://api.llm.ustc.edu.cn/v1"
    llm_model: str = "deepseek-v4-flash-ascend"
    chat_system_prompt: str = DEFAULT_CHAT_SYSTEM_PROMPT
    chat_history_turns: int = 8
    divingfish_client_id: str = ""
    divingfish_client_secret: str = ""
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        app_id = os.getenv("QQ_APP_ID", "").strip()
        app_secret = os.getenv("QQ_APP_SECRET", "").strip()
        missing = [
            name
            for name, value in (("QQ_APP_ID", app_id), ("QQ_APP_SECRET", app_secret))
            if not value
        ]
        if missing:
            raise ConfigError("缺少必填环境变量：" + ", ".join(missing))

        return cls(
            qq_app_id=app_id,
            qq_app_secret=app_secret,
            llm_api_key=os.getenv("USTC_LLM_API_KEY", "").strip(),
            llm_base_url=os.getenv("USTC_LLM_BASE_URL", "https://api.llm.ustc.edu.cn/v1").rstrip(
                "/"
            ),
            llm_model=os.getenv("USTC_LLM_MODEL", "deepseek-v4-flash-ascend").strip(),
            chat_system_prompt=os.getenv("CHAT_SYSTEM_PROMPT", DEFAULT_CHAT_SYSTEM_PROMPT).strip(),
            chat_history_turns=_positive_int("CHAT_HISTORY_TURNS", 8),
            divingfish_client_id=os.getenv("DIVINGFISH_CLIENT_ID", "").strip(),
            divingfish_client_secret=os.getenv("DIVINGFISH_CLIENT_SECRET", "").strip(),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

    @property
    def chat_enabled(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)

    @property
    def scores_enabled(self) -> bool:
        return bool(self.divingfish_client_id and self.divingfish_client_secret)
