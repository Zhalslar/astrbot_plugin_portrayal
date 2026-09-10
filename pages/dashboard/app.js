/* 人物画像 · 人格面板
 * 纯 DOM 实现，无外部依赖；通过 AstrBot plugin page bridge 调用插件后端。
 * 后端路由见 plugin_api.py：overview / users / user/<uid> / update / generate / cache
 */
(function () {
  "use strict";

  var PLUGIN = "astrbot_plugin_portrayal";
  var PAGE_SIZE = 50;

  // ---------------------------------------------------------------- bridge
  function bridge() {
    var api = window.AstrBotPluginPage;
    if (!api || typeof api.apiGet !== "function") {
      throw new Error("未检测到面板桥接（AstrBotPluginPage），请从插件详情页打开本面板");
    }
    return api;
  }

  // 面板可能因为平台原因丢失中文错误文案，补一句 ASCII 兜底
  function errText(err, fallback) {
    var raw = (err && err.message) || "";
    var ascii = raw.replace(/[^\x20-\x7E]/g, "").trim();
    if (!raw) return fallback;
    if (ascii.length < 8) return fallback + (raw ? "（" + raw + "）" : "");
    return raw;
  }

  var api = {
    overview: function () {
      return bridge().apiGet(PLUGIN + "/overview").then(unwrap("读取总览失败"));
    },
    users: function (params) {
      var q = {};
      Object.keys(params || {}).forEach(function (k) {
        var v = params[k];
        if (v === "" || v === null || v === undefined || v === false) return;
        q[k] = v === true ? "1" : String(v);
      });
      return bridge().apiGet(PLUGIN + "/users", q).then(unwrap("读取列表失败"));
    },
    user: function (uid) {
      return bridge().apiGet(PLUGIN + "/user/" + encodeURIComponent(uid)).then(unwrap("读取档案失败"));
    },
    update: function (uid, mode, content) {
      return bridge()
        .apiPost(PLUGIN + "/update", { user_id: uid, mode: mode, content: content })
        .then(unwrap("保存失败"));
    },
    generate: function (uid, mode) {
      return bridge()
        .apiPost(PLUGIN + "/generate", { user_id: uid, mode: mode })
        .then(unwrap("生成失败"));
    },
    cachedUsers: function () {
      return bridge().apiGet(PLUGIN + "/cached-users").then(unwrap("读取缓存候选失败"));
    },
  };

  // 后端统一返回 { status, message, data }；出错时抛中文提示
  function unwrap(fallback) {
    return function (res) {
      if (res && res.status === "error") {
        throw new Error(res.message || fallback);
      }
      return res && Object.prototype.hasOwnProperty.call(res, "data") ? res.data : res;
    };
  }

  // ---------------------------------------------------------------- helpers
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === false || v === null || v === undefined) return;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.slice(0, 2) === "on") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : String(v));
    });
    (children || []).forEach(function (c) {
      if (c === null || c === undefined) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function fmtTime(ts) {
    if (!ts) return "—";
    try {
      return new Date(ts * 1000).toLocaleString();
    } catch (e) {
      return String(ts);
    }
  }

  function copyText(text) {
    if (!text) return Promise.reject(new Error("没有可复制的内容"));

    function legacyCopy() {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.top = "-1000px";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      ta.setSelectionRange(0, ta.value.length);
      var ok = false;
      try {
        ok = document.execCommand("copy");
      } catch (e) {
        ok = false;
      }
      document.body.removeChild(ta);
      return ok;
    }

    // iframe 里 clipboard API 常被权限策略拒绝，失败后必须真正退回复制方案
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).catch(function () {
        if (legacyCopy()) return;
        throw new Error("浏览器拒绝了复制权限，请手动选中复制");
      });
    }
    if (legacyCopy()) return Promise.resolve();
    return Promise.reject(new Error("当前环境不支持自动复制，请手动选中复制"));
  }

  // ---------------------------------------------------------------- app
  var App = function () {
    this.toastSeq = 0;
    this.el = {
      status: document.querySelector("[data-status]"),
      kpi: document.querySelector("[data-kpi]"),
      list: document.querySelector("[data-list]"),
      detail: document.querySelector("[data-detail]"),
      pager: document.querySelector("[data-pager]"),
      toasts: document.querySelector("[data-toasts]"),
      cached: document.querySelector("[data-cached]"),
      notice: document.querySelector("[data-notice]"),
    };
    this.state = {
      limits: { max_safe_len: 2000 },
      stats: {},
      config: {},
      entries: [],
      users: [],
      cached: [],
      total: 0,
      page: 1,
      search: "",
      onlyClone: false,
      onlyPortrait: false,
      offset: 0,
      sort: "nickname",
      desc: false,
      current: null,
      mode: "append",
      draft: "",
      busy: false,
      loading: false,
    };
  };

  App.prototype.status = function (text, kind) {
    if (!this.el.status) return;
    this.el.status.textContent = text;
    this.el.status.className = "status" + (kind ? " status-" + kind : "");
  };

  App.prototype.toast = function (text, kind) {
    if (!this.el.toasts) return;
    var id = ++this.toastSeq;
    var node = el("div", { class: "toast toast-" + (kind || "info"), text: text });
    this.el.toasts.appendChild(node);
    setTimeout(function () {
      node.classList.add("toast-out");
      setTimeout(function () {
        if (node.parentNode) node.parentNode.removeChild(node);
      }, 300);
    }, 3200);
  };

  App.prototype.fail = function (err, fallback) {
    var msg = errText(err, fallback || "操作失败");
    this.status("出错了", "error");
    this.toast(msg, "error");
    console.error(err);
  };

  App.prototype.load = function () {
    var self = this;
    this.state.loading = true;
    self.status("加载中…");
    Promise.resolve()
      .then(function () {
        // 分别取数：一部分失败不影响另一部分渲染
        return Promise.allSettled([api.overview(), api.users(self.userParams())]);
      })
      .then(function (results) {
        var failures = [];
        var overview = results[0].status === "fulfilled" ? results[0].value : null;
        var users = results[1].status === "fulfilled" ? results[1].value : null;
        if (results[0].status === "rejected") failures.push("总览：" + errText(results[0].reason, "接口无响应"));
        if (results[1].status === "rejected") failures.push("列表：" + errText(results[1].reason, "接口无响应"));

        if (overview) {
          self.state.stats = overview.stats || {};
          self.state.config = overview.config || {};
          self.state.entries = overview.entry_commands || [];
          self.state.limits = overview.limits || { max_safe_len: 2000 };
        }
        if (users) {
          self.state.users = users.users || [];
          self.state.total = users.total || 0;
          self.clampOffset();
        }

        // 渲染出错不要伪装成「接口出错」
        try {
          self.renderAll();
        } catch (renderErr) {
          console.error("面板渲染失败", renderErr);
          failures.push("页面渲染：" + errText(renderErr, "渲染异常"));
        }

        if (failures.length) {
          self.status("部分数据加载失败", "error");
          failures.forEach(function (f) {
            self.toast(f, "error");
          });
          self.showLoadError(failures.join("；"));
        } else {
          self.status("就绪", "ok");
          self.clearNotice();
        }
        self.state.loading = false;
      })
      .catch(function (err) {
        self.state.loading = false;
        self.fail(err, "加载面板数据失败");
      });
  };

  // 顶部提示条：明确显示失败原因，并提供重试
  App.prototype.showLoadError = function (detail) {
    var self = this;
    var host = this.el.notice;
    if (!host) return;
    clear(host);
    host.appendChild(
      el("div", { class: "notice" }, [
        el("span", { class: "notice-text", text: "面板数据加载失败：" + detail }),
        el("button", {
          class: "btn",
          text: "重试",
          onclick: function () {
            self.load();
            self.loadCachedUsers();
          },
        }),
      ])
    );
  };

  App.prototype.clearNotice = function () {
    if (this.el.notice) clear(this.el.notice);
  };

  App.prototype.userParams = function () {
    var st = this.state;
    return {
      limit: PAGE_SIZE,
      offset: st.offset,
      search: st.search,
      only_clone: st.onlyClone,
      only_portrait: st.onlyPortrait,
      sort: st.sort,
      desc: st.desc,
    };
  };

  // 列表变短（存档变少 / 搜索过滤）时把 offset 拉回合法范围
  App.prototype.clampOffset = function () {
    var st = this.state;
    var maxPage = Math.max(1, Math.ceil(st.total / PAGE_SIZE));
    var maxOffset = (maxPage - 1) * PAGE_SIZE;
    if (st.offset > maxOffset) {
      st.offset = maxOffset;
      return true;
    }
    return false;
  };

  App.prototype.selectUser = function (uid) {
    var self = this;
    this.state.loading = true;
    self.status("读取档案…");
    api
      .user(uid)
      .then(function (data) {
        self.state.current = data;
        self.state.draft = data.clone_prompt || "";
        self.state.mode = data.clone_len ? "append" : "replace";
        self.renderAll();
        self.status("就绪", "ok");
      })
      .catch(function (err) {
        self.fail(err, "读取档案失败");
      })
      .then(function () {
        self.state.loading = false;
      });
  };

  App.prototype.setMode = function (mode) {
    this.state.mode = mode;
    // 切到「整段替换」时把当前人格灌进编辑框，方便小改
    if (mode === "replace" && this.state.current) {
      this.state.draft = this.state.current.clone_prompt || this.state.draft;
    }
    if (mode === "rewrite") this.state.draft = "";
    this.renderDetail();
  };

  App.prototype.save = function () {
    var self = this;
    var st = this.state;
    if (!st.current) return;
    var mode = st.mode;
    var content = st.draft;
    if (!content || !content.trim()) {
      this.toast(mode === "rewrite" ? "请先写修改要求" : "请先写人格正文", "error");
      return;
    }
    st.busy = true;
    this.renderDetail();
    api
      .update(st.current.user_id, mode, content)
      .then(function (data) {
        self.toast("已保存（" + (data.mode || mode) + "，" + data.length + " 字）", "ok");
        return api.user(st.current.user_id);
      })
      .then(function (fresh) {
        self.state.current = fresh;
        self.state.draft = fresh.clone_prompt || "";
        if (mode === "replace") self.state.mode = "append";
        self.renderAll();
        return self.refreshList();
      })
      .catch(function (err) {
        self.fail(err, "保存失败");
      })
      .then(function () {
        self.state.busy = false;
        self.renderDetail();
      });
  };

  App.prototype.generate = function (mode) {
    var self = this;
    var st = this.state;
    if (!st.current) return;
    if (st.current.cache && !st.current.cache.messages) {
      this.toast("本地没有该群友的聊天记录缓存，请先在群里执行「克隆人格 @群友」", "error");
      return;
    }
    st.busy = true;
    this.renderDetail();
    this.toast(mode === "fresh" ? "正在重新生成…" : "正在融合生成…");
    api
      .generate(st.current.user_id, mode)
      .then(function (data) {
        self.toast(
          "已" + data.mode + "（用 " + data.used_messages + " 条记录，" + data.length + " 字）",
          "ok"
        );
        return api.user(st.current.user_id);
      })
      .then(function (fresh) {
        self.state.current = fresh;
        self.state.draft = fresh.clone_prompt || "";
        self.renderAll();
        return self.refreshList();
      })
      .catch(function (err) {
        self.fail(err, "生成失败");
      })
      .then(function () {
        self.state.busy = false;
        self.renderDetail();
      });
  };

  App.prototype.refreshList = function () {
    var self = this;
    return api
      .users(this.userParams())
      .then(function (data) {
        self.state.users = data.users || [];
        self.state.total = data.total || 0;
        if (self.clampOffset()) return self.refreshList();
        self.renderList();
        self.renderPager();
        return null;
      });
  };

  App.prototype.copyClone = function () {
    var self = this;
    copyText(this.state.current && this.state.current.clone_prompt)
      .then(function () {
        self.toast("人格已复制到剪贴板", "ok");
      })
      .catch(function (err) {
        self.fail(err, "复制失败");
      });
  };

  App.prototype.copySwitchCommand = function () {
    var self = this;
    var cur = this.state.current;
    if (!cur) return;
    copyText("切换人格 @" + (cur.nickname || cur.user_id))
      .then(function () {
        self.toast("已复制：切换人格 @" + (cur.nickname || cur.user_id), "ok");
      })
      .catch(function (err) {
        self.fail(err, "复制失败");
      });
  };

  // ---------------------------------------------------------------- render
  App.prototype.renderAll = function () {
    this.renderKpi();
    this.renderList();
    this.renderPager();
    this.renderDetail();
  };

  App.prototype.renderKpi = function () {
    var self = this;
    var st = this.state;
    var kpi = this.el.kpi;
    if (!kpi) return;
    clear(kpi);
    var cards = [
      ["档案总数", st.stats.profiles, "本地 portrayal.json"],
      ["已克隆人格", st.stats.with_clone, "可用于「切换人格」", "accent"],
      ["已有画像", st.stats.with_portrait, "画像分析文本"],
      ["人格平均字数", st.stats.avg_clone_len, "最长 " + (st.stats.max_clone_len || 0) + " 字"],
      ["保护名单", st.stats.protected, "不允许查询 / 修改"],
    ];
    cards.forEach(function (c) {
      kpi.appendChild(
        el("div", { class: "kpi" + (c[3] ? " kpi-" + c[3] : "") }, [
          el("div", { class: "kpi-label", text: c[0] }),
          el("div", { class: "kpi-value", text: String(c[1] === undefined ? "—" : c[1]) }),
          el("div", { class: "kpi-hint", text: c[2] }),
        ])
      );
    });
  };

  App.prototype.renderList = function () {
    var self = this;
    var st = this.state;
    var list = this.el.list;
    if (!list) return;
    clear(list);

    if (!st.users.length) {
      list.appendChild(el("div", { class: "empty", text: "没有匹配的档案" }));
      return;
    }

    st.users.forEach(function (u) {
      var active = st.current && st.current.user_id === u.user_id;
      var row = el("div", {
        class: "row" + (active ? " active" : ""),
        onclick: function () {
          self.selectUser(u.user_id);
        },
      });
      row.appendChild(
        el("div", { class: "row-name", text: u.nickname || "（无昵称）" })
      );
      row.appendChild(el("div", { class: "row-id", text: u.user_id }));
      row.appendChild(
        el("div", { class: "row-tags" }, [
          u.has_clone
            ? el("span", { class: "tag tag-ok", text: "人格 " + u.clone_len + " 字" })
            : el("span", { class: "tag", text: "未克隆" }),
          u.has_portrait
            ? el("span", { class: "tag", text: "画像 " + u.portrait_len + " 字" })
            : null,
          u.too_long ? el("span", { class: "tag tag-warn", text: "超长" }) : null,
          u.protected ? el("span", { class: "tag tag-warn", text: "保护" }) : null,
        ])
      );
      if (u.timestamp) {
        row.appendChild(el("div", { class: "row-time", text: fmtTime(u.timestamp) }));
      }
      list.appendChild(row);
    });
  };

  App.prototype.renderPager = function () {
    var self = this;
    var st = this.state;
    var pager = this.el.pager;
    if (!pager) return;
    clear(pager);
    var maxPage = Math.max(1, Math.ceil(st.total / PAGE_SIZE));
    var page = Math.min(maxPage, Math.floor(st.offset / PAGE_SIZE) + 1);

    pager.appendChild(
      el("span", {
        class: "muted small",
        text: "共 " + st.total + " 条 · 第 " + page + " / " + maxPage + " 页",
      })
    );
    pager.appendChild(
      el("button", {
        class: "btn btn-ghost",
        text: "上一页",
        disabled: st.loading || st.offset <= 0,
        onclick: function () {
          st.offset = Math.max(0, st.offset - PAGE_SIZE);
          self.load();
        },
      })
    );
    pager.appendChild(
      el("button", {
        class: "btn btn-ghost",
        text: "下一页",
        disabled: st.loading || st.offset + PAGE_SIZE >= st.total,
        onclick: function () {
          st.offset += PAGE_SIZE;
          self.load();
        },
      })
    );
  };

  // ---------------------------------------------------------------- 从缓存建档
  App.prototype.loadCachedUsers = function () {
    var self = this;
    api
      .cachedUsers()
      .then(function (data) {
        self.state.cached = (data && data.users) || [];
        self.renderCached();
      })
      .catch(function () {
        self.state.cached = [];
        self.renderCached();
      });
  };

  App.prototype.createProfile = function (uid) {
    var self = this;
    self.state.busy = true;
    api
      .update(uid, "create", "（占位人格，请用「用缓存生成」或「LLM 重写」补全）")
      .then(function () {
        self.toast("已为 " + uid + " 建档，可用「用缓存生成」生成人格", "ok");
        self.state.cached = (self.state.cached || []).filter(function (u) {
          return u.user_id !== uid;
        });
        self.renderCached();
        return self.load();
      })
      .then(function () {
        self.selectUser(uid);
      })
      .catch(function (err) {
        self.fail(err, "建档失败");
      })
      .then(function () {
        self.state.busy = false;
        self.renderCached();
      });
  };

  App.prototype.renderCached = function () {
    var self = this;
    var st = this.state;
    var host = this.el.cached;
    if (!host) return;
    clear(host);
    var items = st.cached || [];
    if (!items.length) return;

    host.appendChild(
      el("div", {
        class: "cached-title",
        text: "缓存里还有 " + items.length + " 位群友没有档案（先在群里跑过「画像」或「克隆人格」才会有缓存）",
      })
    );
    var row = el("div", { class: "cached-list" });
    items.slice(0, 12).forEach(function (u) {
      row.appendChild(
        el("button", {
          class: "chip",
          disabled: st.busy,
          title: "为该群友建档",
          text: u.user_id + " · " + u.messages + " 条",
          onclick: function () {
            self.createProfile(u.user_id);
          },
        })
      );
    });
    if (items.length > 12) {
      row.appendChild(el("span", { class: "muted small", text: "…等共 " + items.length + " 位" }));
    }
    host.appendChild(row);
  };

  App.prototype.renderDetail = function () {
    var self = this;
    var st = this.state;
    var pane = this.el.detail;
    if (!pane) return;
    clear(pane);

    if (!st.current) {
      pane.appendChild(
        el("div", { class: "empty big", text: "从左侧选一个群友，右侧即可编辑其克隆人格" })
      );
      return;
    }

    var cur = st.current;
    var limit = (st.limits && st.limits.max_safe_len) || 2000;

    // 头部
    pane.appendChild(
      el("div", { class: "detail-head" }, [
        el("div", {}, [
          el("h2", { text: cur.nickname || "（无昵称）" }),
          el("div", {
            class: "muted small",
            text:
              "QQ " +
              cur.user_id +
              " · persona_id " +
              cur.persona_id +
              (cur.timestamp ? " · 更新于 " + fmtTime(cur.timestamp) : ""),
          }),
        ]),
        el("div", { class: "detail-tags" }, [
          el("span", {
            class: "tag " + (cur.clone_len ? "tag-ok" : ""),
            text: "人格 " + cur.clone_len + " 字",
          }),
          el("span", {
            class: "tag " + (cur.clone_len > limit ? "tag-warn" : ""),
            text: "建议 ≤ " + limit,
          }),
          el("span", {
            class: "tag " + (cur.cache && cur.cache.messages ? "" : "tag-warn"),
            text:
              "缓存 " +
              ((cur.cache && cur.cache.messages) || 0) +
              " 条 / " +
              ((cur.cache && cur.cache.groups) || 0) +
              " 群",
          }),
          cur.protected ? el("span", { class: "tag tag-warn", text: "保护名单" }) : null,
        ]),
      ])
    );

    // 模式按钮
    var modes = [
      ["append", "追加"],
      ["replace", "整段替换"],
      ["rewrite", "LLM 重写"],
    ];
    var modeRow = el("div", { class: "actions" });
    modes.forEach(function (m) {
      modeRow.appendChild(
        el("button", {
          class: "btn" + (st.mode === m[0] ? " primary" : ""),
          text: m[1],
          disabled: st.busy,
          onclick: function () {
            self.setMode(m[0]);
          },
        })
      );
    });
    modeRow.appendChild(el("span", { class: "spacer" }));
    modeRow.appendChild(
      el("button", {
        class: "btn btn-ghost",
        text: "复制人格",
        disabled: !cur.clone_len,
        onclick: function () {
          self.copyClone();
        },
      })
    );
    modeRow.appendChild(
      el("button", {
        class: "btn btn-ghost",
        text: "复制切换命令",
        onclick: function () {
          self.copySwitchCommand();
        },
      })
    );
    modeRow.appendChild(
      el("button", {
        class: "btn btn-ghost",
        text: cur.clone_len ? "用缓存融合" : "用缓存生成",
        disabled: st.busy,
        onclick: function () {
          self.generate("merge");
        },
      })
    );
    modeRow.appendChild(
      el("button", {
        class: "btn btn-ghost",
        text: "重新生成",
        disabled: st.busy,
        onclick: function () {
          self.generate("fresh");
        },
      })
    );
    pane.appendChild(modeRow);

    // 模式提示
    var hints = {
      append: "追加：直接在人格末尾拼接这段文字，不调用 LLM（零成本微调）。",
      replace: "整段替换：用编辑框里的内容完整覆盖当前人格，不调用 LLM。",
      rewrite: "LLM 重写：把编辑框内容当作「修改要求」，由 LLM 重写整份人格。",
    };
    pane.appendChild(el("div", { class: "hint", text: hints[st.mode] || "" }));
    if (st.busy) {
      pane.appendChild(
        el("div", { class: "hint hint-warn", text: "正在处理，编辑已暂时锁定…" })
      );
    }

    // 编辑区
    var ta = el("textarea", {
      class: "editor",
      spellcheck: "false",
      disabled: st.busy,
      placeholder:
        st.mode === "rewrite"
          ? "例如：说话更短、少用颜文字、被夸时别急着自嘲"
          : "把人格正文粘贴到这里",
    });
    ta.value = st.draft || "";
    ta.addEventListener("input", function () {
      st.draft = ta.value;
      var counter = pane.querySelector("[data-count]");
      if (counter) counter.textContent = "编辑框 " + ta.value.length + " 字";
      var saveBtn = pane.querySelector("[data-save]");
      if (saveBtn) saveBtn.disabled = st.busy || cur.protected;
    });
    pane.appendChild(ta);

    var foot = el("div", { class: "editor-foot" });
    foot.appendChild(
      el("span", { class: "muted small", "data-count": "1", text: "编辑框 " + (st.draft || "").length + " 字" })
    );
    foot.appendChild(el("span", { class: "spacer" }));
    foot.appendChild(
      el("button", {
        class: "btn btn-ghost",
        text: "载入当前人格",
        disabled: st.busy || st.mode === "rewrite" || !cur.clone_prompt,
        onclick: function () {
          st.draft = cur.clone_prompt || "";
          self.renderDetail();
        },
      })
    );
    foot.appendChild(
      el("button", {
        class: "btn btn-ghost",
        text: "清空",
        disabled: st.busy,
        onclick: function () {
          st.draft = "";
          self.renderDetail();
        },
      })
    );
    foot.appendChild(
      el("button", {
        class: "btn primary",
        "data-save": "1",
        text: st.busy ? "处理中…" : "保存",
        disabled: st.busy || cur.protected,
        onclick: function () {
          self.save();
        },
      })
    );
    pane.appendChild(foot);

    if (cur.protected) {
      pane.appendChild(
        el("div", { class: "hint hint-warn", text: "该用户在保护名单中，面板不允许修改其人格。" })
      );
    }

    // 正文预览
    var split = el("div", { class: "split" });
    split.appendChild(
      el("article", { class: "card" }, [
        el("header", {}, [
          el("h3", { text: "当前克隆人格" }),
          el("span", { class: "muted small", text: cur.clone_len + " 字" }),
        ]),
        el("pre", { class: "pre", text: cur.clone_prompt || "（尚未生成）" }),
      ])
    );
    split.appendChild(
      el("article", { class: "card" }, [
        el("header", {}, [
          el("h3", { text: cur.portrait_stale ? "当前画像（可能早于人格改动）" : "当前画像" }),
          el("span", {
            class: "muted small " + (cur.portrait_stale ? "warn-text" : ""),
            text:
              cur.portrait_len +
              " 字" +
              (cur.portrait_stale ? " · 建议重新跑一次「画像」" : ""),
          }),
        ]),
        el("pre", { class: "pre", text: cur.portrait || "（尚未生成）" }),
      ])
    );
    pane.appendChild(split);
  };

  // ---------------------------------------------------------------- boot
  function boot() {
    var app = new App();
    window.__portrayalApp = app;

    app.el.status = document.querySelector("[data-status]");
    app.el.kpi = document.querySelector("[data-kpi]");
    app.el.list = document.querySelector("[data-list]");
    app.el.detail = document.querySelector("[data-detail]");
    app.el.pager = document.querySelector("[data-pager]");
    app.el.toasts = document.querySelector("[data-toasts]");
    app.el.cached = document.querySelector("[data-cached]");
    app.el.notice = document.querySelector("[data-notice]");

    var search = document.querySelector("[data-search]");
    if (search) {
      search.addEventListener("input", function () {
        app.state.search = search.value;
      });
      search.addEventListener("keyup", function (e) {
        if (e.key === "Enter") {
          app.state.offset = 0;
          app.load();
        }
      });
    }
    var reload = document.querySelector("[data-reload]");
    if (reload) {
      reload.addEventListener("click", function () {
        app.load();
      });
    }

    var toggles = [
      { attr: "only-clone", apply: function (on) { app.state.onlyClone = on; } },
      { attr: "only-portrait", apply: function (on) { app.state.onlyPortrait = on; } },
      { attr: "desc", apply: function (on) { app.state.desc = on; } },
    ];
    toggles.forEach(function (t) {
      var box = document.querySelector("[data-" + t.attr + "]");
      if (!box) return;
      box.addEventListener("change", function () {
        t.apply(box.checked);
        app.state.offset = 0;
        app.load();
      });
    });

    var sort = document.querySelector("[data-sort]");
    if (sort) {
      sort.addEventListener("change", function () {
        app.state.sort = sort.value;
        app.state.offset = 0;
        app.load();
      });
    }

    app.load();
    app.loadCachedUsers();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
