import asyncio
import time
from typing import Any

from astrbot.api import logger, sp
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import At
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.provider.entities import ProviderRequest

from .core.bot_identity import BotIdentityStore, build_avatar_urls, download_avatar_b64
from .core.config import PluginConfig
from .core.db import UserProfileDB
from .core.entry import EntryService
from .core.llm import LLMService
from .core.message import MessageManager
from .core.model import UserProfile
from .core.persona_service import PersonaError, PersonaService
from .plugin_api import register_plugin_page_api

# 「改人格」的三种模式前缀。
# 全角冒号是中文输入法默认，但半角冒号（甚至空格）也很常见——用户写 `重置:xxx`
# 时必须识别成重置模式，否则整段会被当成「LLM 改写要求」。
APPEND_PREFIX = "追加："
RESET_PREFIX = "重置："
APPEND_KEYWORDS = ("追加", "append")
RESET_KEYWORDS = ("重置", "reset")
MODE_SEPARATORS = ("：", ":", " ", "\u3000", "\n", "\t")

# 超出该长度时额外提示「不建议直接群发」
MAX_SAFE_PROMPT_LEN = 2000

# 这些命令由各自的 @filter.command handler 处理，提示词监听器不再重复响应
RESERVED_COMMANDS = frozenset(
    {
        "查看画像",
        "查看克隆",
        "改人格",
        "切换人格",
        "恢复人格",
        "查看机器人身份",
        "还原机器人资料",
        "查头像",
    }
)


class PortrayalPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.cfg = PluginConfig(config, context)
        self.db = UserProfileDB(self.cfg)
        self.msg = MessageManager(self.cfg)
        self.entry_service = EntryService(self.cfg)
        self.llm = LLMService(self.cfg)
        # 机器人自身昵称/头像的全局备份（账号级，修复「切换后昵称头像串了」）
        self.identity = BotIdentityStore(self.cfg.bot_identity_file)
        # 账号级操作（改昵称/头像）串行锁，避免并发切换互相覆盖
        self._identity_lock: asyncio.Lock | None = None
        # 头像下载实现（唯一注入点，便于测试替换）
        self._download_avatar = download_avatar_b64
        # QQ 命令与 WebUI 面板共用的人格服务
        self.persona_service = PersonaService(
            self.cfg, self.db, self.llm, self.msg, self.entry_service
        )
        # 注册 WebUI 面板后端（/api/plug/astrbot_plugin_portrayal/...）
        try:
            self.page_api = register_plugin_page_api(context, self)
        except Exception as e:
            self.page_api = None
            logger.warning(f"注册 WebUI 面板接口失败（不影响聊天命令）：{e}")

    async def initialize(self):
        pass

    async def terminate(self):
        self.msg.save_cache()

    # =========================
    # 解析辅助
    # =========================

    @staticmethod
    def _get_cmd(event: AstrMessageEvent) -> str:
        """取消息中的命令词（默认配置的唤醒前缀已在 message_str 中被剥离）

        使用 split() 而不是 partition(" ")，兼容全角空格与连续空格。
        """
        parts = event.message_str.split()
        if parts:
            return parts[0]
        return event.message_str.strip()

    @staticmethod
    def _split_target_and_text(event: AstrMessageEvent) -> tuple[str, str]:
        """拆分 @群友 与其后的纯文本内容

        Returns:
            (target_id, payload)，没有 @ 时 target_id 为空字符串。

        优先取消息链里的 At 段，并跳过 @机器人 自身（aiocqhttp 会把
        「@bot 改人格 @群友 …」里的第一个 At(self) 留在消息链里）；
        若客户端没有生成 At 段（例如用户直接手打 QQ 号），则退化为把命令词
        之后第一个纯数字 token 当作目标 QQ 号。
        """
        segments = event.get_messages()
        try:
            self_id = str(event.get_self_id())
        except Exception:
            self_id = ""

        ats = [
            str(seg.qq)
            for seg in segments
            if isinstance(seg, At) and str(seg.qq) not in (self_id, "all")
        ]
        target_id = ats[0] if ats else ""
        if target_id:
            texts: list[str] = []
            hit_at = False
            for seg in segments:
                if isinstance(seg, At):
                    # 只在遇到真正的目标 At 之后才开始收集文本
                    hit_at = hit_at or str(seg.qq) == target_id
                    continue
                if not hit_at:
                    continue
                text = getattr(seg, "text", None)
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())

            payload = " ".join(texts).strip()
            # 部分客户端会把 @群友 渲染成 "@123" 混进正文，这里只剔除
            # 「正好等于 QQ 号」或「QQ 号 + 空格」的前缀，避免误伤以该数字
            # 开头的修改要求
            if payload == target_id:
                payload = ""
            elif payload.startswith(f"{target_id} "):
                payload = payload[len(target_id) :].strip()
            return target_id, payload

        # 退化路径：命令词之后的第一个纯数字 token 视为目标 QQ 号
        tokens = event.message_str.split()
        for index, token in enumerate(tokens[1:], start=1):
            if token.isdigit():
                return token, " ".join(tokens[index + 1 :]).strip()
        return "", ""

    @staticmethod
    def _parse_edit_mode(instruction: str) -> tuple[str, str] | None:
        """识别「改人格」的模式

        Returns:
            (mode, payload)，mode 为 "append" / "reset" / "rewrite"；
            payload 是去掉前缀后的正文。
            分隔符接受全角/半角冒号、空格、制表符；「重置abc」这种没有分隔符的
            写法**不**当作重置（避免误伤以「重置」开头的普通改写要求）。

        返回 None 只表示「看起来想用前缀模式但格式不对」，调用方应提示用法，
        以免把整段人格正文当成改写要求喂给 LLM。
        """
        text = (instruction or "").strip()
        if not text:
            return None

        keywords = (
            ("append", APPEND_KEYWORDS),
            ("reset", RESET_KEYWORDS),
        )
        head = text[:6]
        for mode, words in keywords:
            for word in words:
                if not head.lower().startswith(word):
                    continue
                rest = text[len(word) :]
                if not rest:
                    # 只有关键词，没有正文：交给调用方报「内容不能为空」
                    return (mode, "")
                stripped = rest.lstrip("".join(MODE_SEPARATORS))
                if stripped != rest or rest[0] in MODE_SEPARATORS:
                    return (mode, stripped.strip())
                # 关键词后面直接接非分隔符字符：格式可疑，返回 None 让调用方提示
                return None
        return ("rewrite", text)

    # =========================
    # 查看画像
    # =========================

    @filter.command("查看画像")
    async def view_portrayal(self, event: AiocqhttpMessageEvent):
        """
        查看画像 @群友
        """
        ats = [
            str(seg.qq)
            for seg in event.get_messages()[1:]
            if isinstance(seg, At) and str(seg.qq).isdigit()
        ]
        if not ats:
            yield event.plain_result("命令格式：查看画像 @群友")
            return
        target_id = ats[0]
        if self.cfg.message.is_protected_user(target_id):
            yield event.plain_result("该用户在保护名单中，不允许查询")
            return
        profile = self.db.get(target_id)
        if not profile:
            yield event.plain_result("本地暂无该用户画像记录")
            return
        msg = f"【{profile.nickname}】的画像\n{profile.to_text()}"
        yield event.plain_result(msg)

    # =========================
    # 查看克隆人格
    # =========================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查看克隆")
    async def view_clone(self, event: AiocqhttpMessageEvent):
        """
        查看克隆 @群友
        """
        target_id, _ = self._split_target_and_text(event)
        if not target_id or not target_id.isdigit():
            yield event.plain_result("命令格式：查看克隆 @群友")
            return
        if self.cfg.message.is_protected_user(target_id):
            yield event.plain_result("该用户在保护名单中，不允许查询")
            return
        profile = self.db.get(target_id)
        if not profile:
            yield event.plain_result("本地暂无该用户画像记录")
            return
        if not profile.clone_prompt.strip():
            yield event.plain_result(
                f"【{profile.nickname}】暂未生成克隆人格，"
                f"请先执行“克隆人格 @{profile.nickname}”"
            )
            return
        content = profile.clone_prompt.strip()

        # 先说清「AstrBot 里现在到底是哪份内容」，便于判断切换是否生效
        try:
            live = None
            for persona in await self.context.persona_manager.get_all_personas():
                if getattr(persona, "persona_id", None) == profile.persona_id:
                    live = persona
                    break
            if live is None:
                state = "AstrBot 里还没有这份人格，需执行「切换人格 @群友」"
            elif (live.system_prompt or "") == profile.clone_prompt:
                state = "AstrBot 内内容与本地一致 ✅"
            else:
                state = (
                    f"AstrBot 内内容与本地不一致 ⚠️"
                    f"（AstrBot {len(live.system_prompt or '')} 字 / 本地 "
                    f"{len(profile.clone_prompt)} 字，请重新执行「切换人格 @群友」）"
                )
        except Exception as e:
            state = f"（读取 AstrBot 人格失败：{e}）"
        yield event.plain_result(f"人格 ID：{profile.persona_id}\n{state}")

        yield event.plain_result(
            f"【{profile.nickname}】当前的克隆人格（{len(content)} 字）：\n"
            f"{content}"
        )
        if len(content) > MAX_SAFE_PROMPT_LEN:
            yield event.plain_result(
                f"提示：当前人格 {len(content)} 字，超过 {MAX_SAFE_PROMPT_LEN} 字，"
                f"不建议直接群发全文。"
            )

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.cfg.inject_prompt:
            return
        if not event.message_str:
            return
        sender_id = event.get_sender_id()
        profile = self.db.get(sender_id)
        if not profile:
            return
        info = profile.to_text()
        req.system_prompt += f"\n\n### 当前对话用户的背景信息\n{info}\n\n"

    # =========================
    # 画像 / 克隆人格
    # =========================

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def get_portrayal(self, event: AiocqhttpMessageEvent):
        """
        画像 @群友 <查询轮数>
        """
        cmd = self._get_cmd(event)
        # 内置命令有自己的 handler，即使配置里出现同名提示词条目也不要重复处理
        if cmd in RESERVED_COMMANDS:
            return
        prompt = self.entry_service.get_entry(cmd)
        if not prompt:
            # 容错：升级后配置里可能还没回填“重克隆人格”条目，
            # 此时退回复用“克隆人格”的提示词内容
            if cmd == "重克隆人格":
                prompt = self.entry_service.get_entry("克隆人格")
            if not prompt:
                return
        if prompt.need_admin and not event.is_admin():
            return

        ats = [
            str(seg.qq)
            for seg in event.get_messages()[1:]
            if isinstance(seg, At) and str(seg.qq).isdigit()
        ]
        if not ats:
            yield event.plain_result("命令格式：画像 @群友 <查询轮数>")
            return

        # 检查权限
        target_id = ats[0]
        if not target_id.isdigit():
            yield event.plain_result("命令格式：画像 @群友 <查询轮数>")
            return
        if self.cfg.message.is_protected_user(target_id):
            yield event.plain_result("该用户在保护名单中，不允许查询")
            return

        # 解析查询轮数
        end_param = event.message_str.split()[-1]
        query_rounds = self.cfg.message.get_query_rounds(end_param)

        # 获取基本信息（沿用旧档案里已有的画像与克隆人格，避免被空值覆盖）
        try:
            info = await event.bot.get_stranger_info(
                user_id=int(target_id), no_cache=True
            )
            profile = UserProfile.from_qq_data(target_id, data=dict(info))
        except Exception as e:
            logger.error(f"获取用户资料失败：{e}")
            yield event.plain_result(f"获取该用户资料失败：{e}")
            return
        old_profile = self.db.get(target_id)
        if old_profile:
            profile.portrait = old_profile.portrait
            profile.timestamp = old_profile.timestamp
            profile.clone_prompt = old_profile.clone_prompt

        # 只有“克隆”类命令才涉及人格融合，其余命令（画像 / 找对象…）保持原样
        old_clone_prompt = ""
        merge_prompt = ""
        if "克隆" in cmd:
            old_clone_prompt = profile.clone_prompt
            # “重克隆人格”明确要求丢弃旧人格；旧人格为空时自然走全新生成
            if "重克隆" in cmd or not old_clone_prompt.strip():
                old_clone_prompt = ""
            else:
                merge_prompt = self.cfg.get_merge_prompt()

        if old_clone_prompt.strip():
            yield event.plain_result(
                f"检测到【{profile.nickname}】已有克隆人格，将结合新的聊天记录进行融合..."
            )

        yield event.plain_result(
            f"正在发起{query_rounds}轮查询来获取{profile.nickname}的聊天记录..."
        )

        # 获取聊天记录
        result = await self.msg.get_user_texts(
            event,
            profile.user_id,
            max_rounds=query_rounds,
        )
        if result.is_empty:
            yield event.plain_result("没有查询到该群友的任何消息")
            return
        if result.from_cache and result.scanned_messages <= 0:
            yield event.plain_result(
                f"命中缓存，已提取到{result.count}条{profile.nickname}的聊天记录，"
                f"正在{cmd}..."
            )
        else:
            yield event.plain_result(
                f"已从{result.scanned_messages}条群消息中提取到"
                f"{result.count}条{profile.nickname}的聊天记录，正在{cmd}..."
            )

        # LLM 分析画像（存在旧克隆人格时自动走融合）
        try:
            content = await self.llm.generate_portrait(
                result.texts,
                profile,
                prompt.content,
                old_clone_prompt=old_clone_prompt,
                merge_prompt_template=merge_prompt,
                umo=event.unified_msg_origin,
            )
        except Exception as e:
            logger.error(f"LLM 调用失败：{e}")
            yield event.plain_result(f"分析失败：{e}，已保留原有人格")
            return

        # 防御：LLM 返回空内容时不写库，避免把已有的人格覆盖成空
        if not content or not content.strip():
            yield event.plain_result("分析结果为空，已保留原有人格")
            return

        content = content.strip()

        # 保存克隆人格
        if "克隆" in cmd:
            profile.clone_prompt = content

        # 保存画像并发送
        profile.portrait = content
        profile.timestamp = int(time.time())
        self.db.set(profile)
        yield event.plain_result(content)

    # =========================
    # 快捷修改人格
    # =========================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("改人格")
    async def edit_persona(self, event: AiocqhttpMessageEvent):
        """
        改人格 @群友 <修改要求>          —— 由 LLM 按修改要求重写
        改人格 @群友 追加：<补充文字>   —— 直接在末尾追加，不调用 LLM
        改人格 @群友 重置：<完整人格>   —— 整段替换（无档案时自动建档）
        """
        target_id, instruction = self._split_target_and_text(event)
        if not target_id or not target_id.isdigit():
            yield event.plain_result(
                "命令格式：\n"
                "改人格 @群友 <修改要求>（LLM 重写）\n"
                "改人格 @群友 追加：<补充文字>（零成本追加）\n"
                "改人格 @群友 重置：<完整人格>（整段替换）"
            )
            return
        if self.cfg.message.is_protected_user(target_id):
            yield event.plain_result("该用户在保护名单中，不允许修改")
            return

        mode_payload = self._parse_edit_mode(instruction)

        # 看起来想用「重置 / 追加」但格式不对时先提示用法，
        # 避免把整段人格正文当成「改写要求」交给 LLM（白烧 token 还会改坏人格）
        if mode_payload is None:
            yield event.plain_result(
                "没看懂这条修改要求：开头像是「重置 / 追加」模式，但格式不对。\n"
                "正确写法（全角或半角冒号都行）：\n"
                "  改人格 @群友 重置：<完整人格>\n"
                "  改人格 @群友 追加：<补充文字>\n"
                "  改人格 @群友 <修改要求>（交给 LLM 重写）\n"
                "若确实想让 LLM 重写，请把开头的“重置/追加”去掉再发一次。"
            )
            return

        profile = self.db.get(target_id)
        is_new_profile = profile is None

        # 重置模式允许目标用户还没有档案：先拉取陌生人资料，再交给服务层写入
        if is_new_profile and mode_payload[0] == "reset":
            try:
                info = await event.bot.get_stranger_info(
                    user_id=int(target_id), no_cache=True
                )
                profile = UserProfile.from_qq_data(target_id, data=dict(info))
            except Exception as e:
                logger.error(f"获取用户资料失败：{e}")
                yield event.plain_result(f"获取该用户资料失败：{e}")
                return

        mode, payload = mode_payload
        try:
            if mode == "append":
                result = self.persona_service.apply_edit(
                    target_id, "append", payload, profile=profile
                )
            elif mode == "reset":
                # 目标还没有档案时（刚拉过陌生人资料）用 create 建档写入
                result = self.persona_service.apply_edit(
                    target_id,
                    "create" if is_new_profile else "replace",
                    payload,
                    profile=profile,
                )
            else:  # rewrite：把整条指令当作「修改要求」交给 LLM
                result = await self.persona_service.apply_rewrite(
                    target_id,
                    payload,
                    umo=event.unified_msg_origin,
                    profile=profile,
                )
        except PersonaError as e:
            yield event.plain_result(str(e))
            return
        except Exception as e:
            logger.error(f"修改人格失败：{e}", exc_info=True)
            yield event.plain_result(f"修改失败：{e}，已保留原有人格")
            return

        yield event.plain_result(
            f"【{result.nickname}】的克隆人格已{result.mode}（{result.length} 字）。"
            f"\n执行“切换人格 @{result.nickname}”后生效。"
            f"\n发送“查看克隆 @{result.nickname}”可查看当前人格全文。"
        )
        if result.too_long:
            yield event.plain_result(
                f"提示：当前人格 {result.length} 字，超过 {MAX_SAFE_PROMPT_LEN} 字，"
                f"不建议直接群发全文。"
            )

    # =========================
    # 机器人身份（昵称 / 头像）同步
    # =========================

    async def _read_login_info(self, event: AiocqhttpMessageEvent) -> dict:
        """读取协议端的机器人资料（失败返回空 dict）"""
        try:
            info = await event.bot.get_login_info()
            return dict(info) if isinstance(info, dict) else {}
        except Exception as e:
            logger.warning(f"获取机器人资料失败：{e}")
            return {}

    async def _sync_qq_nickname(self, event: AiocqhttpMessageEvent, nickname: str) -> str:
        """设置机器人昵称，返回错误描述（成功返回空串）"""
        nickname = (nickname or "").strip()
        if not nickname:
            return "昵称为空，已跳过"
        try:
            raw = await event.bot.set_qq_profile(nickname=nickname)
            logger.info(f"set_qq_profile({nickname!r}) -> {raw!r}")
        except Exception as e:
            logger.error(f"设置机器人昵称失败：{e}")
            return f"设置昵称失败：{e}"

        # 协议端把失败放在返回体里（status != ok / retcode != 0）时也要报出来
        hint = ""
        if isinstance(raw, dict):
            status = str(raw.get("status") or "").lower()
            retcode = raw.get("retcode")
            message = str(raw.get("message") or raw.get("wording") or "")
            if (status and status != "ok") or (retcode not in (None, 0)):
                hint = f"（协议端 status={status or '-'} retcode={retcode} {message}）"

        # 回读只作参考，**不据此判定失败**：部分协议实现（如 NapCat）的
        # get_login_info 会返回缓存里的旧昵称，会出现「改名其实成功了但回读还是旧值」
        # 的假阴性；据此判失败会连带跳过头像同步，导致「昵称变了头像不变」。
        actual = ""
        for _ in range(3):
            info = await self._read_login_info(event)
            actual = str(info.get("nickname") or "").strip()
            if not actual or actual == nickname:
                break
            await asyncio.sleep(0.5)

        if actual and actual != nickname:
            logger.info(
                f"昵称回读为 {actual!r}（期望 {nickname!r}）：可能是协议端缓存，"
                f"已按设置成功处理{hint}"
            )
            return ""
        return f"昵称已设置但协议端提示异常{hint}" if hint else ""

    async def _sync_qq_avatar(self, event: AiocqhttpMessageEvent, avatar: str) -> str:
        """设置机器人头像（avatar 可以是已确认的 base64 或图片直链）

        只有拿到**确认过的机器人原图字节**时才会走到这里，因此这里不做任何
        「用 QQ 号去猜」的动作。
        """
        avatar = (avatar or "").strip()
        if not avatar:
            return "头像为空，已跳过"

        if avatar.startswith("base64://"):
            avatar = avatar[len("base64://") :]
        if avatar.startswith("http://") or avatar.startswith("https://"):
            try:
                await event.bot.set_qq_avatar(file=avatar)
            except Exception as e:
                logger.error(f"设置机器人头像失败：{e}")
                return f"设置头像失败：{e}"
            return ""

        try:
            await event.bot.set_qq_avatar(file=f"base64://{avatar}")
        except Exception as e:
            logger.error(f"上传机器人头像失败：{e}")
            return f"设置头像失败：{e}"
        return ""

    async def _capture_bot_identity(
        self, event: AiocqhttpMessageEvent, umo: str
    ) -> tuple[str, bool]:
        """切换前备份机器人原始资料

        Returns:
            (提醒文案, 是否刚刚采集到原始头像)

        硬规则：**绝不从当前账号反推头像**。只有在「当前不是克隆状态」时才采集，
        且只采一次；采集失败就保持「未知」，不在后续切换里重试（否则会把群友
        头像写成机器人原图）。
        """
        data = self.identity.load()
        info = await self._read_login_info(event)
        current_nickname = str(info.get("nickname") or "").strip()
        bot_user_id = str(info.get("user_id") or "").strip()

        if bot_user_id:
            self.identity.remember_user_id(bot_user_id)

        warning = ""
        wearing = data.wearing_clone(current_nickname)

        if not data.nickname_known:
            if current_nickname and not wearing:
                self.identity.remember_nickname(current_nickname, user_id=bot_user_id)
                logger.info(f"已备份机器人原始昵称：{current_nickname}")
            elif wearing:
                warning = (
                    f"注意：当前机器人昵称「{current_nickname}」是我们推上去的克隆昵称，"
                    f"本地没有原始昵称备份，无法自动还原。请手动把机器人昵称改回原样后，"
                    f"执行「还原机器人资料 确认」。"
                )
                logger.warning(warning)

        # 头像：只在「还没被替换过」且「尚未采集」时采集一次
        captured_avatar = False
        if not data.avatar_known and not wearing:
            avatar_b64 = await self._download_avatar(bot_user_id) if bot_user_id else ""
            if avatar_b64:
                captured_avatar = self.identity.remember_avatar(avatar_b64)
                if captured_avatar:
                    logger.info(
                        f"已备份机器人原始头像（{len(avatar_b64) * 3 // 4} 字节）"
                    )
            else:
                logger.warning(
                    "本次未能备份机器人原始头像；为避免把群友头像误存为原图，"
                    "之后不会自动重试。需要时请手动改回头像后执行「还原机器人资料 确认」。"
                )
                warning = (warning + "\n" if warning else "") + (
                    "注意：没能备份机器人原始头像。之后「恢复人格」无法自动还原头像，"
                    "请手动把头像改回原样，再执行「还原机器人资料 确认」。"
                )

        return warning, captured_avatar

    async def _adopt_live_avatar(self, event: AiocqhttpMessageEvent) -> str:
        """把**当前**账号头像确认为机器人原始头像（仅由管理员确认命令调用）

        Returns:
            错误描述（成功返回空串）
        """
        info = await self._read_login_info(event)
        bot_user_id = str(info.get("user_id") or "").strip()
        if bot_user_id:
            self.identity.remember_user_id(bot_user_id)

        avatar_b64 = (
            await self._download_avatar(bot_user_id) if bot_user_id else ""
        )
        if not avatar_b64:
            return "下载当前头像失败，未备份头像"
        self.identity.remember_avatar(avatar_b64, overwrite=True)
        return ""

    async def _apply_original_avatar(self, event: AiocqhttpMessageEvent) -> str:
        """用备份的原图还原头像（没有备份就不动，返回错误描述）"""
        data = self.identity.load()
        if not data.avatar_known:
            return "缺少机器人头像原图备份"
        return await self._sync_qq_avatar(event, data.avatar_b64)

    # =========================
    # =========================
    # 切换 / 恢复人格
    # =========================

    def _identity_lock_for(self) -> asyncio.Lock:
        """账号级操作串行锁（机器人昵称头像是账号级的）"""
        if self._identity_lock is None:
            self._identity_lock = asyncio.Lock()
        return self._identity_lock

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("切换人格")
    async def switch_persona(self, event: AiocqhttpMessageEvent):
        """
        切换人格 @群友

        人格是**会话级**绑定：只切当前会话，别的群不会跟着变
        （哪个群要用，就在哪个群执行一次）。
        """
        ats = [
            str(seg.qq)
            for seg in event.get_messages()[1:]
            if isinstance(seg, At) and str(seg.qq).isdigit()
        ]
        if not ats:
            yield event.plain_result("命令格式：切换人格 @群友")
            return

        target_id = ats[0]
        if self.cfg.message.is_protected_user(target_id):
            yield event.plain_result("该用户在保护名单中，不允许切换")
            return

        profile = self.db.get(target_id)
        if not profile or not profile.clone_prompt.strip():
            yield event.plain_result(
                "该群友暂无可用的克隆人格，请先执行“克隆人格 @群友”"
            )
            return

        umo = event.unified_msg_origin
        cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
        if not cid:
            yield event.plain_result(
                "当前没有对话，请先开始对话或使用 /new 创建一个对话。"
            )
            return

        force_applied_persona_id = (
            await sp.get_async(
                scope="umo",
                scope_id=umo,
                key="session_service_config",
                default={},
            )
        ).get("persona_id")

        async with self._identity_lock_for():
            result = await self._do_switch(
                event, profile, umo, cid, force_applied_persona_id
            )
        yield event.plain_result(result)

    async def _do_switch(
        self,
        event: AiocqhttpMessageEvent,
        profile: UserProfile,
        umo: str,
        cid: str,
        force_applied_persona_id,
    ) -> str:
        """把某个群友的人格切到当前会话，并同步机器人昵称/头像"""
        # 备份原始资料（全局唯一一份；绝不从当前账号反推头像）
        identity_warning, _captured = await self._capture_bot_identity(event, umo)

        try:
            await self.context.persona_manager.update_persona(
                persona_id=profile.persona_id,
                system_prompt=profile.clone_prompt,
            )
        except ValueError:
            await self.context.persona_manager.create_persona(
                persona_id=profile.persona_id,
                system_prompt=profile.clone_prompt,
            )

        await self.context.conversation_manager.update_conversation_persona_id(
            umo, profile.persona_id
        )
        await self.context.conversation_manager.update_conversation(
            umo, cid, history=[]
        )

        force_warn_msg = ""
        if force_applied_persona_id:
            force_warn_msg = "提醒：由于自定义规则，您现在切换的人格将不会生效。"

        # 昵称与头像是两件独立的事：任何一个出问题都不该阻断另一个
        # （曾因为「昵称回读不一致」跳过头像同步，导致「昵称变了但头像不变」）
        nickname_error = await self._sync_qq_nickname(event, profile.nickname)
        self.identity.mark_worn(
            nickname=profile.nickname, umo=umo, owner=profile.user_id
        )

        # 头像：用群友自己的头像（这是克隆的一部分），下载后以 base64 上传
        avatar_error = ""
        avatar_b64 = await self._download_avatar(profile.user_id)
        if avatar_b64:
            avatar_error = await self._sync_qq_avatar(event, avatar_b64)
        else:
            avatar_error = (
                "群友头像下载失败（已尝试 qlogo 两个源），头像未同步；"
                "可在日志里搜索「头像下载」查看原因"
            )
            logger.warning(f"群友头像下载失败：{profile.user_id}")

        msg = (
            f"已将当前对话切换为【{profile.nickname}】的克隆人格，对话历史已清空。"
            f"如需还原，请使用：恢复人格。{force_warn_msg}"
        )
        if nickname_error:
            msg += f"\n⚠️ {nickname_error}"
        if avatar_error:
            msg += f"\n⚠️ {avatar_error}"
        if identity_warning:
            msg += f"\n⚠️ {identity_warning}"
        logger.debug(
            f"已切换克隆人格：{profile.nickname}({profile.user_id})"
            f" 昵称错误={nickname_error!r} 头像错误={avatar_error!r}"
        )
        return msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("恢复人格")
    async def restore_persona(self, event: AiocqhttpMessageEvent):
        """
        恢复人格
        """
        umo = event.unified_msg_origin
        cid = await self.context.conversation_manager.get_curr_conversation_id(umo)

        cfg = self.context.get_config(umo=umo)
        default_persona_id = (
            cfg.get("provider_settings", {}).get("default_personality") or "default"
        )

        if cid:
            await self.context.conversation_manager.update_conversation_persona_id(
                umo, default_persona_id
            )
            await self.context.conversation_manager.update_conversation(
                umo, cid, history=[]
            )

        async with self._identity_lock_for():
            data = self.identity.load()

            restored_nickname = ""
            nickname_error = ""
            avatar_error = ""
            avatar_submitted = False
            nickname_known = data.nickname_known
            avatar_known = data.avatar_known

            if nickname_known:
                nickname_error = await self._sync_qq_nickname(event, data.nickname)
                if not nickname_error:
                    restored_nickname = data.nickname

            # 头像：只用**确认过的原图字节**。没有备份就明确让管理员手动处理，
            # 绝不拿 QQ 号地址或当前账号状态去猜（那时账号上挂着的正是克隆头像）。
            avatar_unresolved = not avatar_known
            if avatar_known:
                avatar_error = await self._apply_original_avatar(event)
                avatar_submitted = not avatar_error
                avatar_unresolved = bool(avatar_error)

            self.identity.clear_worn(umo)
            self.identity.set_pending_avatar_restore(avatar_unresolved)

        msg = f"已恢复默认人格【{default_persona_id}】，对话历史已清空。"
        if restored_nickname:
            msg += f" 机器人昵称已还原为【{restored_nickname}】。"
        if nickname_error:
            msg += f"\n⚠️ {nickname_error}"
        if avatar_submitted:
            msg += " 头像已按备份原图提交还原（协议端未回执时请到 QQ 确认）。"
        else:
            msg += (
                "\n⚠️ 头像未自动还原：本地没有可用的机器人头像原图备份。"
                "请在 QQ 里手动把头像改成原图，然后执行「还原机器人资料 确认」——"
                "之后「恢复人格」就能自动还原头像了。"
            )
        if not nickname_known:
            msg += (
                "\n⚠️ 也没有机器人原始昵称备份：请在 QQ 里改回你要的名字，"
                "再执行「还原机器人资料 确认」。"
            )
        yield event.plain_result(msg)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查看机器人身份")
    async def show_bot_identity(self, event: AiocqhttpMessageEvent):
        """
        查看机器人身份
        """
        data = self.identity.load()
        info = await self._read_login_info(event)
        current = str(info.get("nickname") or "").strip()
        avatar_desc = (
            f"已备份 {max(1, len(data.avatar_b64) * 3 // 4 // 1024)} KB"
            if data.avatar_known
            else "未备份（未知）"
        )
        lines = [
            "【机器人身份备份】",
            f"备份昵称：{data.nickname or '（未备份）'}",
            f"备份 QQ：{data.user_id or '（未知）'}",
            f"备份头像：{avatar_desc}",
            f"当前协议端昵称：{current or '（读取失败）'}",
        ]
        if data.clone_names:
            names = "、".join(list(data.clone_names)[:8])
            lines.append(f"记录过的克隆昵称（{len(data.clone_names)}）：{names}")
        if data.worn:
            worn = "、".join(f"{k}→{v}" for k, v in list(data.worn.items())[:5])
            lines.append(f"会话占用：{worn}")
        if data.pending_avatar_restore:
            lines.append("待处理：上次恢复时缺少头像原图，补好备份后会自动补还原")

        if not current:
            lines.append("⚠️ 读不到当前昵称，无法判断是否处于克隆状态。")
        elif data.wearing_clone(current):
            owner = data.worn_owner(current) or {}
            whose = f"，来自 {owner.get('user_id')}" if owner.get("user_id") else ""
            lines.append(
                f"⚠️ 当前处于克隆状态（昵称「{current}」{whose}），"
                f"执行「恢复人格」可还原。"
            )
        elif not data.nickname_known:
            lines.append("⚠️ 没有原始昵称备份，无法判断是否一致。")
        elif current != data.nickname:
            lines.append("⚠️ 当前昵称与备份不一致，可执行「恢复人格」还原。")
        else:
            lines.append("✅ 当前昵称与备份一致。")

        if not data.avatar_known:
            lines.append(
                "ℹ️ 头像原图未备份：请手动把头像改回原图，再执行「还原机器人资料 确认」。"
            )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查头像")
    async def check_avatar(self, event: AiocqhttpMessageEvent):
        """
        查头像 @群友 —— 诊断：下载该群友头像并尝试设置，报告协议端原始返回
        """
        ats = [
            str(seg.qq)
            for seg in event.get_messages()[1:]
            if isinstance(seg, At) and str(seg.qq).isdigit()
        ]
        if not ats:
            yield event.plain_result("命令格式：查头像 @群友")
            return
        uid = ats[0]
        info = await self._read_login_info(event)

        # 1) 下载测试
        avatar_b64 = await self._download_avatar(uid)
        if not avatar_b64:
            yield event.plain_result(
                f"❌ 下载 {uid} 的头像失败（qlogo 两个源都没拿到图片，详见日志「头像下载」）"
            )
            return
        size_kb = len(avatar_b64) * 3 // 4 // 1024

        # 2) 上传测试（拿原始返回）
        raw: Any = None
        err = ""
        try:
            raw = await event.bot.set_qq_avatar(file=f"base64://{avatar_b64}")
        except Exception as e:
            err = f"{type(e).__name__}: {e}"

        # 3) 回读当前资料
        after = await self._read_login_info(event)
        lines = [
            f"当前机器人昵称：{str(info.get('nickname') or '（读取失败）')}",
            f"下载 {uid} 头像：成功（{size_kb} KB）",
            f"set_qq_avatar 返回：{raw if raw is not None else ('异常 ' + err)}",
        ]
        if isinstance(raw, dict):
            lines.append(
                f"  status={raw.get('status')} retcode={raw.get('retcode')} "
                f"message={raw.get('message') or raw.get('wording') or ''}"
            )
        lines.append(f"回读机器人昵称：{str(after.get('nickname') or '（读取失败）')}")
        lines.append(
            "提示：协议端返回 ok 但 QQ 头像没变时，通常是实现/账号限制；"
            "可到 QQ 客户端确认，或在协议端后台看调用记录。"
        )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("还原机器人资料")
    async def remember_bot_identity(self, event: AiocqhttpMessageEvent):
        """
        还原机器人资料 确认 —— 把「当前」昵称/头像确认为机器人原始资料

        必须显式带上「确认」二字：该命令会把当前账号状态当成原图存下来，
        如果此刻还挂着群友的克隆头像，就会把群友头像存成机器人原图。
        """
        if "确认" not in (event.message_str or ""):
            yield event.plain_result(
                "该命令会把**当前**机器人昵称/头像记为原始资料。\n"
                "请先在 QQ 里把机器人昵称、头像改回原样，然后发送：还原机器人资料 确认"
            )
            return

        info = await self._read_login_info(event)
        nickname = str(info.get("nickname") or "").strip()
        bot_user_id = str(info.get("user_id") or "").strip()
        if not nickname and not bot_user_id:
            yield event.plain_result(
                "读不到机器人昵称/QQ 号，无法记录（请检查协议端连接后重试）"
            )
            return
        if not nickname:
            yield event.plain_result(
                "读不到当前昵称，无法确认。请稍后重试，或先在 QQ 里确认昵称正常。"
            )
            return

        data = self.identity.load()
        if data.wearing_clone(nickname):
            yield event.plain_result(
                f"当前仍处于克隆状态（昵称「{nickname}」）。请先在 QQ 里把昵称和头像"
                f"改回原样（或先执行「恢复人格」），再发送：还原机器人资料 确认"
            )
            return

        async with self._identity_lock_for():
            self.identity.remember_nickname(
                nickname, user_id=bot_user_id, overwrite=True
            )
            self.identity.clear_worn()
            avatar_error = await self._adopt_live_avatar(event)
            if not avatar_error:
                self.identity.set_pending_avatar_restore(False)
            data = self.identity.load()

        if avatar_error:
            yield event.plain_result(
                f"已记录机器人原始昵称【{nickname}】。\n⚠️ 头像未记录：{avatar_error}"
            )
            return

        yield event.plain_result(
            f"已记录机器人原始资料：昵称【{nickname}】、"
            f"头像 {max(1, len(data.avatar_b64) * 3 // 4 // 1024)} KB。\n"
            f"之后「切换人格 / 恢复人格」都会以此为准。"
        )
