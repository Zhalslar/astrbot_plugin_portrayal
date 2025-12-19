from astrbot.core.message.components import At
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


async def get_nickname_gender(
    event: AiocqhttpMessageEvent, user_id: str | int
) -> tuple[str, str]:
    """获取指定群友的昵称和性别"""
    all_info = await event.bot.get_group_member_info(
        group_id=int(event.get_group_id()), user_id=int(user_id)
    )
    nickname = all_info.get("card") or all_info.get("nickname")
    gender = all_info.get("sex")
    return nickname, gender


def get_at_id(event: AiocqhttpMessageEvent) -> str | None:
    return next(
        (
            str(seg.qq)
            for seg in event.get_messages()
            if (isinstance(seg, At)) and str(seg.qq) != event.get_self_id()
        ),
        None,
    )
