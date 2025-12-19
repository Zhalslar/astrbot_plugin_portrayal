from astrbot.core.message.components import At
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


async def get_nickname(event: AiocqhttpMessageEvent, user_id) -> str:
    """获取指定群友的群昵称或Q名"""
    client = event.bot
    group_id = event.get_group_id()
    if group_id:
        member_info = await client.get_group_member_info(
            group_id=int(group_id), user_id=int(user_id)
        )
        return member_info.get("card") or member_info.get("nickname")
    else:
        stranger_info = await client.get_stranger_info(user_id=int(user_id))
        return stranger_info.get("nickname")

async def get_at_id(self, event: AiocqhttpMessageEvent) -> str | None:
    return next(
        (
            str(seg.qq)
            for seg in event.get_messages()
            if (isinstance(seg, At)) and str(seg.qq) != event.get_self_id()
        ),
        None,
    )
