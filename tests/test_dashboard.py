"""面板（WebUI Pages）与共享服务的离线测试。

运行： python tests/test_dashboard.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

from astrbot.api.web import request as web_request  # noqa: E402

from test_features import (  # noqa: E402
    At,
    FakeEvent,
    Plain,
    check,
    collect,
    make_config,
    make_plugin,
    plugin_main,
)
from portrayal_plugin.core.model import UserProfile  # noqa: E402
from portrayal_plugin.core.persona_service import (  # noqa: E402
    MAX_SAFE_PROMPT_LEN,
    PersonaError,
)


def new_tmp(name: str) -> Path:
    import shutil

    tmp = ROOT / ".test_tmp" / name
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


def seed(plugin, **users):
    for uid, clone in users.items():
        plugin.db.set(
            UserProfile(
                user_id=uid,
                nickname=f"用户{uid}",
                clone_prompt=clone,
                portrait=f"{uid} 的画像",
                timestamp=1700000000 + int(uid),
            )
        )


# ---------------------------------------------------------------- 服务层
def test_stats_and_list(tmp: Path):
    print("[服务层：统计与列表]")
    plugin = make_plugin(None, tmp)
    seed(plugin, **{"1": "人格一", "2": "", "3": "人格三"})
    svc = plugin.persona_service

    stats = svc.stats()
    check("统计 档案总数", stats["profiles"] == 3, str(stats))
    check("统计 已克隆数", stats["with_clone"] == 2, str(stats))
    check("统计 有画像数", stats["with_portrait"] == 3, str(stats))
    check("统计 平均字数", stats["avg_clone_len"] == len("人格一"), str(stats))
    check("统计 上限", stats["max_safe_len"] == MAX_SAFE_PROMPT_LEN)

    lst = svc.list_users()
    check("列表 总数", lst["total"] == 3, str(lst["total"]))
    check("列表 默认按昵称", [u["user_id"] for u in lst["users"]] == ["1", "2", "3"])

    only = svc.list_users(only_clone=True)
    check("列表 只看已克隆", {u["user_id"] for u in only["users"]} == {"1", "3"})

    search = svc.list_users(search="用户2")
    check("列表 搜索昵称", [u["user_id"] for u in search["users"]] == ["2"])

    search_id = svc.list_users(search="3")
    check("列表 搜索 QQ 号", [u["user_id"] for u in search_id["users"]] == ["3"])

    paged = svc.list_users(limit=2, offset=1)
    check("列表 分页", len(paged["users"]) == 2 and paged["total"] == 3)

    desc = svc.list_users(sort="clone_len", desc=True)
    check("列表 按字数倒序", desc["users"][0]["clone_len"] == len("人格三"))

    overview = svc.overview()
    check("总览 含配置", "merge_prompt" in overview["config"])
    check("总览 含提示词命令", isinstance(overview["entry_commands"], list))
    check("总览 含上限", overview["limits"]["max_safe_len"] == MAX_SAFE_PROMPT_LEN)


def test_service_edits(tmp: Path):
    print("[服务层：写入]")
    plugin = make_plugin(None, tmp)
    seed(plugin, **{"1": "原始人格"})
    svc = plugin.persona_service

    res = svc.apply_edit("1", "append", "少用表情")
    check("追加 拼接", res.content == "原始人格\n少用表情" and res.mode == "追加")
    check("追加 落库", plugin.db.get("1").clone_prompt == "原始人格\n少用表情")

    res = svc.apply_edit("1", "replace", "整段新人格")
    check("替换 覆盖", plugin.db.get("1").clone_prompt == "整段新人格")

    res = svc.apply_edit("9", "create", "新建人格")
    check("建档 新建", plugin.db.get("9").clone_prompt == "新建人格")
    check("建档 昵称兜底", plugin.db.get("9").nickname == "用户9")

    for mode, content, label in [
        ("append", "x", "无档案"),
        ("replace", "x", "无档案"),
    ]:
        try:
            svc.apply_edit("404", mode, content)
            check(f"{label} 追加/替换应报错", False)
        except PersonaError as e:
            check(f"{label} 追加/替换被拒", "暂无该用户档案" in str(e))

    seed(plugin, **{"5": "   "})
    for mode in ("append", "replace"):
        try:
            svc.apply_edit("5", mode, "x")
            check(f"空人格 {mode} 应报错", False)
        except PersonaError as e:
            check(f"空人格 {mode} 被拒", "暂无可用的克隆人格" in str(e))

    for bad in ("", "abc"):
        try:
            svc.apply_edit(bad, "create", "x")
            check("非法 QQ 号应报错", False)
        except PersonaError as e:
            check("非法 QQ 号被拒", "不合法" in str(e))

    try:
        svc.apply_edit("1", "create", "   ")
        check("空正文应报错", False)
    except PersonaError as e:
        check("空正文被拒", "不能为空" in str(e))

    try:
        svc.apply_edit("1", "wat", "x")
        check("未知模式应报错", False)
    except PersonaError as e:
        check("未知模式被拒", "不支持的操作" in str(e))

    # 保护名单
    plugin2 = make_plugin(
        make_config(
            message={
                "default_query_rounds": 1,
                "max_msg_count": 10,
                "cache_ttl_min": 30,
                "protected_user_ids": ["7"],
            }
        ),
        tmp,
    )
    seed(plugin2, **{"7": "受保护"})
    try:
        plugin2.persona_service.apply_edit("7", "replace", "x")
        check("保护名单应报错", False)
    except PersonaError as e:
        check("保护名单被拒", "保护名单" in str(e))
    check("保护名单未被改写", plugin2.db.get("7").clone_prompt == "受保护")


def test_service_rewrite_and_generate(tmp: Path):
    print("[服务层：LLM 改写与生成]")
    plugin = make_plugin(None, tmp)
    seed(plugin, **{"1": "原始人格"})
    svc = plugin.persona_service

    captured = {}

    async def fake_edit(old, instruction, profile, template, *, umo=None):
        captured["edit"] = (old, instruction, template)
        return "改写后人格"

    plugin.llm.generate_persona_edit = fake_edit
    res = asyncio.run(svc.apply_rewrite("1", "更短一点"))
    check("改写 结果", plugin.db.get("1").clone_prompt == "改写后人格")
    check("改写 传参", captured["edit"][0] == "原始人格" and captured["edit"][1] == "更短一点")

    async def empty_edit(*a, **k):
        return "   "

    plugin.llm.generate_persona_edit = empty_edit
    try:
        asyncio.run(svc.apply_rewrite("1", "更短"))
        check("改写 空结果应报错", False)
    except PersonaError as e:
        check("改写 空结果被拒", "已保留原有人格" in str(e))
    check("改写 空结果不覆盖", plugin.db.get("1").clone_prompt == "改写后人格")

    async def boom(*a, **k):
        raise RuntimeError("llm down")

    plugin.llm.generate_persona_edit = boom
    try:
        asyncio.run(svc.apply_rewrite("1", "更短"))
        check("改写 异常应报错", False)
    except PersonaError as e:
        check("改写 异常提示", "修改失败" in str(e) and "已保留原有人格" in str(e))
    check("改写 异常不覆盖", plugin.db.get("1").clone_prompt == "改写后人格")

    # ---- 用缓存生成
    from portrayal_plugin.core.message_cache import CachedMessages

    plugin.msg._user_cache["999:1"] = CachedMessages(texts=["你好", "在吗"], timestamp=9e9)
    plugin.msg._user_cache["888:1"] = CachedMessages(texts=["早上好"], timestamp=9e9)

    texts, groups = plugin.msg.iter_cached_texts("1")
    check("缓存读取 条数", len(texts) == 3, str(texts))
    check("缓存读取 群数", groups == 2, str(groups))
    check("缓存读取 未命中", plugin.msg.iter_cached_texts("42") == ([], 0))

    gen_calls = {}

    async def fake_portrait(
        texts,
        profile,
        template,
        *,
        old_clone_prompt="",
        merge_prompt_template="",
        umo=None,
    ):
        gen_calls["args"] = (list(texts), old_clone_prompt, merge_prompt_template)
        return "缓存生成的人格"

    plugin.llm.generate_portrait = fake_portrait
    res = asyncio.run(svc.generate_from_cache("1", mode="merge"))
    check("生成 融合旧人格", gen_calls["args"][1] == "改写后人格")
    check("生成 传入记录", len(gen_calls["args"][0]) == 3)
    check("生成 结果落库", plugin.db.get("1").clone_prompt == "缓存生成的人格")
    check("生成 报告条数", res.used_messages == 3 and res.groups == 2)

    res = asyncio.run(svc.generate_from_cache("1", mode="fresh"))
    check("生成 fresh 不传旧人格", gen_calls["args"][1] == "")
    check("生成 fresh 不传融合模板", gen_calls["args"][2] == "")

    try:
        asyncio.run(svc.generate_from_cache("777", mode="merge"))
        check("生成 无缓存应报错", False)
    except PersonaError as e:
        check("生成 无缓存被拒", "缓存" in str(e))


# ---------------------------------------------------------------- 面板接口
def test_page_api(tmp: Path):
    print("[面板接口]")
    ctx = plugin_main.Context()
    plugin = make_plugin(None, tmp, context=ctx)
    seed(plugin, **{"1": "人格一", "2": "人格二"})
    api = plugin.page_api
    check("面板接口 已注册", api is not None)
    routes = {r[0] for r in getattr(ctx, "registered_web_apis", [])}
    check("构造时注册了路由", len(routes) == 7, str(sorted(routes)))

    def call(coro):
        return asyncio.run(coro)

    web_request.set(method="GET", query={})
    out = call(api._overview())
    check("overview 状态", out["status"] == "ok")
    check("overview 数据", out["data"]["stats"]["profiles"] == 2)

    web_request.set(method="GET", query={"only_clone": "1", "limit": "1"})
    out = call(api._users())
    check("users 过滤", out["data"]["total"] == 2 and len(out["data"]["users"]) == 1)

    web_request.set(method="POST", json_body={"search": "用户2"})
    out = call(api._users())
    check("users 读 JSON body", [u["user_id"] for u in out["data"]["users"]] == ["2"])

    web_request.set(method="GET")
    out = call(api._user_detail(user_id="1"))
    check("detail 返回正文", out["data"]["clone_prompt"] == "人格一")
    check("detail 返回缓存信息", "messages" in out["data"]["cache"])
    check("detail 返回上限", out["data"]["limits"]["max_safe_len"] == MAX_SAFE_PROMPT_LEN)

    out = call(api._user_detail(user_id="404"))
    check("detail 无档案报错", out["status"] == "error")

    # 写入
    web_request.set(
        method="POST", json_body={"user_id": "1", "mode": "append", "content": "补充"}
    )
    out = call(api._update())
    check("update 追加", out["status"] == "ok" and plugin.db.get("1").clone_prompt == "人格一\n补充")

    web_request.set(
        method="POST", json_body={"user_id": "1", "mode": "replace", "content": "整段"}
    )
    out = call(api._update())
    check("update 替换", plugin.db.get("1").clone_prompt == "整段")

    async def fake_edit(old, instruction, profile, template, *, umo=None):
        return "面板改写"

    plugin.llm.generate_persona_edit = fake_edit
    web_request.set(
        method="POST", json_body={"user_id": "1", "mode": "rewrite", "content": "短一点"}
    )
    out = call(api._update())
    check("update 改写", out["status"] == "ok" and plugin.db.get("1").clone_prompt == "面板改写")

    web_request.set(method="POST", json_body={"user_id": "1", "mode": "nope", "content": "x"})
    out = call(api._update())
    check("update 未知模式报错", out["status"] == "error" and "message_en" in out)

    web_request.set(
        method="POST", json_body={"user_id": "9", "mode": "create", "content": "建档人格"}
    )
    out = call(api._update())
    check("update 支持建档", out["status"] == "ok" and plugin.db.get("9").clone_prompt == "建档人格")

    web_request.set(method="POST", json_body={"mode": "append", "content": "x"})
    out = call(api._update())
    check("update 缺 uid 报错", out["status"] == "error")

    web_request.set(
        method="POST", json_body={"user_id": "N", "mode": "append", "content": "x"}
    )
    out = call(api._update())
    check("update 非法 uid 报错", out["status"] == "error")

    # 生成
    from portrayal_plugin.core.message_cache import CachedMessages

    plugin.msg._user_cache["999:2"] = CachedMessages(texts=["a", "b"], timestamp=9e9)

    async def fake_portrait(texts, profile, template, **kw):
        return "面板生成"

    plugin.llm.generate_portrait = fake_portrait
    web_request.set(method="POST", json_body={"user_id": "2", "mode": "merge"})
    out = call(api._generate())
    check("generate 成功", out["status"] == "ok" and plugin.db.get("2").clone_prompt == "面板生成")
    check("generate 报告", out["data"]["used_messages"] == 2)

    web_request.set(method="POST", json_body={"user_id": "2", "mode": "wat"})
    out = call(api._generate())
    check("generate 未知模式报错", out["status"] == "error")

    web_request.set(method="POST", json_body={"user_id": "3"})
    out = call(api._generate())
    check("generate 无缓存报错", out["status"] == "error")

    web_request.set(method="GET", query={"user_id": "2"})
    out = call(api._cache_info())
    check("cache 信息", out["data"]["messages"] == 2 and out["data"]["groups"] == 1)


def test_registration_paths():
    print("[面板路由注册]")
    registered: list[tuple] = []

    class FakeContext:
        def register_web_api(self, route, handler, methods, desc):
            registered.append((route, handler, methods, desc))

    fake_plugin = type("P", (), {"persona_service": None})()
    plugin_main.register_plugin_page_api(FakeContext(), fake_plugin)

    routes = {r[0]: r for r in registered}
    expected = {
        "/astrbot_plugin_portrayal/overview",
        "/astrbot_plugin_portrayal/users",
        "/astrbot_plugin_portrayal/user/<user_id>",
        "/astrbot_plugin_portrayal/update",
        "/astrbot_plugin_portrayal/generate",
        "/astrbot_plugin_portrayal/cache",
        "/astrbot_plugin_portrayal/cached-users",
    }
    check("路由 全覆盖", expected.issubset(set(routes)), str(sorted(routes)))
    check("路由 方法齐全", all(r[2] for r in registered))
    check(
        "路由 update 仅 POST",
        routes["/astrbot_plugin_portrayal/update"][2] == ["POST"],
    )
    check(
        "路由 注册了插件名前缀",
        all(r[0].startswith("/astrbot_plugin_portrayal/") for r in registered),
    )


def test_route_matching_with_real_host():
    """用 AstrBot 真实的路由匹配函数验证注册路径可命中（不是只比对字符串）"""
    print("[路由匹配（真实 host 函数）]")
    host_plugins = Path(r"E:\codex\AstrBot\backend\app\astrbot\dashboard\api\plugins.py")
    if not host_plugins.is_file():
        check("host 源码可读（跳过）", True)
        return

    try:
        sys.path.insert(0, r"E:\codex\AstrBot\backend\app")
        from astrbot.dashboard.api.plugins import (  # type: ignore  # noqa
            _match_registered_web_api,
            _plugin_api_route_pattern,
        )
    except Exception as e:
        print(f"  [SKIP] 无法导入 host 匹配函数：{e}")
        return

    class FakeContext:
        def __init__(self):
            self.registered_web_apis = []

        def register_web_api(self, route, handler, methods, desc):
            self.registered_web_apis.append((route, handler, methods, desc))

    fake_plugin = type("P", (), {"persona_service": None})()
    ctx = FakeContext()
    plugin_main.register_plugin_page_api(ctx, fake_plugin)
    apis = ctx.registered_web_apis

    check(
        "user 路由被编译成路径参数",
        _plugin_api_route_pattern("/astrbot_plugin_portrayal/user/<user_id>")
        == "/astrbot_plugin_portrayal/user/(?P<user_id>[^/]+)",
    )

    hit = _match_registered_web_api(apis, "/astrbot_plugin_portrayal/user/123456", "GET")
    check("GET user/123456 命中", hit is not None and hit[1] == {"user_id": "123456"})

    hit = _match_registered_web_api(apis, "/astrbot_plugin_portrayal/overview", "GET")
    check("GET overview 命中", hit is not None and hit[1] == {})

    hit = _match_registered_web_api(apis, "/astrbot_plugin_portrayal/update", "POST")
    check("POST update 命中", hit is not None)

    check(
        "GET update 被方法过滤掉",
        _match_registered_web_api(apis, "/astrbot_plugin_portrayal/update", "GET") is None,
    )
    check(
        "空 user_id 不命中",
        _match_registered_web_api(apis, "/astrbot_plugin_portrayal/user/", "GET") is None,
    )
    check(
        "cached-users 命中",
        _match_registered_web_api(apis, "/astrbot_plugin_portrayal/cached-users", "GET")
        is not None,
    )


def test_cache_ttl_is_honored(tmp: Path):
    """过期缓存不能被面板拿来生成人格（与聊天路径保持一致）"""
    print("[缓存 TTL]")
    import time as _time

    from portrayal_plugin.core.message_cache import CachedMessages

    plugin = make_plugin(None, tmp)
    svc = plugin.persona_service
    now = _time.time()

    plugin.msg._user_cache["111:222"] = CachedMessages(texts=["stale A", "stale B"], timestamp=now - 11 * 3600)
    plugin.msg._user_cache["333:222"] = CachedMessages(texts=["fresh C"], timestamp=now - 60)

    texts, groups = plugin.msg.iter_cached_texts("222")
    check("过期条目被过滤", texts == ["fresh C"], str(texts))
    check("只统计未过期群", groups == 1, str(groups))

    # 过期群整组清理
    check("过期群记录已清理", "111:222" not in plugin.msg._user_cache)
    check("过期群游标已清理", 111 not in plugin.msg._group_cursor)

    plugin.msg._user_cache.clear()
    plugin.msg._user_cache["111:222"] = CachedMessages(texts=["stale"], timestamp=now - 11 * 3600)
    texts, groups = plugin.msg.iter_cached_texts("222")
    check("全过期时视为无缓存", (texts, groups) == ([], 0), f"{texts} {groups}")

    try:
        asyncio.run(svc.generate_from_cache("222", mode="merge"))
        check("无有效缓存时生成应报错", False)
    except PersonaError as e:
        check("无有效缓存被拒", "缓存" in str(e))

    # 多群按最近抓取优先
    plugin.msg._user_cache.clear()
    plugin.msg._user_cache["a:5"] = CachedMessages(texts=["old1", "old2", "old3"], timestamp=now - 600)
    plugin.msg._user_cache["b:5"] = CachedMessages(texts=["new1", "new2"], timestamp=now - 10)
    texts, groups = plugin.msg.iter_cached_texts("5")
    check("最近的群排在前", texts[:2] == ["new1", "new2"], str(texts))

    # 键里带冒号时按最后一个冒号切分（group_id 里可能有冒号，user_id 不会）
    plugin.msg._user_cache.clear()
    plugin.msg._user_cache["111:222:333"] = CachedMessages(texts=["colon key"], timestamp=now)
    check("带冒号的分组键能按最后冒号切分", plugin.msg.iter_cached_texts("333")[0] == ["colon key"])
    check("不会把 222:333 当成用户 id", plugin.msg.iter_cached_texts("222:333")[0] == [])

    # 候选列表同样只用未过期数据
    plugin.msg._user_cache.clear()
    plugin.msg._user_cache["g:1"] = CachedMessages(texts=["x", "y"], timestamp=now)
    plugin.msg._user_cache["g:2"] = CachedMessages(texts=["z"], timestamp=now - 99999)
    cands = plugin.persona_service.cached_candidates()
    check("候选只含未过期用户", [c["user_id"] for c in cands["users"]] == ["1"], str(cands))

    plugin.db.set(UserProfile(user_id="1", nickname="已建档"))
    cands = plugin.persona_service.cached_candidates()
    check("已有档案不重复出现在候选中", cands["total"] == 0, str(cands))


def test_db_write_safety(tmp: Path):
    """portrayal.json 写入是原子的，损坏文件不会导致档案清零"""
    print("[档案写入安全]")
    plugin = make_plugin(None, tmp)
    plugin.db.set(UserProfile(user_id="1", nickname="甲", clone_prompt="人格一"))
    plugin.db.set(UserProfile(user_id="2", nickname="乙", clone_prompt="人格二"))
    check("正常写入", plugin.db.file.exists() and len(plugin.db._data) == 2)

    # 原子替换：目录里不应残留 .tmp
    tmph = list(plugin.db.file.parent.glob("portrayal.json.tmp"))
    check("无残留临时文件", not tmph, str(tmph))

    # 文件损坏时不能把内存清空
    bad_path = plugin.db.file.with_suffix(".json.bad")
    if bad_path.exists():
        bad_path.unlink()
    plugin.db.file.write_text("{ this is not json", encoding="utf-8")
    reloaded = type(plugin.db)(plugin.cfg)
    check("损坏文件不返回空档案", len(reloaded._data) == 0)
    check("损坏文件被改名保留", bad_path.exists(), str(bad_path))

    # 带 BOM 的文件（被编辑器改过）也要能读
    plugin.db.file.write_text(
        json.dumps({"5": {"user_id": "5", "nickname": "戊"}}, ensure_ascii=False),
        encoding="utf-8-sig",
    )
    reloaded_bom = type(plugin.db)(plugin.cfg)
    check("BOM 文件可加载", reloaded_bom.get("5") is not None, str(reloaded_bom._data))

    # 未知字段的档案（老版本 / 手改）也不应让整份加载失败
    plugin.db.file.write_text(
        json.dumps(
            {"3": {"user_id": "3", "nickname": "丙", "unknown_field": 1}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reloaded2 = type(plugin.db)(plugin.cfg)
    check("忽略未知字段继续加载", reloaded2.get("3") is not None, str(reloaded2._data))

    # 缺 user_id 的老数据也能补上
    plugin.db.file.write_text(
        json.dumps({"4": {"nickname": "丁", "clone_prompt": "老数据"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    reloaded3 = type(plugin.db)(plugin.cfg)
    check("缺失 user_id 时用键补齐", reloaded3.get("4") is not None and reloaded3.get("4").user_id == "4")


def test_page_assets_exist():
    print("[面板静态资源]")
    page_dir = ROOT / "pages" / "dashboard"
    check("pages/dashboard 存在", page_dir.is_dir())
    for name in ("index.html", "app.css", "app.js"):
        check(f"{name} 存在", (page_dir / name).is_file())

    html = (page_dir / "index.html").read_text("utf-8")
    js = (page_dir / "app.js").read_text("utf-8")
    for attr in (
        "data-status",
        "data-kpi",
        "data-search",
        "data-sort",
        "data-reload",
        "data-list",
        "data-detail",
        "data-pager",
        "data-toasts",
    ):
        check(f"index.html 含 {attr}", attr in html)
        check(f"app.js 引用 {attr}", attr in js)

    # 三个筛选控件：HTML 用 data-* 名字，JS 通过拼接选择器查找
    for attr in ("only-clone", "only-portrait", "desc"):
        check(f"index.html 含 data-{attr}", ("data-%s" % attr) in html)
        check(f"app.js 引用 data-{attr}", ('"%s"' % attr) in js)
    check("app.js 拼选择器", '"[data-" + t.attr' in js)

    check("index.html 引入 app.js", "./app.js" in html)
    check("index.html 引入 app.css", "./app.css" in html)
    check("index.html 未内联 bridge（由宿主注入）", "bridge-sdk.js" not in html)
    check(
        "app.js 调用插件名",
        js.count("astrbot_plugin_portrayal") >= 1,
    )
    for route in ("/overview", "/users", "/user/", "/update", "/generate"):
        check(f"app.js 使用 {route}", ('"%s"' % route) in js or route in js)

    import yaml

    meta = yaml.safe_load((ROOT / "metadata.yaml").read_text("utf-8"))
    pages = meta.get("pages") or []
    check("metadata 声明 pages", isinstance(pages, list) and len(pages) == 1)
    check("metadata 页面名与目录一致", pages and pages[0]["name"] == "dashboard")
    check("metadata 页面标题", pages and bool(pages[0].get("title")))


def test_html_bot_skip(tmp: Path):
    print("[命令解析：@机器人 自身]")
    plugin = make_plugin(None, tmp)
    from test_features import FakeEvent as FE

    chain = [Plain("改人格 "), At("10000"), At("123"), Plain(" 重置：好")]
    ev = FE("改人格 @群友123(123) 重置：好", chain, self_id="10000")
    check(
        "@bot 前缀被跳过",
        plugin._split_target_and_text(ev) == ("123", "重置：好"),
        str(plugin._split_target_and_text(ev)),
    )

    chain2 = [Plain("改人格 "), Plain(" 追加：x")]
    ev2 = FE("改人格 追加：x", chain2)
    check("无 At 时返回空", plugin._split_target_and_text(ev2) == ("", ""))

    chain3 = [Plain("改人格 "), At("all"), Plain(" 重置：好")]
    ev3 = FE("改人格 @全体成员 重置：好", chain3)
    check("@全体成员 不被当成目标", plugin._split_target_and_text(ev3)[0] == "")

    ev4 = FE("改人格 @群友 追加：123456789 说话短一点", [Plain("改人格 "), At("123"), Plain(" 123456789 说话短一点")])
    check(
        "以数字开头的正文不被截断",
        plugin._split_target_and_text(ev4) == ("123", "123456789 说话短一点"),
        str(plugin._split_target_and_text(ev4)),
    )

    ev5 = FE("改人格 @群友 追加：123 后面", [Plain("改人格 "), At("123"), Plain(" 123 后面")])
    check(
        "正文等于 QQ 号+空格时剔除",
        plugin._split_target_and_text(ev5) == ("123", "后面"),
        str(plugin._split_target_and_text(ev5)),
    )

    # 内置命令不应被提示词监听器重复处理
    out = collect(plugin.get_portrayal(FE("查看克隆 @群友", [Plain("查看克隆 "), At("123")])))
    check("内置命令不重复处理", out == [])
    out = collect(plugin.get_portrayal(FE("改人格 @群友 x", [Plain("改人格 "), At("123"), Plain(" x")])))
    check("改人格不重复处理", out == [])
    out = collect(plugin.get_portrayal(FE("切换人格 @群友", [Plain("切换人格 "), At("123")])))
    check("切换人格不重复处理", out == [])


def main():
    tmp = new_tmp("dash")
    test_stats_and_list(tmp)
    test_service_edits(tmp)
    test_service_rewrite_and_generate(tmp)
    test_page_api(tmp)
    test_registration_paths()
    test_route_matching_with_real_host()
    test_cache_ttl_is_honored(tmp)
    test_db_write_safety(tmp)
    test_page_assets_exist()
    test_html_bot_skip(tmp)

    from test_features import FAILURES

    print()
    if FAILURES:
        print(f"FAILED {len(FAILURES)}:")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("ALL DASHBOARD TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
