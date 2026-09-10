"""离线逻辑测试：验证新增的融合 / 改人格 / 查看克隆 行为。

运行： python tests/test_features.py
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

from astrbot.api.star import Context  # noqa: E402
from astrbot.core.config.astrbot_config import AstrBotConfig  # noqa: E402
from astrbot.core.message.components import At, Plain  # noqa: E402

# 以包的形式加载插件本体，使 main.py 里的相对导入生效
import importlib.util  # noqa: E402

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "portrayal_plugin",
    _PLUGIN_ROOT / "main.py",
    submodule_search_locations=[str(_PLUGIN_ROOT)],
)
plugin_main = importlib.util.module_from_spec(_spec)
sys.modules["portrayal_plugin"] = plugin_main
_spec.loader.exec_module(plugin_main)

from portrayal_plugin.core.config import (  # noqa: E402
    DEFAULT_EDIT_PROMPT,
    DEFAULT_MERGE_PROMPT,
    render_template,
)
from portrayal_plugin.core.model import UserProfile  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {extra}")
        FAILURES.append(f"{name} {extra}")


# ---------------------------------------------------------------- fake event
class FakeBot:
    def __init__(self, nickname: str = "bot", user_id: str = "10000"):
        self.calls: list[tuple] = []
        self.stranger_error: Exception | None = None
        self.nickname = nickname
        self.user_id = user_id

    async def get_stranger_info(self, user_id, no_cache=False):
        self.calls.append(("get_stranger_info", user_id))
        if self.stranger_error:
            raise self.stranger_error
        return {"nickname": f"用户{user_id}", "sex": "男"}

    async def get_login_info(self):
        return {"user_id": self.user_id, "nickname": self.nickname}

    async def set_qq_profile(self, nickname=""):
        self.calls.append(("set_qq_profile", nickname))
        self.nickname = nickname

    async def set_qq_avatar(self, file=""):
        self.calls.append(("set_qq_avatar", file))


class FakeEvent:
    def __init__(
        self,
        message_str: str,
        chain: list,
        is_admin: bool = True,
        self_id: str = "10000",
        bot: "FakeBot | None" = None,
    ):
        self.message_str = message_str
        self._chain = chain
        self._is_admin = is_admin
        self._self_id = self_id
        self.unified_msg_origin = "aiocqhttp:GroupMessage:999"
        self.bot = bot or FakeBot()

    def get_messages(self):
        return self._chain

    def is_admin(self):
        return self._is_admin

    def get_self_id(self):
        return self._self_id

    def get_sender_id(self):
        return "10000"

    def get_group_id(self):
        return "999"

    def plain_result(self, text):
        return ("plain", text)


class FakeConversationManager:
    def __init__(self, cid="cid1"):
        self.cid = cid
        self.persona_calls: list[tuple] = []
        self.history_cleared = 0

    async def get_curr_conversation_id(self, umo):
        return self.cid

    async def update_conversation_persona_id(self, umo, pid):
        self.persona_calls.append((umo, pid))

    async def update_conversation(self, umo, cid, history=None):
        if history == []:
            self.history_cleared += 1


class FakePersonaManager:
    def __init__(self):
        self.personas: list = []
        self.updated: list[tuple] = []
        self.created: list[tuple] = []

    async def get_all_personas(self):
        return list(self.personas)

    async def get_persona(self, persona_id):
        for item in self.personas:
            if item.persona_id == persona_id:
                return item
        return None

    async def update_persona(self, persona_id, system_prompt):
        self.updated.append((persona_id, system_prompt))
        return None

    async def create_persona(self, persona_id, system_prompt):
        self.created.append((persona_id, system_prompt))
        return None


class FakeStarContext:
    """插件 context 的最小替身（会话 + 人格管理）"""

    def __init__(self):
        self.conversation_manager = FakeConversationManager()
        self.persona_manager = FakePersonaManager()

    def register_web_api(self, *args):
        return None

    def get_config(self, umo=None):
        return {"provider_settings": {"default_personality": "default"}}


def _persona_entry(plugin):
    """给测试用的入口：模拟 `切换人格 <名字>` 的分发结果"""

    async def run(event):
        ats = [
            str(seg.qq)
            for seg in event.get_messages()[1:]
            if isinstance(seg, At) and str(seg.qq).isdigit()
        ]
        if ats:
            async for item in plugin.switch_persona(event):
                yield item
            return
        name = plugin._persona_arg(event.message_str, event.get_messages())
        if not name:
            yield event.plain_result("命令格式测试")
            return
        msg = await plugin._switch_named_persona(event, name)
        yield event.plain_result(msg)

    return run


def collect(agen):
    async def run():
        return [item async for item in agen]

    return asyncio.run(run())


# ---------------------------------------------------------------- fixtures
def make_config(**overrides):
    data = {
        "llm": {"provider_id": "", "retry_times": 0},
        "message": {
            "default_query_rounds": 1,
            "max_msg_count": 10,
            "cache_ttl_min": 30,
            "protected_user_ids": [],
        },
        "inject_prompt": False,
        "entry_storage": [
            {"command": "克隆人格", "need_admin": False, "content": "克隆 {nickname}"},
            {"command": "重克隆人格", "need_admin": False, "content": "重克隆 {nickname}"},
        ],
    }
    data.update(overrides)
    return AstrBotConfig(data)


def make_plugin(config=None, tmp: Path | None = None, context=None):
    # 每个 plugin 用独立的配置副本，避免 EntryService 回写污染其它测试
    cfg = copy.deepcopy(config) if config is not None else make_config()
    plugin = plugin_main.PortrayalPlugin(context or Context(), cfg)
    if tmp is not None:
        # 同时改 cfg，保证「用同一个 cfg 重新构造 DB」也指向测试目录
        plugin.cfg.portrayal_file = tmp / "portrayal.json"
        plugin.db.file = plugin.cfg.portrayal_file
    # 测试入口：直接走「切换人格 <名字>」那条分发路径
    plugin.switch_named_persona_entry = _persona_entry(plugin)
    return plugin


# ---------------------------------------------------------------- tests
def test_render_template():
    print("[render_template]")
    check(
        "单花括号替换",
        render_template("你好 {nickname}", nickname="小明") == "你好 小明",
    )
    check(
        "双花括号替换",
        render_template("你好 {{nickname}}", nickname="小明") == "你好 小明",
    )
    check(
        "其它花括号不炸",
        render_template('JSON {"a": 1} {nickname}', nickname="小明") == 'JSON {"a": 1} 小明',
    )
    check("空模板", render_template("") == "")
    check("None 模板", render_template(None) == "")


def test_config_fallbacks(tmp: Path):
    print("[config fallback]")
    cfg = make_config()
    plugin = make_plugin(cfg, tmp)
    check("merge_prompt 缺省回退", plugin.cfg.get_merge_prompt() == DEFAULT_MERGE_PROMPT)
    check("edit_prompt 缺省回退", plugin.cfg.get_edit_prompt() == DEFAULT_EDIT_PROMPT)

    cfg2 = make_config(merge_prompt="自定义融合", edit_prompt="自定义改写")
    plugin2 = make_plugin(cfg2, tmp)
    check("merge_prompt 自定义生效", plugin2.cfg.get_merge_prompt() == "自定义融合")
    check("edit_prompt 自定义生效", plugin2.cfg.get_edit_prompt() == "自定义改写")


def test_parsing(tmp: Path):
    print("[命令解析]")
    plugin = make_plugin(None, tmp)

    e1 = FakeEvent("改人格 @用户 追加：说话更短", [Plain("改人格 "), At("123"), Plain(" 追加：说话更短")])
    check("基础解析 target", plugin._split_target_and_text(e1)[0] == "123")
    check("基础解析 payload", plugin._split_target_and_text(e1)[1] == "追加：说话更短")

    e2 = FakeEvent("改人格　@用户　重置：全量人格", [Plain("改人格　"), At("123"), Plain("　重置：全量人格")])
    check("全角空格 payload", plugin._split_target_and_text(e2)[1] == "重置：全量人格")

    e3 = FakeEvent("改人格 @用户 追加：前半 后半", [At("123"), Plain(" 追加：前半 后半")])
    check("多段文本拼接", plugin._split_target_and_text(e3)[1] == "追加：前半 后半")

    e4 = FakeEvent("改人格 @123 追加：你好", [At("123"), Plain(" 追加：你好")])
    check("fallback 剔除裸 id", plugin._split_target_and_text(e4)[1] == "追加：你好")

    e5 = FakeEvent("改人格 没有at", [Plain("改人格 没有at")])
    check("无 At 返回空", plugin._split_target_and_text(e5) == ("", ""))

    e6 = FakeEvent("改人格 @用户 追加：x", [Plain("改人格 @用户 追加：x")])
    check("无 At 段时返回空", plugin._split_target_and_text(e6)[0] == "")

    e7 = FakeEvent("改人格 10001 追加：手打QQ", [Plain("改人格 10001 追加：手打QQ")])
    check("裸 QQ 号退化解析", plugin._split_target_and_text(e7) == ("10001", "追加：手打QQ"))

    e8 = FakeEvent("改人格 @用户 重置：10001 是你的编号", [Plain("改人格 "), At("123"), Plain(" 重置：10001 是你的编号")])
    check(
        "At 存在时不吃 payload 里的数字",
        plugin._split_target_and_text(e8) == ("123", "重置：10001 是你的编号"),
    )

    e9 = FakeEvent("改人格 10001", [Plain("改人格 10001")])
    check("裸 QQ 号无 payload", plugin._split_target_and_text(e9) == ("10001", ""))

    e10 = FakeEvent("改人格 @用户 追加：后 追加：再", [Plain("改人格 "), At("123"), Plain(" 追加：后"), Plain(" 追加：再")])
    check("多文本段合并", plugin._split_target_and_text(e10)[1] == "追加：后 追加：再")

    check("命令词 split", plugin._get_cmd(FakeEvent("改人格 x y", [])) == "改人格")
    check("命令词 全角空格", plugin._get_cmd(FakeEvent("改人格　x", [])) == "改人格")
    check("命令词 空串", plugin._get_cmd(FakeEvent("", [])) == "")


def _fake_llm(plugin, *, portrait=None, edit=None):
    """替换 LLMService 方法，记录调用参数"""
    captured: dict = {}

    async def fake_generate_portrait(
        texts, profile, system_prompt_template, *, old_clone_prompt="",
        merge_prompt_template="", umo=None,
    ):
        captured["portrait"] = {
            "texts": texts,
            "old_clone_prompt": old_clone_prompt,
            "merge_prompt_template": merge_prompt_template,
            "system_prompt_template": system_prompt_template,
        }
        return portrait if portrait is not None else "新人格"

    async def fake_generate_persona_edit(
        old_clone_prompt, instruction, profile, edit_prompt_template, *, umo=None
    ):
        captured["edit"] = {
            "old": old_clone_prompt,
            "instruction": instruction,
            "template": edit_prompt_template,
        }
        return edit if edit is not None else "改写后人格"

    plugin.llm.generate_portrait = fake_generate_portrait
    plugin.llm.generate_persona_edit = fake_generate_persona_edit
    return captured


def test_edit_persona(tmp: Path):
    print("[改人格]")
    plugin = make_plugin(None, tmp)
    profile = UserProfile(user_id="123", nickname="小明", clone_prompt="原始人格")
    plugin.db.set(profile)
    captured0 = _fake_llm(plugin)

    # --- 追加模式
    ev = FakeEvent("改人格 @用户 追加：少用表情", [At("123"), Plain(" 追加：少用表情")])
    out = collect(plugin.edit_persona(ev))
    check("追加 未调用 LLM", "edit" not in captured0 and "portrait" not in captured0)
    check("追加 写入 DB", plugin.db.get("123").clone_prompt == "原始人格\n少用表情")
    check("追加 有回执", any("已追加" in t for _, t in out), str(out))

    # --- 重置模式
    ev = FakeEvent("改人格 @用户 重置：全新人格", [At("123"), Plain(" 重置：全新人格")])
    collect(plugin.edit_persona(ev))
    check("重置 整段替换", plugin.db.get("123").clone_prompt == "全新人格")

    # --- 重置模式：无档案自动建档
    ev = FakeEvent("改人格 @用户 重置：崭新人格", [At("456"), Plain(" 重置：崭新人格")])
    collect(plugin.edit_persona(ev))
    p456 = plugin.db.get("456")
    check("重置 自动建档", p456 is not None and p456.clone_prompt == "崭新人格")
    check("重置 调用 get_stranger_info", ("get_stranger_info", 456) in ev.bot.calls)

    # --- 重置模式：拉资料失败
    ev2 = FakeEvent("改人格 @用户 重置：x", [At("789"), Plain(" 重置：x")])
    ev2.bot.stranger_error = RuntimeError("boom")
    out2 = collect(plugin.edit_persona(ev2))
    check("重置 拉资料失败有提示", any("失败" in t for _, t in out2))
    check("重置 拉资料失败不写库", plugin.db.get("789") is None)

    # --- LLM 重写模式
    plugin2 = make_plugin(make_config(edit_prompt="改写模板 {nickname}"), tmp)
    plugin2.db.set(UserProfile(user_id="123", nickname="小明", clone_prompt="原始人格"))
    captured = _fake_llm(plugin2, edit="重写后的人格")
    ev = FakeEvent("改人格 @用户 更简短", [At("123"), Plain(" 更简短")])
    out = collect(plugin2.edit_persona(ev))
    check("重写 调用 LLM", captured.get("edit", {}).get("instruction") == "更简短")
    check("重写 传入旧人格", captured["edit"]["old"] == "原始人格")
    check("重写 使用配置模板", captured["edit"]["template"] == "改写模板 {nickname}")
    check("重写 写入 DB", plugin2.db.get("123").clone_prompt == "重写后的人格")
    check("重写 有回执", any("已重写" in t for _, t in out))

    # --- LLM 返回空
    plugin3 = make_plugin(None, tmp)
    plugin3.db.set(UserProfile(user_id="123", nickname="小明", clone_prompt="原始人格"))
    _fake_llm(plugin3, edit="")
    out = collect(plugin3.edit_persona(FakeEvent("改人格 @u x", [At("123"), Plain(" x")])))
    check("重写 空结果不覆盖", plugin3.db.get("123").clone_prompt == "原始人格")
    check("重写 空结果有提示", any("为空" in t for _, t in out))

    # --- LLM 抛异常
    plugin4 = make_plugin(None, tmp)
    plugin4.db.set(UserProfile(user_id="123", nickname="小明", clone_prompt="原始人格"))

    async def boom(*a, **k):
        raise RuntimeError("llm down")

    plugin4.llm.generate_persona_edit = boom
    out = collect(plugin4.edit_persona(FakeEvent("改人格 @u x", [At("123"), Plain(" x")])))
    check("重写 异常不覆盖", plugin4.db.get("123").clone_prompt == "原始人格")
    check("重写 异常有提示", any("已保留原有人格" in t for _, t in out))

    # --- 无 At
    out = collect(plugin.edit_persona(FakeEvent("改人格", [Plain("改人格")])))
    check("无 At 给用法", any("命令格式" in t for _, t in out))

    # --- 追加但无旧人格
    plugin5 = make_plugin(None, tmp)
    plugin5.db.set(UserProfile(user_id="123", nickname="小明", clone_prompt=""))
    out = collect(plugin5.edit_persona(FakeEvent("改人格 @u 追加：x", [At("123"), Plain(" 追加：x")])))
    check("追加 无旧人格被拒", any("暂无可用的克隆人格" in t for _, t in out))

    # --- 追加空内容
    out = collect(plugin5.edit_persona(FakeEvent("改人格 @u 追加：", [At("123"), Plain(" 追加：")])))
    check("追加 空内容被拒", any("不能为空" in t for _, t in out))

    # --- 保护名单
    plugin6 = make_plugin(
        make_config(
            message={
                "default_query_rounds": 1,
                "max_msg_count": 10,
                "cache_ttl_min": 30,
                "protected_user_ids": ["123"],
            }
        ),
        tmp,
    )
    out = collect(plugin6.edit_persona(FakeEvent("改人格 @u 重置：x", [At("123"), Plain(" 重置：x")])))
    check("保护名单被拒", any("保护名单" in t for _, t in out))


def test_edit_mode_prefix(tmp: Path):
    print("[改人格 模式前缀]")
    plugin = make_plugin(None, tmp)
    parse = plugin._parse_edit_mode

    check("全角冒号 重置", parse("重置：abc") == ("reset", "abc"))
    check("半角冒号 重置（回归）", parse("重置:abc") == ("reset", "abc"), str(parse("重置:abc")))
    check("空格 重置", parse("重置 abc") == ("reset", "abc"))
    check("全角空格 重置", parse("重置\u3000abc") == ("reset", "abc"))
    check("全角冒号 追加", parse("追加：x") == ("append", "x"))
    check("半角冒号 追加", parse("追加:x") == ("append", "x"))
    check("冒号后带空格", parse("重置： abc ") == ("reset", "abc"))
    check("多行正文", parse("重置：第一行\n第二行") == ("reset", "第一行\n第二行"))
    check("前后缀无分隔符不认", parse("重置abc") is None)
    check("普通改写要求走 rewrite", parse("说话更短一点") == ("rewrite", "说话更短一点"), str(parse("说话更短一点")))
    check("只看关键词也判定", parse("重置 一下语气") == ("reset", "一下语气"))
    check("空正文", parse("追加：") == ("append", ""))

    # 端到端：半角冒号必须走「重置」（不进 LLM）
    plugin2 = make_plugin(None, tmp)
    plugin2.db.set(UserProfile(user_id="123", nickname="小明", clone_prompt="旧人格"))
    captured = _fake_llm(plugin2, edit="不该被调用")
    ev = FakeEvent("改人格 @用户 重置:半角冒号新人格", [Plain("改人格 "), At("123"), Plain(" 重置:半角冒号新人格")])
    out = collect(plugin2.edit_persona(ev))
    check("半角冒号 重置生效", plugin2.db.get("123").clone_prompt == "半角冒号新人格", str(out))
    check("半角冒号 未调用 LLM", "edit" not in captured)

    # 前缀写错时给出用法提示，且不改库
    plugin3 = make_plugin(None, tmp)
    plugin3.db.set(UserProfile(user_id="123", nickname="小明", clone_prompt="旧人格"))
    captured3 = _fake_llm(plugin3, edit="不该被调用")
    ev3 = FakeEvent("改人格 @用户 重置abc", [Plain("改人格 "), At("123"), Plain(" 重置abc")])
    out3 = collect(plugin3.edit_persona(ev3))
    check("前缀写错时提示用法", any("正确写法" in t for _, t in out3), str(out3))
    check("前缀写错时不改库", plugin3.db.get("123").clone_prompt == "旧人格")
    check("前缀写错时不调 LLM", "edit" not in captured3)


def test_switch_named_persona(tmp: Path):
    print("[切换人格 <人格名>]")
    plugin = make_plugin(None, tmp)
    ctx = FakeStarContext()

    # AstrBot 里已有的人格（非群员克隆）
    class P:
        def __init__(self, pid, prompt):
            self.persona_id = pid
            self.system_prompt = prompt

    personas = [
        P("岑知秋", "你是岑知秋，一个角色"),
        P("岑知夏", "另一个名字相近的人格"),
        P("宵宫", "你是宵宫"),
        P("陈舟", "抽象老哥"),
        P("小明_123", "群员克隆"),
    ]

    async def get_all_personas():
        return list(personas)

    async def get_persona(pid):
        for item in personas:
            if item.persona_id == pid:
                return item
        return None

    ctx.persona_manager.get_all_personas = get_all_personas
    ctx.persona_manager.get_persona = get_persona
    plugin.context = ctx
    plugin.db.set(UserProfile(user_id="123", nickname="小明", clone_prompt="群员克隆"))

    # 参数解析
    check("解析人格名", plugin._persona_arg("切换人格 岑知秋") == "岑知秋")
    check("解析带引号", plugin._persona_arg("切换人格 「岑知秋」") == "岑知秋")
    check("无参数返回空", plugin._persona_arg("切换人格") == "")

    # 回归：引用消息会把引用段渲染进 message_str（[MSG_ID:...]），
    # 之前它被当成名字的一部分，导致「明明名字对却说没找到」
    check(
        "引用残留被剔除",
        plugin._persona_arg("切换人格 岑知秋 [MSG_ID:1452650487]") == "岑知秋",
        plugin._persona_arg("切换人格 岑知秋 [MSG_ID:1452650487]"),
    )
    check(
        "引用前缀+残留都被剔除",
        plugin._persona_arg(
            "[引用消息(幻: x)] 切换人格 宵宫 [MSG_ID:-443483943]",
            [Plain("[引用消息(幻: x)] 切换人格 宵宫 [MSG_ID:-443483943]")],
        )
        == "宵宫",
    )
    check(
        "图片残留被剔除",
        plugin._persona_arg("切换人格 苏晴 [图片]") == "苏晴",
        plugin._persona_arg("切换人格 苏晴 [图片]"),
    )
    check(
        "消息链优先且能跳过 At",
        plugin._persona_arg("切换人格 陈舟", [At("10000"), Plain(" 切换人格 陈舟")])
        == "陈舟",
    )
    check("全角空格也能切", plugin._persona_arg("切换人格\u3000岑知秋") == "岑知秋")

    # 匹配
    rows = [("岑知秋", "x", "AstrBot"), ("宵宫", "y", "AstrBot"), ("岑知夏", "z", "AstrBot")]
    check("精确匹配", len(plugin._match_persona("岑知秋", rows)) == 1)
    check("忽略大小写", len(plugin._match_persona("ABC", [("abc", "", "")])) == 1)
    check("唯一前缀可匹配", [r[0] for r in plugin._match_persona("宵", rows)] == ["宵宫"])
    check("多个匹配全返回", len(plugin._match_persona("岑知", rows)) == 2)
    check("无匹配返回空", plugin._match_persona("不存在", rows) == [])

    # 切到 AstrBot 自带人格
    ev = FakeEvent("切换人格 岑知秋", [Plain("切换人格 岑知秋")])
    bot = FakeBot(nickname="真机器人", user_id="999")
    ev = FakeEvent("切换人格 岑知秋", [Plain("切换人格 岑知秋")], bot=bot)
    out = collect(plugin.switch_named_persona_entry(ev))
    text = out[0][1]
    check("切换成功提示", "已把当前会话切到人格【岑知秋】" in text, text)
    check("标明来源是 AstrBot", "来源：AstrBot" in text, text)
    check("昵称改为该人格 ID", bot.nickname == "岑知秋", str(bot.nickname))
    check("回执说明昵称已改", "机器人昵称已改为【岑知秋】" in text, text)
    check("回执说明头像未改", "头像未改动" in text, text)
    check(
        "头像未被上传（非克隆无来源头像）",
        [c for c in bot.calls if c[0] == "set_qq_avatar"] == [],
        str([c for c in bot.calls if c[0] == "set_qq_avatar"]),
    )
    check("备份了机器人原始昵称", plugin.identity.load().nickname == "真机器人",
          str(plugin.identity.load().nickname))
    check("登记了占用（便于恢复）", plugin.identity.load().worn != {})
    check("会话已切到该人格", ctx.conversation_manager.persona_calls[-1][1] == "岑知秋")
    check("清空了历史", ctx.conversation_manager.history_cleared >= 1)

    # 切到群员克隆（来自本插件）时来源标注为克隆
    bot2 = FakeBot(nickname="真机器人", user_id="999")
    ev2 = FakeEvent("切换人格 小明_123", [Plain("切换人格 小明_123")], bot=bot2)
    out2 = collect(plugin.switch_named_persona_entry(ev2))
    check("克隆来源标注", "来源：群员克隆" in out2[0][1], out2[0][1])
    check("克隆也把昵称改成 ID", bot2.nickname == "小明_123", str(bot2.nickname))
    check("克隆不提示无头像", "没有对应的群友头像可用" not in out2[0][1])

    # 端到端：引用消息里的名字必须能切（之前这里会报「没找到人格」）
    bot_ref = FakeBot(nickname="真机器人", user_id="999")
    ev_ref = FakeEvent(
        "[引用消息(x)] 切换人格 岑知秋 [MSG_ID:1452650487]",
        [Plain("[引用消息(x)] 切换人格 岑知秋 [MSG_ID:1452650487]")],
        bot=bot_ref,
    )
    out_ref = collect(plugin.switch_named_persona_entry(ev_ref))
    check(
        "引用消息里也能切人格",
        "已把当前会话切到人格【岑知秋】" in out_ref[0][1],
        out_ref[0][1],
    )

    # 找不到时给出候选
    ev3 = FakeEvent("切换人格 不存在的人", [Plain("切换人格 不存在的人")])
    out3 = collect(plugin.switch_named_persona_entry(ev3))
    check("找不到时给出候选", "没找到人格" in out3[0][1] and "人格列表" in out3[0][1], out3[0][1])

    # 多个候选时要求写全名
    ev4 = FakeEvent("切换人格 岑知", [Plain("切换人格 岑知")])
    out4 = collect(plugin.switch_named_persona_entry(ev4))
    check("歧义时要求写全名", "匹配到多个人格" in out4[0][1], out4[0][1])

    # 没有参数时给出用法
    ev5 = FakeEvent("切换人格", [Plain("切换人格")])
    out5 = collect(plugin.switch_persona(ev5))
    check("无参数给用法", "命令格式" in out5[0][1] and "人格列表" in out5[0][1], out5[0][1])

    # 人格列表命令
    out6 = collect(plugin.list_personas(FakeEvent("人格列表", [Plain("人格列表")])))
    listed = out6[0][1]
    check("列表含自带人格", "岑知秋" in listed and "[自带]" in listed, listed[:120])
    check("列表标记克隆人格", "[克隆]" in listed and "小明_123" in listed, listed[:120])
    check("列表给出切换用法", "切换人格 <名称>" in listed)

    # 读不到人格时给出引导
    ctx2 = FakeStarContext()

    async def empty():
        return []

    ctx2.persona_manager.get_all_personas = empty
    plugin2 = make_plugin(None, tmp)
    plugin2.context = ctx2
    out7 = collect(plugin2.list_personas(FakeEvent("人格列表", [Plain("人格列表")])))
    check("无人格时引导创建", "人格设定" in out7[0][1], out7[0][1])


def test_view_clone(tmp: Path):
    print("[查看克隆]")
    plugin = make_plugin(None, tmp)
    plugin.db.set(UserProfile(user_id="123", nickname="小明", clone_prompt="人格正文"))

    out = collect(plugin.view_clone(FakeEvent("查看克隆 @u", [At("123")])))
    check("输出全文", any("人格正文" in t for _, t in out))

    out = collect(plugin.view_clone(FakeEvent("查看克隆 @u", [At("999")])))
    check("无记录提示", any("暂无该用户画像记录" in t for _, t in out))

    plugin.db.set(UserProfile(user_id="888", nickname="小红", clone_prompt=""))
    out = collect(plugin.view_clone(FakeEvent("查看克隆 @u", [At("888")])))
    check("空人格提示", any("暂未生成克隆人格" in t for _, t in out))

    out = collect(plugin.view_clone(FakeEvent("查看克隆", [Plain("查看克隆")])))
    check("无 At 给用法", any("命令格式" in t for _, t in out))

    # 超长人格应附加长度提示，且输出用 strip 后的正文
    plugin.db.set(
        UserProfile(user_id="777", nickname="小刚", clone_prompt="  " + "长" * 2100 + "  ")
    )
    out = collect(plugin.view_clone(FakeEvent("查看克隆 @u", [At("777")])))
    check("超长有提示", any("不建议直接群发全文" in t for _, t in out))
    body = [t for _, t in out if "当前的克隆人格" in t]
    check("超长正文已 strip", bool(body) and body[0].rstrip().endswith("长" * 10), str(len(body)))


def test_portrait_merge(tmp: Path):
    print("[克隆人格 融合分支]")
    plugin = make_plugin(make_config(merge_prompt="融合指令 {nickname}"), tmp)
    plugin.db.set(UserProfile(user_id="123", nickname="小明", clone_prompt="旧人格"))

    captured = _fake_llm(plugin, portrait="融合后人格")

    async def fake_get_user_texts(event, target_id, *, max_rounds):
        class R:
            texts = ["你好", "在吗"]
            scanned_messages = 200
            from_cache = False
            count = 2
            is_empty = False

        return R()

    plugin.msg.get_user_texts = fake_get_user_texts

    # 真实消息链形如 [Plain("克隆人格 "), At(群友)]
    ev = FakeEvent("克隆人格 @用户", [Plain("克隆人格 "), At("123")])
    out = collect(plugin.get_portrayal(ev))
    check("融合 传旧人格", captured["portrait"]["old_clone_prompt"] == "旧人格")
    check("融合 传融合模板", captured["portrait"]["merge_prompt_template"] == "融合指令 {nickname}")
    check("融合 写入 DB", plugin.db.get("123").clone_prompt == "融合后人格")
    check("融合 有融合提示", any("融合" in t for _, t in out))

    # --- 无旧人格 → 全新生成
    plugin2 = make_plugin(None, tmp)
    plugin2.msg.get_user_texts = fake_get_user_texts
    captured2 = _fake_llm(plugin2, portrait="全新人格")
    collect(
        plugin2.get_portrayal(FakeEvent("克隆人格 @用户", [Plain("克隆人格 "), At("123")]))
    )
    check("全新 不传旧人格", captured2["portrait"]["old_clone_prompt"] == "")
    check("全新 写入 DB", plugin2.db.get("123").clone_prompt == "全新人格")

    # --- 重克隆人格 无视旧人格
    plugin3 = make_plugin(make_config(merge_prompt="融合指令"), tmp)
    plugin3.db.set(UserProfile(user_id="123", nickname="小明", clone_prompt="旧人格"))
    plugin3.msg.get_user_texts = fake_get_user_texts
    captured3 = _fake_llm(plugin3, portrait="重克隆结果")
    collect(
        plugin3.get_portrayal(
            FakeEvent("重克隆人格 @用户", [Plain("重克隆人格 "), At("123")])
        )
    )
    check("重克隆 不传旧人格", captured3["portrait"]["old_clone_prompt"] == "")
    check("重克隆 写入 DB", plugin3.db.get("123").clone_prompt == "重克隆结果")

    # --- 非克隆命令：既不能走融合，也不能丢已有画像 / 克隆人格
    plugin4 = make_plugin(None, tmp)
    plugin4.db.set(
        UserProfile(
            user_id="123",
            nickname="小明",
            clone_prompt="旧人格",
            portrait="旧画像",
            timestamp=12345,
        )
    )
    plugin4.msg.get_user_texts = fake_get_user_texts
    captured4 = _fake_llm(plugin4, portrait="画像结果")
    out4 = collect(
        plugin4.get_portrayal(FakeEvent("画像 @用户", [Plain("画像 "), At("123")]))
    )
    check(
        "画像 不走融合",
        captured4["portrait"]["old_clone_prompt"] == ""
        and captured4["portrait"]["merge_prompt_template"] == "",
    )
    check("画像 无融合提示", not any("融合" in t for _, t in out4))
    check("画像 不动克隆人格", plugin4.db.get("123").clone_prompt == "旧人格")
    check("画像 更新画像字段", plugin4.db.get("123").portrait == "画像结果")

    # --- 非克隆命令：没有旧档案时也不应携带融合参数
    plugin4b = make_plugin(None, tmp)
    plugin4b.msg.get_user_texts = fake_get_user_texts
    captured4b = _fake_llm(plugin4b, portrait="画像结果")
    collect(
        plugin4b.get_portrayal(FakeEvent("画像 @用户", [Plain("画像 "), At("999")]))
    )
    check("画像 无档案也不融合", captured4b["portrait"]["old_clone_prompt"] == "")

    # --- 找对象 同样不应融合
    plugin4c = make_plugin(None, tmp)
    plugin4c.entry_service.add_entry(
        [{"command": "找对象", "need_admin": False, "content": "找对象 {nickname}"}]
    )
    plugin4c.db.set(UserProfile(user_id="123", nickname="小明", clone_prompt="旧人格"))
    plugin4c.msg.get_user_texts = fake_get_user_texts
    captured4c = _fake_llm(plugin4c, portrait="推荐结果")
    collect(
        plugin4c.get_portrayal(FakeEvent("找对象 @用户", [Plain("找对象 "), At("123")]))
    )
    check("找对象 不走融合", captured4c["portrait"]["old_clone_prompt"] == "")
    check("找对象 不动克隆人格", plugin4c.db.get("123").clone_prompt == "旧人格")

    # --- LLM 返回空：不覆盖
    plugin5 = make_plugin(None, tmp)
    plugin5.db.set(UserProfile(user_id="123", nickname="小明", clone_prompt="旧人格"))
    plugin5.msg.get_user_texts = fake_get_user_texts
    _fake_llm(plugin5, portrait="   ")
    out = collect(
        plugin5.get_portrayal(FakeEvent("克隆人格 @用户", [Plain("克隆人格 "), At("123")]))
    )
    check("空结果不覆盖人格", plugin5.db.get("123").clone_prompt == "旧人格")
    check("空结果有提示", any("结果为空" in t for _, t in out))

    # --- 重克隆人格 条目缺失时回退到「克隆人格」的提示词
    plugin6 = make_plugin(None, tmp)
    plugin6.msg.get_user_texts = fake_get_user_texts
    captured6 = _fake_llm(plugin6, portrait="结果")
    restored = [
        e for e in plugin6.entry_service.entries if e.command == "重克隆人格"
    ]
    plugin6.entry_service.entries = [
        e for e in plugin6.entry_service.entries if e.command != "重克隆人格"
    ]
    collect(
        plugin6.get_portrayal(
            FakeEvent("重克隆人格 @用户", [Plain("重克隆人格 "), At("123")])
        )
    )
    plugin6.entry_service.entries.extend(restored)
    check(
        "重克隆条目缺失时回退",
        captured6["portrait"]["system_prompt_template"]
        == plugin6.entry_service.get_entry("克隆人格").content,
    )

    # --- need_admin 拦截
    plugin7 = make_plugin(None, tmp)
    for entry in plugin7.entry_service.entries:
        if entry.command == "克隆人格":
            entry._data["need_admin"] = True
    plugin7.msg.get_user_texts = fake_get_user_texts
    out = collect(
        plugin7.get_portrayal(
            FakeEvent("克隆人格 @用户", [Plain("克隆人格 "), At("123")], is_admin=False)
        )
    )
    check("need_admin 拦截", out == [])

    # --- 无 At
    plugin8 = make_plugin(None, tmp)
    out = collect(plugin8.get_portrayal(FakeEvent("克隆人格", [Plain("克隆人格")])))
    check("克隆人格 无 At 给用法", any("命令格式" in t for _, t in out))


def test_llm_prompt_builders(tmp: Path):
    print("[LLM 提示词构建]")

    class FakeProviderMeta:
        id = "fake"

    class FakeProvider:
        def meta(self):
            return FakeProviderMeta()

        async def text_chat(self, system_prompt, prompt):
            FakeProvider.last = (system_prompt, prompt)

            class R:
                completion_text = "ok"

            return R()

    cfg = make_config(merge_prompt="融合 {nickname}", edit_prompt="改写 {nickname}")
    plugin = make_plugin(cfg, tmp)
    fake_provider = FakeProvider()
    plugin.cfg.get_provider = lambda umo=None: fake_provider

    profile = UserProfile(user_id="1", nickname="小明", sex="男")
    llm = plugin.llm

    resp = asyncio.run(
        llm.generate_portrait(
            ["你好"], profile, "克隆 {nickname}",
            old_clone_prompt="旧人格", merge_prompt_template="融合 {nickname}",
        )
    )
    system_prompt, prompt = FakeProvider.last
    check("融合 system 渲染", system_prompt == "克隆 小明")
    check("融合 prompt 含旧人格", "旧人格" in prompt)
    check("融合 prompt 含聊天记录", "你好" in prompt)
    check("融合 prompt 含融合指令", "融合 小明" in prompt)
    check("返回内容", resp == "ok")

    asyncio.run(
        llm.generate_persona_edit("旧人格", "更简短", profile, "改写 {nickname}")
    )
    system_prompt, prompt = FakeProvider.last
    check("改写 system 渲染", system_prompt == "改写 小明")
    check("改写 prompt 含旧人格", "旧人格" in prompt)
    check("改写 prompt 含要求", "更简短" in prompt)

    # 无旧人格 → 走原始画像 prompt
    asyncio.run(llm.generate_portrait(["你好"], profile, "克隆 {nickname}"))
    _, prompt = FakeProvider.last
    check("无旧人格 prompt 不含旧人格", "旧人格" not in prompt)

    # 用户提示词里的花括号不再炸
    asyncio.run(llm.generate_portrait(["x"], profile, '克隆 {{nickname}} {"a":1}'))
    system_prompt, _ = FakeProvider.last
    check("花括号安全", system_prompt == '克隆 小明 {"a":1}')


def test_schema_and_yaml():
    print("[配置 schema / 内置提示词]")
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "_conf_schema.json").read_text("utf-8"))
    check("schema 有 merge_prompt", schema.get("merge_prompt", {}).get("type") == "text")
    check("schema 有 edit_prompt", schema.get("edit_prompt", {}).get("type") == "text")
    check("schema merge_prompt 有默认值", bool(schema["merge_prompt"].get("default")))
    check("schema edit_prompt 有默认值", bool(schema["edit_prompt"].get("default")))
    check(
        "schema 默认值与代码兜底一致",
        schema["merge_prompt"]["default"] == DEFAULT_MERGE_PROMPT
        and schema["edit_prompt"]["default"] == DEFAULT_EDIT_PROMPT,
    )
    check("schema 字段顺序含 entry_storage", "entry_storage" in schema)

    import yaml

    data = yaml.safe_load((root / "builtin_prompts.yaml").read_text("utf-8"))
    commands = [item["command"] for item in data]
    check("yaml 有重克隆人格", "重克隆人格" in commands)
    check("yaml 条目顺序", commands.index("重克隆人格") == commands.index("克隆人格") + 1)
    check("yaml 全部含 content", all(item.get("content", "").strip() for item in data))

    meta = yaml.safe_load((root / "metadata.yaml").read_text("utf-8"))
    changelog = (root / "CHANGELOG.md").read_text("utf-8")
    check(
        "CHANGELOG 有与 metadata 对应的版本段",
        f"## {meta['version']}" in changelog,
    )
    check("CHANGELOG 不再用 Unreleased 占位", "## Unreleased" not in changelog)


def test_entry_loading(tmp: Path):
    print("[内置提示词装载]")
    plugin = make_plugin(make_config(entry_storage=[]), tmp)
    commands = [e.command for e in plugin.entry_service.entries]
    check("空配置能装载全部内置条目", "重克隆人格" in commands and "克隆人格" in commands)
    check(
        "新条目 need_admin 已补齐",
        plugin.entry_service.get_entry("重克隆人格").need_admin is False,
    )
    check(
        "存储里也补齐了 need_admin",
        all("need_admin" in item for item in plugin.cfg.entry_storage),
    )

    # 重复装载不应产生重复条目
    plugin.entry_service._load_prompts()
    commands2 = [e.command for e in plugin.entry_service.entries]
    check("重复装载不重复", len(commands2) == len(set(commands2)))

    # 用户自定义同名条目优先
    plugin2 = make_plugin(
        make_config(
            entry_storage=[
                {"command": "重克隆人格", "need_admin": True, "content": "我的自定义"}
            ]
        ),
        tmp,
    )
    entry = plugin2.entry_service.get_entry("重克隆人格")
    check("同名自定义条目优先", entry.content == "我的自定义" and entry.need_admin is True)


def main():
    tmp = _PLUGIN_ROOT / ".test_tmp" / "case"
    if tmp.exists():
        import shutil

        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    test_render_template()
    test_config_fallbacks(tmp)
    test_parsing(tmp)
    test_edit_persona(tmp)
    test_edit_mode_prefix(tmp)
    test_switch_named_persona(tmp)
    test_view_clone(tmp)
    test_portrait_merge(tmp)
    test_llm_prompt_builders(tmp)
    test_schema_and_yaml()
    test_entry_loading(tmp)
    print()
    if FAILURES:
        print(f"FAILED {len(FAILURES)}:")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
