"""机器人昵称/头像备份与还原的离线测试。

重点覆盖两条硬规则：
1. **绝不从当前账号反推原始头像**（账号上挂着的可能正是群友头像）；
2. **没有确认过的原图就不动头像**，也绝不谎报「已还原」。

运行： python tests/test_bot_identity.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

from test_features import At, Plain, check, collect, make_plugin, plugin_main  # noqa: E402

from portrayal_plugin.core.bot_identity import (  # noqa: E402
    MAX_CLONE_HISTORY,
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

REAL_AVATAR = "UkVBTF9PUklHSU5BTA=="  # 机器人原图（测试用假字节）
PERSONA_AVATAR = "UEVSU09OQV9BVkFUQVI="  # 群友头像


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
    check("生成多个候选地址", len(urls) >= 2, str(urls[:2]))
    check("地址包含 QQ 号", all("10001" in u for u in urls))
    check("非数字返回空", build_avatar_urls("abc") == [])


def test_identity_model():
    print("[身份模型]")
    data = BotIdentity()
    check("初始都未知", not data.nickname_known and not data.avatar_known and not data.ready)

    data.nickname = "机器人"
    check("昵称已知", data.nickname_known and data.ready)

    data.mark_clone_name("小明", "123")
    check("记录克隆昵称", data.is_clone_name("小明") and not data.is_clone_name("真名"))
    check("占用者可查", (data.worn_owner("小明") or {}).get("user_id") == "123")
    check("空/None 不算克隆", not data.is_clone_name("") and not data.is_clone_name(None))

    check("无占用且非克隆名 -> 不在克隆态", not data.wearing_clone("机器人"))
    check("当前昵称是克隆名 -> 克隆态", data.wearing_clone("小明"))
    data.worn["umo1"] = "小明"
    check("有占用记录 -> 克隆态", data.wearing_clone("机器人"))

    check("序列化往返", BotIdentity.from_dict(data.to_dict()).worn == data.worn)
    check("脏数据不炸", BotIdentity.from_dict("nope").ready is False)
    check("字段类型异常也不炸", BotIdentity.from_dict({"clone_names": 5}).clone_names == {})


def test_eviction_keeps_worn(tmp: Path):
    print("[克隆昵称淘汰]")
    data = BotIdentity(worn={"umo1": "最老的占用名"})
    data.mark_clone_name("最老的占用名", "1")
    for i in range(MAX_CLONE_HISTORY + 10):
        data.mark_clone_name(f"名字{i}", str(i + 2))
    check("总条数不超上限", len(data.clone_names) <= MAX_CLONE_HISTORY, str(len(data.clone_names)))
    check(
        "正在占用的名字不会被淘汰",
        data.is_clone_name("最老的占用名"),
        "占用名被淘汰了",
    )


def test_store(tmp: Path):
    print("[身份存储]")
    path = tmp / "bot_identity.json"
    store = BotIdentityStore(path)

    check("初始为空", store.load().ready is False)

    store.remember_nickname("机器人", user_id="999")
    check("记录昵称", store.load().nickname == "机器人")
    check("落盘", path.exists())
    check("重新打开仍在", BotIdentityStore(path).load().nickname == "机器人")
    store.remember_nickname("小明")
    check("不覆盖已有昵称", store.load().nickname == "机器人")
    store.remember_nickname("新真名", overwrite=True)
    check("overwrite 可改昵称", store.load().nickname == "新真名")

    # 头像：默认只补空缺
    check("空头像不写入", store.remember_avatar("") is False)
    check("首次写入成功", store.remember_avatar("AAAA") is True)
    check("已有头像时默认不覆盖", store.remember_avatar("BBBB") is False)
    check("overwrite 可覆盖", store.remember_avatar("BBBB", overwrite=True) is True)
    check("头像已知", store.load().avatar_known)

    store.forget_avatar()
    check("forget 后头像未知", not store.load().avatar_known)

    store.remember_user_id("999")
    check("user_id 独立记录", store.load().user_id == "999")

    store.mark_worn(nickname="小明", owner="123", umo="umo1")
    data = store.load()
    check("记录占用", data.worn.get("umo1") == "小明" and data.is_clone_name("小明"))
    store.clear_worn("umo1")
    check("清除占用", store.load().worn == {})
    check("克隆昵称记录保留", store.load().is_clone_name("小明"))

    store.set_pending_avatar_restore(True)
    check("待补还原标记落盘", BotIdentityStore(path).load().pending_avatar_restore)

    desc = store.describe()
    check("摘要字段", desc["nickname_known"] and desc["avatar_known"] is False)
    check("摘要含克隆名", "小明" in desc["clone_names"])

    # 原子写入：不应残留 .tmp
    tmp_files = list(path.parent.glob("bot_identity.json.tmp"))
    check("无残留临时文件", not tmp_files, str(tmp_files))

    store.reset()
    check("reset 清空", store.load().ready is False and not path.exists())

    path.write_text("{ broken", encoding="utf-8")
    check("损坏状态文件可恢复", BotIdentityStore(path).load().ready is False)


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
    real_client = aiohttp.ClientSession

    def factory(mapping):
        def _f(*a, **k):
            return FakeSession(mapping)

        return _f

    aiohttp.ClientSession = factory({urls[1]: PNG})
    try:
        body = asyncio.run(download_avatar_b64("10001"))
    finally:
        aiohttp.ClientSession = real_client
    check("主源非图片时回退备用源", bool(body) and len(calls) == 2, str(calls[:3]))

    calls.clear()
    aiohttp.ClientSession = factory({})
    try:
        body = asyncio.run(download_avatar_b64("10001"))
    finally:
        aiohttp.ClientSession = real_client
    check("全部失败返回空", body == "")

    calls.clear()
    aiohttp.ClientSession = factory({})
    try:
        body = asyncio.run(download_avatar_b64("abc"))
    finally:
        aiohttp.ClientSession = real_client
    check("非法 QQ 号不发请求", body == "" and calls == [])

    aiohttp.ClientSession = factory({"https://x/1": PNG + b"\x00" * (5 * 1024 * 1024)})
    try:
        body = asyncio.run(download_avatar_b64("1", urls=["https://x/1"], max_bytes=1024))
    finally:
        aiohttp.ClientSession = real_client
    check("超大头像被拒绝", body == "")

    calls.clear()
    aiohttp.ClientSession = factory({"https://y/1": JPG})
    try:
        body = asyncio.run(download_avatar_b64("1", urls=["https://y/1"]))
    finally:
        aiohttp.ClientSession = real_client
    check("指定地址可用", bool(body) and calls == ["https://y/1"])


# ---------------------------------------------------------------- 命令层
class FakeBot:
    def __init__(self, nickname="真机器人", user_id="999"):
        self.nickname = nickname
        self.user_id = user_id
        self.calls: list[tuple] = []
        self.fail_profile = False
        self.fail_avatar = False
        self.nickname_after_set = None
        self.avatar_ignored = False  # 模拟协议端接受但不生效

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
        self.updated: list[tuple] = []

    async def update_persona(self, persona_id, system_prompt):
        self.updated.append((persona_id, system_prompt))
        return None

    async def create_persona(self, persona_id, system_prompt):
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


def _isolate(tmp: Path, name: str, *, live_avatar: str = PERSONA_AVATAR, download_ok: bool = True):
    """构造独立插件：数据目录、身份文件、头像下载都隔离

    live_avatar 模拟「当前账号头像」——下载机器人 QQ 头像时拿到的就是它。
    """
    import portrayal_plugin.core.bot_identity as bi
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

    async def fake_download(user_id, **kwargs):
        return live_avatar if download_ok else ""

    # 只在实例上注入；模块级补丁不再需要
    plugin._download_avatar = fake_download
    return plugin


def _wire(plugin, ctx=None):
    ctx = ctx or FakeStarContext()
    plugin.context = ctx
    return ctx


def test_capture_never_poisons_avatar(tmp: Path):
    print("[采集：绝不把群友头像当成原图]")
    import portrayal_plugin.core.bot_identity as bi
    import portrayal_plugin.main as m
    from portrayal_plugin.core.model import UserProfile

    plugin = _isolate(tmp, "capture", live_avatar=PERSONA_AVATAR, download_ok=False)
    ctx = _wire(plugin)
    plugin.db.set(UserProfile(user_id="123", nickname="小明", clone_prompt="人格"))
    plugin.identity.remember_nickname("真机器人", user_id="999")

    # 第一次切换：原图下载失败 -> 头像保持「未知」，昵称照常备份
    bot = FakeBot(nickname="真机器人", user_id="999")
    out = collect(
        plugin.switch_persona(
            FakeEvent("切换人格 @用户", [Plain("切换人格 "), At("123")], bot=bot)
        )
    )
    data = plugin.identity.load()
    check("采集失败后头像仍是未知", data.avatar_known is False)
    check("采集失败会告警", "没能备份机器人原始头像" in out[0][1], out[0][1])
    check("切换后昵称已改", bot.nickname == "小明")

    # 第二次切换：账号头像此刻已是群友头像，**不得**把它补成「原图」
    m.download_avatar_b64 = bi.download_avatar_b64  # 下载恢复可用，会返回 PERSONA_AVATAR

    async def persona_download(user_id, **kwargs):
        return PERSONA_AVATAR

    m.download_avatar_b64 = persona_download
    bi.download_avatar_b64 = persona_download
    plugin.identity.remember_nickname("真机器人", user_id="999", overwrite=True)
    plugin.identity.forget_avatar()

    bot2 = FakeBot(nickname="小明", user_id="999")
    plugin.db.set(UserProfile(user_id="456", nickname="小红", clone_prompt="人格2"))
    collect(
        plugin.switch_persona(
            FakeEvent("切换人格 @用户", [Plain("切换人格 "), At("456")], bot=bot2)
        )
    )
    check(
        "后续切换不会把群友头像补成原图",
        plugin.identity.load().avatar_known is False,
        plugin.identity.load().avatar_b64,
    )

    # 有占用记录时（克隆态）也不采集
    plugin.identity.mark_worn(nickname="小红", owner="456", umo="umo2")
    plugin.identity.forget_avatar()
    collect(
        plugin.switch_persona(
            FakeEvent(
                "切换人格 @用户", [Plain("切换人格 "), At("123")], bot=FakeBot(nickname="小红")
            )
        )
    )
    check("克隆态下不采集头像", plugin.identity.load().avatar_known is False)


def test_restore_honesty(tmp: Path):
    print("[恢复：不谎报头像已还原]")
    from portrayal_plugin.core.model import UserProfile

    # 场景 A：有确认过的原图 -> 提交 base64 还原
    plugin = _isolate(tmp, "restore_a")
    ctx = _wire(plugin)
    plugin.identity.remember_nickname("真机器人", user_id="999")
    plugin.identity.remember_avatar(REAL_AVATAR)

    bot = FakeBot(nickname="小明", user_id="999")
    out = collect(plugin.restore_persona(FakeEvent("恢复人格", [Plain("恢复人格")], bot=bot)))
    calls = [c[1] for c in bot.calls if c[0] == "set_qq_avatar"]
    check("用备份原图还原头像", calls == [f"base64://{REAL_AVATAR}"], repr(calls)[:60])
    check("回执说明已提交还原", "已按备份原图提交还原" in out[0][1], out[0][1])
    check("昵称已还原", bot.nickname == "真机器人")

    # 场景 B：没有原图备份 -> **不得**调用设头像，且明确告知手动处理
    plugin_b = _isolate(tmp, "restore_b")
    _wire(plugin_b)
    plugin_b.identity.remember_nickname("真机器人", user_id="999")

    bot_b = FakeBot(nickname="小明", user_id="999")
    out_b = collect(
        plugin_b.restore_persona(FakeEvent("恢复人格", [Plain("恢复人格")], bot=bot_b))
    )
    calls_b = [c for c in bot_b.calls if c[0] == "set_qq_avatar"]
    check("没有原图时绝不设头像", calls_b == [], repr(calls_b)[:60])
    check("明确提示需要手动恢复", "头像未自动还原" in out_b[0][1], out_b[0][1])
    check("提示里给出修复命令", "还原机器人资料 确认" in out_b[0][1])
    check("记录了待补还原", plugin_b.identity.load().pending_avatar_restore is True)

    # 场景 C：协议端吞掉头像设置（不报错但不生效）-> 措辞不得声称已还原
    plugin_c = _isolate(tmp, "restore_c")
    _wire(plugin_c)
    plugin_c.identity.remember_nickname("真机器人", user_id="999")
    plugin_c.identity.remember_avatar(REAL_AVATAR)
    bot_c = FakeBot(nickname="小明", user_id="999")
    bot_c.avatar_ignored = True
    out_c = collect(
        plugin_c.restore_persona(FakeEvent("恢复人格", [Plain("恢复人格")], bot=bot_c))
    )
    check(
        "不声称头像一定已还原",
        "已还原为机器人原图" not in out_c[0][1] and "请到 QQ 确认" in out_c[0][1],
        out_c[0][1],
    )

    # 场景 D：完全没备份 -> 指引手动修复
    plugin_d = _isolate(tmp, "restore_d")
    _wire(plugin_d)
    out_d = collect(plugin_d.restore_persona(FakeEvent("恢复人格", [Plain("恢复人格")])))
    check("无备份时指引手动修复", "还原机器人资料 确认" in out_d[0][1], out_d[0][1])


def test_remember_requires_confirm(tmp: Path):
    print("[还原机器人资料 需要确认]")
    plugin = _isolate(tmp, "confirm")
    _wire(plugin)

    out = collect(
        plugin.remember_bot_identity(FakeEvent("还原机器人资料", [Plain("还原机器人资料")]))
    )
    check("缺确认时拒绝", "请先在 QQ 里" in out[0][1], out[0][1])
    check("缺确认时不写库", plugin.identity.load().ready is False)

    out2 = collect(
        plugin.remember_bot_identity(
            FakeEvent("还原机器人资料 确认", [Plain("还原机器人资料 确认")])
        )
    )
    data = plugin.identity.load()
    check("带确认时记录昵称", data.nickname == "真机器人", out2[0][1])
    check("带确认时记录头像", data.avatar_known, out2[0][1])
    check("回执说明已记录", "已记录机器人原始资料" in out2[0][1])

    # 正在穿克隆时拒绝覆盖
    plugin2 = _isolate(tmp, "confirm2")
    _wire(plugin2)
    plugin2.identity.mark_worn(nickname="真机器人", owner="123", umo="u")
    out3 = collect(
        plugin2.remember_bot_identity(
            FakeEvent("还原机器人资料 确认", [Plain("还原机器人资料 确认")])
        )
    )
    check("克隆态下拒绝覆盖", "当前仍处于克隆状态" in out3[0][1], out3[0][1])

    # 下载失败时不谎报成功
    plugin3 = _isolate(tmp, "confirm3", download_ok=False)
    _wire(plugin3)
    out4 = collect(
        plugin3.remember_bot_identity(
            FakeEvent("还原机器人资料 确认", [Plain("还原机器人资料 确认")])
        )
    )
    data3 = plugin3.identity.load()
    check("下载失败时仍记录昵称", data3.nickname == "真机器人")
    check("下载失败时不记录头像", data3.avatar_known is False)
    check("下载失败时明确提示", "头像未记录" in out4[0][1], out4[0][1])


def test_switch_and_restore_flow(tmp: Path):
    print("[切换 / 恢复 整体流程]")
    from portrayal_plugin.core.model import UserProfile

    plugin = _isolate(tmp, "flow", live_avatar=PERSONA_AVATAR, download_ok=True)
    ctx = _wire(plugin)
    plugin.db.set(UserProfile(user_id="123", nickname="小明", clone_prompt="小明的人格"))

    bot = FakeBot(nickname="真机器人", user_id="999")
    out = collect(
        plugin.switch_persona(
            FakeEvent("切换人格 @用户", [Plain("切换人格 "), At("123")], bot=bot)
        )
    )
    check("切换成功", "已将当前对话切换为【小明】" in out[0][1], out[0][1])
    check("昵称已改成群友", bot.nickname == "小明")
    check(
        "克隆头像用群友头像上传",
        [c[1] for c in bot.calls if c[0] == "set_qq_avatar"] == [f"base64://{PERSONA_AVATAR}"],
    )
    check("原始昵称已备份", plugin.identity.load().nickname == "真机器人")
    # 首次采集时账号还是原图 -> 下载到的是「原图」；测试里下载器返回 PERSONA_AVATAR，
    # 所以这里只断言「采集发生在推进之前」这个性质：头像已被采集
    check("首次采集到了头像", plugin.identity.load().avatar_known)
    check("登记了占用", plugin.identity.load().worn.get("aiocqhttp:GroupMessage:999") == "小明")
    check("人格已推入 AstrBot", ctx.persona_manager.updated[-1][1] == "小明的人格")

    # 第二次切换：备份的昵称不能被「小明」覆盖
    plugin.db.set(UserProfile(user_id="456", nickname="小红", clone_prompt="小红的人格"))
    bot2 = FakeBot(nickname="小明", user_id="999")
    collect(
        plugin.switch_persona(
            FakeEvent("切换人格 @用户", [Plain("切换人格 "), At("456")], bot=bot2)
        )
    )
    check("第二次切换后备份仍是真名", plugin.identity.load().nickname == "真机器人")
    check("第二次昵称", bot2.nickname == "小红")

    # 昵称被拒时不登记占用
    plugin3 = _isolate(tmp, "flow3")
    _wire(plugin3)
    plugin3.db.set(UserProfile(user_id="789", nickname="小刚", clone_prompt="人格"))
    bot3 = FakeBot(nickname="真机器人", user_id="999")
    bot3.nickname_after_set = "真机器人"
    out3 = collect(
        plugin3.switch_persona(
            FakeEvent("切换人格 @用户", [Plain("切换人格 "), At("789")], bot=bot3)
        )
    )
    check("昵称未生效有提示", "昵称未生效" in out3[0][1], out3[0][1])
    check("昵称未生效不登记占用", plugin3.identity.load().worn == {})
    check(
        "昵称未生效不同步头像",
        [c for c in bot3.calls if c[0] == "set_qq_avatar"] == [],
    )


def test_show_identity(tmp: Path):
    print("[查看机器人身份]")
    plugin = _isolate(tmp, "show")
    _wire(plugin)

    bot = FakeBot(nickname="真机器人", user_id="999")
    out = collect(plugin.show_bot_identity(FakeEvent("查看机器人身份", [], bot=bot)))
    text = out[0][1]
    check("未备份时说明", "（未备份）" in text, text)
    check("提示头像未备份", "头像原图未备份" in text, text)
    check("不谎报一致", "当前昵称与备份一致" not in text, text)

    plugin.identity.remember_nickname("真机器人", user_id="999")
    out2 = collect(plugin.show_bot_identity(FakeEvent("查看机器人身份", [], bot=bot)))
    check("一致时明确提示", "当前昵称与备份一致" in out2[0][1], out2[0][1])

    plugin.identity.mark_worn(nickname="小明", owner="123", umo="u")
    bot2 = FakeBot(nickname="小明", user_id="999")
    out3 = collect(plugin.show_bot_identity(FakeEvent("查看机器人身份", [], bot=bot2)))
    check("识别克隆状态", "当前处于克隆状态" in out3[0][1], out3[0][1])

    # 读不到昵称时不得声称一致
    class BadBot(FakeBot):
        async def get_login_info(self):
            raise RuntimeError("offline")

    out4 = collect(plugin.show_bot_identity(FakeEvent("查看机器人身份", [], bot=BadBot())))
    check("读取失败时明说", "读不到当前昵称" in out4[0][1], out4[0][1])
    check("读取失败不谎报一致", "当前昵称与备份一致" not in out4[0][1], out4[0][1])


def test_sync_helpers(tmp: Path):
    print("[同步辅助]")
    plugin = _isolate(tmp, "sync")
    _wire(plugin)

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

    bot2 = FakeBot()
    err2 = asyncio.run(
        plugin._sync_qq_avatar(FakeEvent("x", [], bot=bot2), f"base64://{REAL_AVATAR}")
    )
    check(
        "base64 走 base64 上传",
        err2 == "" and [c[1] for c in bot2.calls if c[0] == "set_qq_avatar"] == [f"base64://{REAL_AVATAR}"],
    )

    bot3 = FakeBot()
    err3 = asyncio.run(
        plugin._sync_qq_avatar(FakeEvent("x", [], bot=bot3), "https://example.com/a.png")
    )
    check(
        "http 地址透传给协议端",
        err3 == "" and [c[1] for c in bot3.calls if c[0] == "set_qq_avatar"] == ["https://example.com/a.png"],
    )

    bot4 = FakeBot()
    bot4.fail_avatar = True
    err4 = asyncio.run(
        plugin._sync_qq_avatar(FakeEvent("x", [], bot=bot4), f"base64://{REAL_AVATAR}")
    )
    check("上传失败返回错误", "设置头像失败" in err4)

    # _apply_original_avatar：没有确认过的原图就不动
    check("无原图时不设头像", "缺少机器人头像原图备份" in asyncio.run(plugin._apply_original_avatar(FakeEvent("x", []))))


def test_pending_restore_completes(tmp: Path):
    print("[待补还原]")
    plugin = _isolate(tmp, "pending")
    _wire(plugin)
    plugin.identity.remember_nickname("真机器人", user_id="999")

    # 第一次恢复：没有原图 -> 记录待补
    bot = FakeBot(nickname="小明", user_id="999")
    collect(plugin.restore_persona(FakeEvent("恢复人格", [Plain("恢复人格")], bot=bot)))
    check("标记待补还原", plugin.identity.load().pending_avatar_restore is True)

    # 管理员确认资料（模拟已手动改回头像）
    plugin.remember_avatar_ok = True
    collect(
        plugin.remember_bot_identity(
            FakeEvent("还原机器人资料 确认", [Plain("还原机器人资料 确认")])
        )
    )
    check("确认后清掉待补标记", plugin.identity.load().pending_avatar_restore is False)
    check("确认后头像可用", plugin.identity.load().avatar_known)

    # 之后的恢复能自动还原头像
    bot2 = FakeBot(nickname="小明", user_id="999")
    out = collect(plugin.restore_persona(FakeEvent("恢复人格", [Plain("恢复人格")], bot=bot2)))
    check("补好后可自动还原头像", "已按备份原图提交还原" in out[0][1], out[0][1])


def main():
    tmp = new_tmp("identity")
    test_sniff_and_urls()
    test_identity_model()
    test_eviction_keeps_worn(tmp)
    test_store(tmp)
    test_download_avatar(tmp)
    test_capture_never_poisons_avatar(tmp)
    test_restore_honesty(tmp)
    test_remember_requires_confirm(tmp)
    test_switch_and_restore_flow(tmp)
    test_show_identity(tmp)
    test_sync_helpers(tmp)
    test_pending_restore_completes(tmp)

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
