"""机器人昵称/头像备份与还原的离线测试。

对应「切换人格后昵称头像串了」的修复：
  python tests/test_bot_identity.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

from test_features import At, Plain, check, collect, make_plugin, plugin_main  # noqa: E402

from portrayal_plugin.core.bot_identity import (  # noqa: E402
    BotIdentity,
    BotIdentityStore,
    build_avatar_urls,
    download_avatar_b64,
    sniff_image_type,
)

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
HTML = b"<!DOCTYPE html><html><body>404</body></html>"


def new_tmp(name: str) -> Path:
    tmp = ROOT / ".test_tmp" / name
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


# ---------------------------------------------------------------- 纯逻辑
def test_sniff_and_urls():
    print("[头像识别 / 地址]")
    check("识别 PNG", sniff_image_type(PNG) == "png")
    check("识别 JPG", sniff_image_type(JPG) == "jpg")
    check("识别 GIF", sniff_image_type(b"GIF89a" + b"\x00" * 10) == "gif")
    check("识别 WEBP", sniff_image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp")
    check("拒绝 HTML", sniff_image_type(HTML) is None)
    check("拒绝过短", sniff_image_type(b"\x89PNG") is None)

    urls = build_avatar_urls("10001")
    check("生成多个候选地址", len(urls) >= 2, str(urls))
    check("地址包含 QQ 号", all("10001" in u for u in urls))
    check("非数字返回空", build_avatar_urls("abc") == [])
    check("数字带空格也能用", build_avatar_urls(" 10001 ") and True)


def test_identity_model():
    print("[身份模型]")
    data = BotIdentity()
    check("初始未备份", not data.ready)

    data.mark_clone_name("小明", "123")
    check("记录克隆昵称", data.is_clone_name("小明") and not data.is_clone_name("真名"))
    check("占用者可查", (data.worn_owner("小明") or {}).get("user_id") == "123")
    check("空昵称不算克隆", not data.is_clone_name(""))
    check("None 不算克隆", not data.is_clone_name(None))

    check("序列化往返", BotIdentity.from_dict(data.to_dict()).clone_names == data.clone_names)
    check("脏数据不炸", BotIdentity.from_dict("nope").ready is False)
    check("字段类型异常也不炸", BotIdentity.from_dict({"clone_names": 5}).clone_names == {})


def test_store(tmp: Path):
    print("[身份存储]")
    path = tmp / "bot_identity.json"
    store = BotIdentityStore(path)

    check("初始为空", store.load().ready is False)

    store.remember_original(nickname="机器人", user_id="999")
    check("记录昵称", store.load().nickname == "机器人")
    check("落盘", path.exists())
    check("重新打开仍在", BotIdentityStore(path).load().nickname == "机器人")

    # 关键：默认只补空缺，不会把真名覆盖成克隆名
    store.remember_original(nickname="小明")
    check("不覆盖已有昵称", store.load().nickname == "机器人")

    store.remember_original(nickname="新真名", overwrite=True)
    check("overwrite 可以改", store.load().nickname == "新真名")

    store.mark_worn(nickname="小明", user_id="123", umo="umo1", owner="123")
    data = store.load()
    check("记录占用", data.worn.get("umo1") == "123" and data.is_clone_name("小明"))

    store.clear_worn("umo1")
    check("清除占用", store.load().worn == {})
    check("克隆昵称记录保留", store.load().is_clone_name("小明"))

    # 头像只补空缺
    store.remember_original(avatar_b64="AAAA")
    check("记录头像", store.load().avatar_b64 == "AAAA")
    store.remember_original(avatar_b64="BBBB")
    check("不覆盖已有头像", store.load().avatar_b64 == "AAAA")
    store.remember_original(avatar_b64="BBBB", overwrite=True)
    check("overwrite 覆盖头像", store.load().avatar_b64 == "BBBB")

    desc = store.describe()
    check("摘要字段", desc["nickname"] == "新真名" and desc["has_avatar"] is True)
    check("摘要含克隆名", "小明" in desc["clone_names"])

    store.reset()
    check("reset 清空", store.load().ready is False)
    check("reset 删文件", not path.exists())

    # 损坏的状态文件不应导致崩溃
    path.write_text("{ broken", encoding="utf-8")
    broken = BotIdentityStore(path)
    check("损坏状态文件可恢复", broken.load().ready is False)


def test_download_avatar(tmp: Path):
    print("[头像下载]")
    calls: list[str] = []

    class FakeResp:
        def __init__(self, body: bytes, status: int = 200):
            self._body = body
            self.status = status

        async def read(self):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def __init__(self, mapping):
            self.mapping = mapping

        def get(self, url):
            calls.append(url)
            return FakeResp(self.mapping.get(url, HTML))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    import aiohttp

    urls = build_avatar_urls("10001")

    # 主源返回 HTML，备用源返回 PNG -> 应回退成功
    def factory_first_bad(*a, **k):
        return FakeSession({urls[1]: PNG})

    real_client = aiohttp.ClientSession
    aiohttp.ClientSession = factory_first_bad
    try:
        body = asyncio.run(download_avatar_b64("10001"))
    finally:
        aiohttp.ClientSession = real_client
    check("回退到备用源", bool(body) and len(calls) == 2, str(calls[:3]))

    # 全部失败 -> 空串
    calls.clear()

    def factory_all_bad(*a, **k):
        return FakeSession({})

    aiohttp.ClientSession = factory_all_bad
    try:
        body = asyncio.run(download_avatar_b64("10001"))
    finally:
        aiohttp.ClientSession = real_client
    check("全部失败返回空", body == "")

    # 非数字直接返回空，不发请求
    calls.clear()
    aiohttp.ClientSession = factory_all_bad
    try:
        body = asyncio.run(download_avatar_b64("abc"))
    finally:
        aiohttp.ClientSession = real_client
    check("非法 QQ 号不发请求", body == "" and calls == [])

    # 超大响应被拒绝
    calls.clear()

    def factory_huge(*a, **k):
        return FakeSession({"https://x/1": PNG + b"\x00" * (5 * 1024 * 1024)})

    aiohttp.ClientSession = factory_huge
    try:
        body = asyncio.run(
            download_avatar_b64("1", urls=["https://x/1"], max_bytes=1024)
        )
    finally:
        aiohttp.ClientSession = real_client
    check("超大头像被拒绝", body == "")

    # 指定 urls 参数生效
    calls.clear()

    def factory_ok(*a, **k):
        return FakeSession({"https://y/1": JPG})

    aiohttp.ClientSession = factory_ok
    try:
        body = asyncio.run(download_avatar_b64("1", urls=["https://y/1"]))
    finally:
        aiohttp.ClientSession = real_client
    check("指定地址可用", bool(body) and calls == ["https://y/1"])


# ---------------------------------------------------------------- 命令层
class FakeBot:
    def __init__(self, nickname="机器人", user_id="999"):
        self.nickname = nickname
        self.user_id = user_id
        self.calls: list[tuple] = []
        self.fail_profile = False
        self.fail_avatar = False
        self.nickname_after_set = None  # 模拟协议端没改成功

    async def get_login_info(self):
        self.calls.append(("get_login_info",))
        return {"nickname": self.nickname, "user_id": self.user_id}

    async def set_qq_profile(self, nickname=""):
        self.calls.append(("set_qq_profile", nickname))
        if self.fail_profile:
            raise RuntimeError("profile refused")
        if self.nickname_after_set is None:
            self.nickname = nickname
        else:
            self.nickname = self.nickname_after_set

    async def set_qq_avatar(self, file=""):
        self.calls.append(("set_qq_avatar", file))
        if self.fail_avatar:
            raise RuntimeError("avatar refused")

    async def get_stranger_info(self, user_id, no_cache=False):
        return {"nickname": f"用户{user_id}", "sex": "男"}


class FakeEvent:
    def __init__(self, message_str, chain, bot=None, self_id="10000"):
        self.message_str = message_str
        self._chain = chain
        self._self_id = self_id
        self.unified_msg_origin = "aiocqhttp:GroupMessage:999"
        self.bot = bot or FakeBot()

    def get_messages(self):
        return self._chain

    def is_admin(self):
        return True

    def get_self_id(self):
        return self._self_id

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
        self.created: list[tuple] = []
        self.updated: list[tuple] = []

    async def update_persona(self, persona_id, system_prompt):
        self.updated.append((persona_id, system_prompt))
        return None


class FakeStarContext:
    def __init__(self):
        self.conversation_manager = FakeConversationManager()
        self.persona_manager = FakePersonaManager()
        self.registered_web_apis: list[tuple] = []

    def register_web_api(self, *a):
        self.registered_web_apis.append(a)

    def get_config(self, umo=None):
        return {"provider_settings": {"default_personality": "默认人格"}}


def wire(plugin, ctx):
    plugin.context = ctx
    return plugin


def _patch_download(fake_avatar: str = "ZmFrZQ=="):
    """替换头像下载（main.py 以 from ... import 方式引入，需同时替换两处）"""
    import portrayal_plugin.core.bot_identity as bi
    import portrayal_plugin.main as m

    async def fake_download(user_id, **kwargs):
        return fake_avatar

    m.download_avatar_b64 = fake_download
    bi.download_avatar_b64 = fake_download
    return fake_download


def _isolate(tmp: Path, name: str):
    """把插件的数据目录 / 身份文件 / 头像下载都换成测试用的实现"""
    import portrayal_plugin.main as m

    sub = tmp / name
    sub.mkdir(parents=True, exist_ok=True)
    for leftover in ("portrayal.json", "bot_identity.json"):
        p = sub / leftover
        if p.exists():
            p.unlink()

    plugin = make_plugin(None, sub)
    plugin.cfg.bot_identity_file = sub / "bot_identity.json"
    plugin.identity = m.BotIdentityStore(plugin.cfg.bot_identity_file)
    plugin._avatar_downloader = _patch_download()
    return plugin


def test_switch_and_restore(tmp: Path):
    print("[切换 / 恢复 身份同步]")
    import portrayal_plugin.core.bot_identity as bi
    import portrayal_plugin.main as m
    from portrayal_plugin.core.model import UserProfile

    real_download = m.download_avatar_b64
    real_bi = bi.download_avatar_b64

    # ---- 场景一：备份原始资料 -> 切换 -> 还原
    plugin = _isolate(tmp, "case1")
    ctx = FakeStarContext()
    wire(plugin, ctx)
    plugin.db.set(UserProfile(user_id="123", nickname="小明", clone_prompt="小明的人格"))

    bot = FakeBot(nickname="真机器人", user_id="999")
    out = collect(
        plugin.switch_persona(
            FakeEvent("切换人格 @用户", [Plain("切换人格 "), At("123")], bot=bot)
        )
    )
    text = out[0][1]
    check("切换成功", "已将当前对话切换为【小明】" in text, text)
    check("昵称已改成群友", bot.nickname == "小明", bot.nickname)
    check(
        "头像已按群友 QQ 设置",
        ("set_qq_avatar", "base64://ZmFrZQ==") in bot.calls,
    )
    check("原始昵称已备份（全局）", plugin.identity.load().nickname == "真机器人")
    check("原始 QQ 已备份", plugin.identity.load().user_id == "999")
    check(
        "记录占用",
        plugin.identity.load().worn.get("aiocqhttp:GroupMessage:999") == "123",
    )
    check("人格已推入 AstrBot", ctx.persona_manager.updated[-1][1] == "小明的人格")

    # 再切另一个人格：不能把「小明」当成原始昵称
    plugin.db.set(UserProfile(user_id="456", nickname="小红", clone_prompt="小红的人格"))
    bot2 = FakeBot(nickname="小明", user_id="999")  # 当前昵称已经是克隆昵称
    out2 = collect(
        plugin.switch_persona(
            FakeEvent("切换人格 @用户", [Plain("切换人格 "), At("456")], bot=bot2)
        )
    )
    check("第二次切换后备份仍是真名", plugin.identity.load().nickname == "真机器人")
    check("第二次切换未误报", "⚠️" not in out2[0][1], out2[0][1])
    check("第二次昵称", bot2.nickname == "小红")

    # 还原
    bot3 = FakeBot(nickname="小红", user_id="999")
    out3 = collect(
        plugin.restore_persona(FakeEvent("恢复人格", [Plain("恢复人格")], bot=bot3))
    )
    text3 = out3[0][1]
    check("还原为真名", bot3.nickname == "真机器人", bot3.nickname)
    check("还原回执", "机器人昵称已还原为【真机器人】" in text3, text3)
    check("还原默认人格", ctx.conversation_manager.persona_calls[-1][1] == "默认人格")
    check("占用已清空", plugin.identity.load().worn == {})

    # ---- 场景二：备份丢失 + 当前昵称是克隆昵称 -> 必须告警，不能把克隆名当真名
    plugin2 = _isolate(tmp, "case2")
    ctx2 = FakeStarContext()
    wire(plugin2, ctx2)
    plugin2.db.set(UserProfile(user_id="123", nickname="小明", clone_prompt="小明的人格"))
    plugin2.identity.mark_worn(nickname="小明", user_id="123")

    bot4 = FakeBot(nickname="小明", user_id="999")
    out4 = collect(
        plugin2.switch_persona(
            FakeEvent("切换人格 @用户", [Plain("切换人格 "), At("123")], bot=bot4)
        )
    )
    check("备份丢失时告警", "无法自动还原" in out4[0][1], out4[0][1])
    check("没有把克隆名记成真名", plugin2.identity.load().nickname == "")

    # ---- 场景六：头像备份缺失时，还原不能拿 QQ 号地址去顶
    #（那一刻协议端的头像还是克隆头像，用 dst_uin URL 会把克隆头像设回去）
    import portrayal_plugin.main as m

    plugin6 = _isolate(tmp, "case6")
    ctx6 = FakeStarContext()
    wire(plugin6, ctx6)
    plugin6.identity.remember_original(nickname="真机器人", user_id="999")

    async def real_fetch(user_id, **kwargs):
        return "T1JJR0lOQUw="  # 模拟「重新下载到了机器人原图」

    plugin6._avatar_downloader = real_fetch
    bot8 = FakeBot(nickname="小明", user_id="999")
    out8 = collect(
        plugin6.restore_persona(FakeEvent("恢复人格", [Plain("恢复人格")], bot=bot8))
    )
    avatar_calls = [c[1] for c in bot8.calls if c[0] == "set_qq_avatar"]
    check("还原头像用重下的原图", avatar_calls == ["base64://T1JJR0lOQUw="], repr(avatar_calls))
    check("还原回执标明已还原头像", "头像已还原为机器人原图" in out8[0][1], out8[0][1])

    # ---- 场景七：重下也失败 -> 明确告知需要手动恢复，且不要用错误来源设置头像
    plugin7 = _isolate(tmp, "case7")
    ctx7 = FakeStarContext()
    wire(plugin7, ctx7)
    plugin7.identity.remember_original(nickname="真机器人", user_id="999")

    async def fail_fetch2(user_id, **kwargs):
        return ""

    plugin7._avatar_downloader = fail_fetch2
    bot9 = FakeBot(nickname="小明", user_id="999")
    out9 = collect(
        plugin7.restore_persona(FakeEvent("恢复人格", [Plain("恢复人格")], bot=bot9))
    )
    avatar_calls9 = [c[1] for c in bot9.calls if c[0] == "set_qq_avatar"]
    check("重下失败时不乱设头像", avatar_calls9 == [], repr(avatar_calls9))
    check("重下失败时明确提示", "头像可能仍需手动恢复" in out9[0][1], out9[0][1])

    # ---- 场景三：昵称没改成功（协议端拒绝）-> 回执要提示
    plugin3 = _isolate(tmp, "case3")
    ctx3 = FakeStarContext()
    wire(plugin3, ctx3)
    plugin3.db.set(UserProfile(user_id="123", nickname="小明", clone_prompt="人格"))
    bot5 = FakeBot(nickname="真机器人")
    bot5.nickname_after_set = "真机器人"  # 设了但没变
    out5 = collect(
        plugin3.switch_persona(
            FakeEvent("切换人格 @用户", [Plain("切换人格 "), At("123")], bot=bot5)
        )
    )
    check("昵称未生效有提示", "昵称可能未生效" in out5[0][1], out5[0][1])

    # ---- 场景四：头像设置抛异常 -> 提示但不中断
    plugin4 = _isolate(tmp, "case4")
    ctx4 = FakeStarContext()
    wire(plugin4, ctx4)
    plugin4.db.set(UserProfile(user_id="123", nickname="小明", clone_prompt="人格"))
    bot6 = FakeBot(nickname="真机器人")
    bot6.fail_avatar = True
    out6 = collect(
        plugin4.switch_persona(
            FakeEvent("切换人格 @用户", [Plain("切换人格 "), At("123")], bot=bot6)
        )
    )
    check("头像失败有提示", "头像" in out6[0][1] and "⚠️" in out6[0][1], out6[0][1])
    check("昵称仍然设置成功", bot6.nickname == "小明")

    # ---- 场景五：没有备份时还原要说明需手动处理
    plugin5 = _isolate(tmp, "case5")
    ctx5 = FakeStarContext()
    wire(plugin5, ctx5)
    out7 = collect(plugin5.restore_persona(FakeEvent("恢复人格", [Plain("恢复人格")])))
    check("无备份时给出指引", "没有机器人原始资料备份" in out7[0][1], out7[0][1])

    m.download_avatar_b64 = real_download
    bi.download_avatar_b64 = real_bi


def test_identity_commands(tmp: Path):
    print("[查看/记录 机器人身份]")
    import portrayal_plugin.core.bot_identity as bi
    import portrayal_plugin.main as m

    real_download = m.download_avatar_b64
    real_bi = bi.download_avatar_b64
    try:
        plugin = _isolate(tmp, "cmd")
        ctx = FakeStarContext()
        wire(plugin, ctx)

        bot = FakeBot(nickname="真机器人", user_id="999")
        out = collect(plugin.show_bot_identity(FakeEvent("查看机器人身份", [], bot=bot)))
        text = out[0][1]
        check("显示未备份", "（未备份）" in text, text)
        check("显示当前昵称", "当前协议端昵称：真机器人" in text, text)

        plugin.identity.remember_original(nickname="真机器人", user_id="999")
        plugin.identity.mark_worn(nickname="小明", user_id="123")
        bot2 = FakeBot(nickname="小明", user_id="999")
        out2 = collect(plugin.show_bot_identity(FakeEvent("查看机器人身份", [], bot=bot2)))
        check("识别出当前是克隆昵称", "当前昵称是克隆昵称" in out2[0][1], out2[0][1])

        # 记录当前资料
        bot3 = FakeBot(nickname="正版机器人", user_id="999")
        out3 = collect(
            plugin.remember_bot_identity(FakeEvent("还原机器人资料", [], bot=bot3))
        )
        check("记录当前资料", plugin.identity.load().nickname == "正版机器人", out3[0][1])
        check("记录后清空占用", plugin.identity.load().worn == {})

        # 当前是克隆昵称时拒绝记录
        plugin.identity.mark_worn(nickname="小红", user_id="456")
        bot4 = FakeBot(nickname="小红", user_id="999")
        out4 = collect(
            plugin.remember_bot_identity(FakeEvent("还原机器人资料", [], bot=bot4))
        )
        check("拒绝把克隆名记成真名", "克隆昵称" in out4[0][1], out4[0][1])
        check("真名未被覆盖", plugin.identity.load().nickname == "正版机器人")
    finally:
        m.download_avatar_b64 = real_download
        bi.download_avatar_b64 = real_bi


def test_sync_helpers(tmp: Path):
    print("[同步辅助]")
    plugin = make_plugin(None, tmp)

    async def fake_fetch(user_id, **kwargs):
        return "ZmFrZQ=="

    async def fail_fetch(user_id, **kwargs):
        return ""

    def has_call(bot_, value):
        return ("set_qq_avatar", value) in bot_.calls

    bot = FakeBot(nickname="机器人")
    check("空昵称被拒绝", asyncio.run(plugin._sync_qq_nickname(None, "   ")) != "")
    check(
        "正常设置昵称",
        asyncio.run(plugin._sync_qq_nickname(FakeEvent("x", [], bot=bot), "新名字")) == "",
    )
    check("昵称已生效", bot.nickname == "新名字")

    bot.fail_profile = True
    err = asyncio.run(plugin._sync_qq_nickname(FakeEvent("x", [], bot=bot), "再改"))
    check("设置失败返回错误", "设置昵称失败" in err)

    check("空头像被拒绝", asyncio.run(plugin._sync_qq_avatar(FakeEvent("x", []), "")) != "")

    # 数字 QQ 号：先下载再以 base64 上传
    bot2 = FakeBot()
    err2 = asyncio.run(
        plugin._sync_qq_avatar(
            FakeEvent("x", [], bot=bot2), "10001", downloader=fake_fetch
        )
    )
    check(
        "数字 QQ 号走下载+base64 上传",
        err2 == "" and has_call(bot2, "base64://ZmFrZQ=="),
        str([c[0] for c in bot2.calls]),
    )

    # http 地址：同样先自己下载
    bot3 = FakeBot()
    err3 = asyncio.run(
        plugin._sync_qq_avatar(
            FakeEvent("x", [], bot=bot3),
            "https://example.com/a.png",
            downloader=fake_fetch,
        )
    )
    check(
        "http 地址也先自己下载",
        err3 == "" and has_call(bot3, "base64://ZmFrZQ=="),
        repr(err3),
    )

    # base64 直接上传
    bot4 = FakeBot()
    err4 = asyncio.run(
        plugin._sync_qq_avatar(
            FakeEvent("x", [], bot=bot4), "base64://AAAA", downloader=fake_fetch
        )
    )
    check("base64 直接上传", err4 == "" and has_call(bot4, "base64://AAAA"))

    # 下载失败：数字 QQ 号要报错
    bot5 = FakeBot()
    err5 = asyncio.run(
        plugin._sync_qq_avatar(
            FakeEvent("x", [], bot=bot5), "10001", downloader=fail_fetch
        )
    )
    check("数字 QQ 号下载失败要报错", "头像下载失败" in err5, repr(err5))

    # 下载失败：http 地址退回让协议端自己拉
    bot6 = FakeBot()
    err6 = asyncio.run(
        plugin._sync_qq_avatar(
            FakeEvent("x", [], bot=bot6),
            "https://example.com/a.png",
            downloader=fail_fetch,
        )
    )
    check(
        "下载失败时退回让协议端拉取",
        err6 == "" and has_call(bot6, "https://example.com/a.png"),
        repr(err6),
    )

    # 上传失败要报错
    bot7 = FakeBot()
    bot7.fail_avatar = True
    err7 = asyncio.run(
        plugin._sync_qq_avatar(
            FakeEvent("x", [], bot=bot7), "base64://AAAA", downloader=fake_fetch
        )
    )
    check("上传失败返回错误", "设置头像失败" in err7, repr(err7))


def main():
    tmp = new_tmp("identity")
    test_sniff_and_urls()
    test_identity_model()
    test_store(tmp)
    test_download_avatar(tmp)
    test_switch_and_restore(tmp)
    test_identity_commands(tmp)
    test_sync_helpers(tmp)

    from test_features import FAILURES

    print()
    if FAILURES:
        print(f"FAILED {len(FAILURES)}:")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("ALL IDENTITY TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
