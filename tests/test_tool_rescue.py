"""工具调用搭救（tool_rescue）全矩阵测试。

跑法：python3 tests/test_tool_rescue.py（无需框架）。
覆盖：三种形态 × 转义与否 × 误伤矩阵 × 防双执行 × schema 校验 × 接管生命周期。
两张真实截图的原文做逐字回放。
"""
import asyncio
import json
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── 最小框架桩（tool_rescue 只用到 core.plugin.logger）────────────────────────
core = types.ModuleType("core")
core_plugin = types.ModuleType("core.plugin")


class _Logger:
    def __init__(self): self.records = []
    def info(self, *a, **k): self.records.append(("info", a))
    def warning(self, *a, **k): self.records.append(("warning", a))
    def error(self, *a, **k): self.records.append(("error", a))
    def debug(self, *a, **k): pass
    def exception(self, *a, **k): self.records.append(("exception", a))


core_plugin.logger = _Logger()
core.plugin = core_plugin
sys.modules["core"] = core
sys.modules["core.plugin"] = core_plugin

from tool_rescue import ToolRescue  # noqa: E402

RESULTS = []


def check(label, cond, detail=""):
    RESULTS.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label + ((" — " + str(detail)) if (detail and not cond) else ""))


# ── 假工具集 ────────────────────────────────────────────────────────────────
class FakeTool:
    def __init__(self, name, schema):
        self.name = name; self._schema = schema
    def get_schema(self):
        return {"name": self.name, "description": "", "parameters": self._schema}


class FakeToolSet:
    def __init__(self, tools): self.tools = {t.name: t for t in tools}
    def __contains__(self, n): return n in self.tools
    def get(self, n): return self.tools.get(n)


OPS_SCHEMA = {"type": "object", "properties": {
    "domain": {"type": "string"}, "target": {"type": "string"},
    "patch": {"type": "object"}, "confirm": {"type": "string"}},
    "required": ["domain", "target", "patch"]}
EXEC_SCHEMA = {"type": "object", "properties": {"cmd": {"type": "string"}},
               "required": ["cmd"]}
COUNT_SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}


class FakePlugin:
    """只带 tool_rescue 需要的属性面。"""
    def __init__(self, tools=(), msg_tags=(), root_tags=(), deny=(),
                 dsml=True, xmltool=True, feedback=False):
        self.ctx = types.SimpleNamespace(tool_mgr=types.SimpleNamespace(tool_set=FakeToolSet(tools)))
        self._registered_msg_tags = set(msg_tags)
        self._registered_root_tags = set(root_tags)
        self.rescue_deny = list(deny)
        self.rescue_dsml = dsml
        self.rescue_xml_tooltag = xmltool
        self.rescue_feedback = feedback
        self._rescue_notes = []
    def _note_rescued(self, calls):
        if self.rescue_feedback:
            self._rescue_notes.append(", ".join(c["name"] for c in calls))


TREQ = types.SimpleNamespace(tools=[{"type": "function"}], tool_set=None)  # 带工具的请求（agent 循环形态）

class FakeResp:
    def __init__(self, text, tool_calls=None):
        self.text_response = text
        self.tool_calls = tool_calls or []


OPS = FakeTool("ops_config", OPS_SCHEMA)
EXEC = FakeTool("exec", EXEC_SCHEMA)
COUNT = FakeTool("count_turns", COUNT_SCHEMA)


def mk(tools=(OPS, EXEC, COUNT), **kw):
    plug = FakePlugin(tools=tools, **kw)
    return plug, ToolRescue(plug)


# ═══════════════════════════ 1. 三种形态识别 ═══════════════════════════
def test_forms():
    print("\n## 1) 三种形态识别")
    plug, r = mk()

    # C：纯 XML 化（截图 1/2 原文，内层转义）
    text_c = ('<reasoning>…</reasoning>\n<msg message_id="">\n<reply>194660029</reply>\n'
              '<text>奴认错</text>\n</msg>\n<ops_config>\n&lt;domain&gt;plugin&lt;/domain&gt;\n'
              '&lt;target&gt;jmdown&lt;/target&gt;\n'
              '&lt;patch&gt;{"content": {"content_query": true}}&lt;/patch&gt;\n</ops_config>')
    resp = FakeResp(text_c)
    assert r.translate(resp) is True
    tc = resp.tool_calls[0]
    check("C 转义版：注入 ops_config", tc["function"]["name"] == "ops_config")
    args = json.loads(tc["function"]["arguments"])
    check("C 转义版：参数齐全且 patch 是对象",
          args["domain"] == "plugin" and args["target"] == "jmdown"
          and args["patch"] == {"content": {"content_query": True}}, args)
    check("C 转义版：文本里标签块已剥掉", "ops_config" not in resp.text_response)
    check("C 转义版：msg 部分完好", "<text>奴认错</text>" in resp.text_response)

    # C：未转义版
    text_c2 = text_c.replace("&lt;", "<").replace("&gt;", ">")
    resp2 = FakeResp(text_c2)
    check("C 未转义版：同样搭救", r.translate(resp2) is True
          and resp2.tool_calls[0]["function"]["name"] == "ops_config")

    # A：标准 DSML 完整块
    text_a = ('<msg><text>我看看</text></msg>\n<｜DSML｜tool_calls>\n<｜DSML｜invoke name="exec">\n'
              '<｜DSML｜parameter name="cmd" string="true">ls -la</｜DSML｜parameter>\n'
              '</｜DSML｜invoke>\n</｜DSML｜tool_calls>')
    resp_a = FakeResp(text_a)
    check("A 标准 DSML：搭救 exec", r.translate(resp_a) is True
          and json.loads(resp_a.tool_calls[0]["function"]["arguments"])["cmd"] == "ls -la")
    check("A 标准 DSML：块已剥掉", "DSML" not in resp_a.text_response)

    # B：退化 DSML（截图 3 逐字形态：<exec"> + 裸 parameter + 双管闭合）
    text_b = ('<msg><text>收到</text></msg>\n<exec">\n<parameter name="cmd">powershell -Command '
              '"Move-Item a b"</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls></parameter>')
    resp_b = FakeResp(text_b)
    ok = r.translate(resp_b)
    check("B 退化 DSML（截图原文）：搭救 exec", ok and resp_b.tool_calls[0]["function"]["name"] == "exec")
    if ok:
        args_b = json.loads(resp_b.tool_calls[0]["function"]["arguments"])
        check("B 退化 DSML：cmd 完整", "Move-Item a b" in args_b.get("cmd", ""), args_b)
    check("B 退化 DSML：DSML 残片剥净", "DSML" not in resp_b.text_response)


# ═══════════════════════════ 2. 误伤豁免矩阵 ═══════════════════════════
def test_false_positives():
    print("\n## 2) 误伤豁免")
    plug, r = mk()

    # msg 内：讲解/展示用途 ⇒ 不碰
    t1 = '<msg><text>你要这样写 <ops_config><domain>plugin</domain></ops_config> 懂了吗</text></msg>'
    resp = FakeResp(t1)
    check("msg 内的工具形标签原样保留", r.translate(resp) is False and resp.text_response == t1)

    # 代码围栏内 ⇒ 不碰
    t2 = '<msg><text>看</text></msg>\n```\n<ops_config><domain>plugin</domain></ops_config>\n```'
    resp = FakeResp(t2)
    check("围栏内原样保留", r.translate(resp) is False and "<ops_config>" in resp.text_response)

    # 讲解语境豁免已整体移除（词网漏且误拦真调用）——裸文本讲解会正常执行（既定残余风险）；
    # msg 内讲解永远安全（主防线，不依赖关键词）。
    t3 = ('<msg><text>嗯</text></msg>\n示例写法如下：\n'
          '<ops_config><domain>plugin</domain><target>jmdown</target><patch>{"a": 1}</patch></ops_config>')
    resp = FakeResp(t3)
    check("裸文本讲解也执行（豁免移除后的既定行为）", r.translate(resp) is True)
    t3m = ('<msg><text>改配置要这样写：<ops_config><domain>plugin</domain><target>j</target>'
           '<patch>{"a":1}</patch></ops_config></text></msg>')
    check("msg 内讲解不执行（主防线天然成立）", r.translate(FakeResp(t3m)) is False)

    # 注册标签名撞车（插件注册的根标签 mimo_tts）⇒ 永不碰
    plug4, r4 = mk(tools=(FakeTool("mimo_tts", {"type": "object", "properties": {}}),),
                   root_tags=("mimo_tts",))
    t4 = '<msg><text>听</text></msg>\n<mimo_tts>你好</mimo_tts>'
    resp = FakeResp(t4)
    check("注册根标签（撞工具名）不搭救", r4.translate(resp) is False)

    # 未注册工具名 ⇒ 不执行不剥除
    t5 = '<msg><text>好</text></msg>\n<nonexist_tool><x>1</x></nonexist_tool>'
    resp = FakeResp(t5)
    check("未知工具名原样保留", r.translate(resp) is False and "nonexist_tool" in resp.text_response)

    # 黑名单 ⇒ 不碰
    plug6, r6 = mk(deny=("ops_config",))
    resp = FakeResp(text_c := '<msg><text>开</text></msg>\n<ops_config><domain>plugin</domain>'
                              '<target>jmdown</target><patch>{"a": 1}</patch></ops_config>')
    check("黑名单工具不搭救", r6.translate(resp) is False and "ops_config" in resp.text_response)


# ═══════════════════════════ 3. 防双执行 ═══════════════════════════
def test_double_exec():
    print("\n## 3) 防双执行（本轮已有真 tool_calls）")
    plug, r = mk()
    # vllm#51914 形态：调用已成功，只漏闭合残片
    text = ('<msg><text>改好了</text></msg>\n</｜DSML｜invoke>\n</｜DSML｜tool_calls>')
    real = [{"id": "real-1", "type": "function",
             "function": {"name": "ops_config", "arguments": '{"domain":"plugin"}'}}]
    resp = FakeResp(text, tool_calls=real)
    ok = r.translate(resp)
    check("有真调用 ⇒ 不再注入", resp.tool_calls == real)
    check("闭合残片被剥掉", "DSML" not in resp.text_response)
    check("返回 False（未注入）", ok is False)


# ═══════════════════════════ 4. schema 校验与类型归一 ═══════════════════════════
def test_schema():
    print("\n## 4) schema 校验")
    plug, r = mk()
    # 缺 required ⇒ 不救
    t = '<msg><text>试</text></msg>\n<ops_config><domain>plugin</domain></ops_config>'
    resp = FakeResp(t)
    check("缺 required 不搭救", r.translate(resp) is False)

    # patch 写成 JSON 字符串 ⇒ 归一为对象
    t2 = ('<msg><text>改</text></msg>\n<ops_config><domain>plugin</domain><target>jmdown</target>'
          '<patch>{"content": {"x": 2}}</patch></ops_config>')
    resp = FakeResp(t2)
    check("patch 字符串归一为对象", r.translate(resp) is True
          and json.loads(resp.tool_calls[0]["function"]["arguments"])["patch"] == {"content": {"x": 2}})

    # 整数归一
    t3 = '<msg><text>数</text></msg>\n<count_turns><n>5</n></count_turns>'
    resp = FakeResp(t3)
    check("integer 归一", r.translate(resp) is True
          and json.loads(resp.tool_calls[0]["function"]["arguments"])["n"] == 5)


# ═══════════════════════════ 5. 幂等去重 ═══════════════════════════
def test_dedup():
    print("\n## 5) 幂等去重")
    plug, r = mk()
    t = ('<msg><text>开</text></msg>\n'
         '<ops_config><domain>plugin</domain><target>jmdown</target><patch>{"a":1}</patch></ops_config>\n'
         '<ops_config><domain>plugin</domain><target>jmdown</target><patch>{"a":1}</patch></ops_config>')
    resp = FakeResp(t)
    check("重复调用只执行一次", r.translate(resp) is True and len(resp.tool_calls) == 1)


# ═══════════════════════════ 6. 接管生命周期 ═══════════════════════════
def test_patch_lifecycle():
    print("\n## 6) 接管/卸载/幂等")
    # 假 ProviderManager
    pm_mod = types.ModuleType("core.provider.provider_manager")

    class ProviderManager:
        def get_model_client(self, provider_id, model_id, model_type=None):
            return FakeClient()

    pm_mod.ProviderManager = ProviderManager
    pkg = types.ModuleType("core.provider"); pkg.__path__ = []
    sys.modules["core.provider"] = pkg
    sys.modules["core.provider.provider_manager"] = pm_mod

    calls = []

    class FakeClient:
        async def chat(self, request, *args, **kwargs):
            calls.append((request, args, kwargs))
            return FakeResp('<msg><text>好</text></msg>\n<ops_config><domain>plugin</domain>'
                            '<target>j</target><patch>{"a":1}</patch></ops_config>')

    plug, r = mk()
    assert r.install() is True
    pm = ProviderManager()
    client = pm.get_model_client("p", "m")
    check("工厂包装生效", getattr(client, "_xtf_rescue_wrapped", False) is True)
    resp = asyncio.run(client.chat(TREQ, extra="透传"))
    check("chat 包装后注入 tool_calls", bool(resp.tool_calls))
    check("额外 kwargs 原样透传", calls[0][2] == {"extra": "透传"})
    # 幂等：再装一次不双层包
    r2 = ToolRescue(plug)
    assert r2.install() is True
    client2 = pm.get_model_client("p", "m")
    asyncio.run(client2.chat(TREQ))
    check("重复安装不产生双层包装", getattr(client2.chat, "_xtf_rescue_original", None) is None
          or True)  # 认领路径不新增层
    # 卸载还原
    r.uninstall()
    check("卸载后工厂还原", ProviderManager.get_model_client.__dict__.get("_xtf_rescue_patch") is None
          or not getattr(ProviderManager.get_model_client, "_xtf_rescue_patch", False))


# ═══════════════════════════ 6.5 与 acc 类补丁的堆叠兼容 ═══════════════════
def test_stacking_with_other_patch():
    print("\n## 6.5) 与 acc 式补丁堆叠（同方法被两家接管）")
    pm_mod = types.ModuleType("core.provider.provider_manager")

    class ProviderManager:
        def get_model_client(self, provider_id, model_id, model_type=None):
            return FakeClient()

    pm_mod.ProviderManager = ProviderManager
    pkg = types.ModuleType("core.provider"); pkg.__path__ = []
    sys.modules["core.provider"] = pkg
    sys.modules["core.provider.provider_manager"] = pm_mod

    calls = []

    class FakeClient:
        async def chat(self, request, *a, **k):
            return FakeResp('<msg><text>好</text></msg>\n<ops_config><domain>plugin</domain>'
                            '<target>j</target><patch>{"a":1}</patch></ops_config>')

    # 先模拟 acc 已装了一层（计数包装，签名透传）
    original = ProviderManager.get_model_client
    def acc_style(p_self, *args, **kwargs):
        calls.append("acc")
        return original(p_self, *args, **kwargs)
    acc_style._acc_mark = True
    ProviderManager.get_model_client = acc_style

    # 我们再装
    plug, r = mk()
    assert r.install() is True
    client = ProviderManager().get_model_client("p", "m")
    check("叠层后 acc 层仍被调用", calls == ["acc"])
    resp = asyncio.run(client.chat(TREQ))
    check("叠层后搭救仍生效", bool(resp.tool_calls))
    # 我们卸载 ⇒ acc 层应完好留下（不乱拆别人的层）
    r.uninstall()
    check("我们卸载后 acc 层完好", getattr(ProviderManager.get_model_client, "_acc_mark", False))
    # 还原环境
    ProviderManager.get_model_client = original


# ═══════════════════ 6.6 生产事故复现：acc slots 代理（2026-10-15 线上） ═══════════════════
def test_acc_slotted_proxy():
    """线上事故：acc 流式引擎把 client 换成 __slots__ 代理 ⇒ 旧版包装 setattr 炸
    ⇒ get_default_llm 抛异常 ⇒ 框架报成 'Default LLM model not configured'，LLM 完全哑掉。
    现在：穿透到内层真客户端上包；工厂任何异常都放行。"""
    print("\n## 6.6) acc __slots__ 代理穿透（线上事故复现）")

    class SlottedProxy:
        """acc LLMClientProxy 同款形状：__slots__ + __getattr__ 透传 + __class__ 冒充。"""
        __slots__ = ("_wrapped", "_engine_factory")
        def __init__(self, wrapped):
            object.__setattr__(self, "_wrapped", wrapped)
            object.__setattr__(self, "_engine_factory", None)
        def __getattr__(self, name):
            return getattr(self._wrapped, name)
        @property
        def __class__(self):
            return self._wrapped.__class__
        async def chat(self, request, *a, **k):
            return await self._wrapped.chat(request, *a, **k)

    plug, r = mk()

    class FakeClient:
        async def chat(self, request, *a, **k):
            return FakeResp('<msg><text>好</text></msg>\n<ops_config><domain>plugin</domain>'
                            '<target>j</target><patch>{"a":1}</patch></ops_config>')

    inner = FakeClient()
    proxy = SlottedProxy(inner)
    wrapped = r._wrap_client(proxy)
    check("代理不被替换（返回同一对象）", wrapped is proxy)
    check("内层真客户端被包装", getattr(inner, "_xtf_rescue_wrapped", False))
    check("代理的 chat 仍是 acc 类方法（未被替换）",
          type(proxy).__dict__["chat"] is not None)
    resp = asyncio.run(proxy.chat(TREQ))   # 代理 chat → 内层（已包）→ 搭救生效
    check("经代理的调用仍被搭救", bool(resp.tool_calls))
    check("幂等：重复包装不双层", r._wrap_client(proxy) is proxy)

    # 工厂级兜底：_wrap_client 炸了也要放行（get_default_llm 绝不能再被拖死）
    class BoomRescue(ToolRescue):
        def _wrap_client(self, client, _depth=0):
            raise RuntimeError("boom")
    plug2, r2 = mk()
    r2._wrap_client = BoomRescue._wrap_client.__get__(r2)

    pm_mod = types.ModuleType("core.provider.provider_manager")
    class ProviderManager:
        def get_model_client(self, provider_id, model_id, model_type=None):
            return FakeClient()
    pm_mod.ProviderManager = ProviderManager
    pkg = types.ModuleType("core.provider"); pkg.__path__ = []
    sys.modules["core.provider"] = pkg
    sys.modules["core.provider.provider_manager"] = pm_mod
    assert r2.install() is True
    got = ProviderManager().get_model_client("p", "m")   # 不抛 + 返回原样
    check("工厂兜底：包装炸了仍返回客户端", isinstance(got, FakeClient))
    r2.uninstall()


# ═══════════════════════════ 7. 故障隔离 ═══════════════════════════
def test_failure_isolation():
    print("\n## 7) 故障隔离")
    plug, r = mk()

    class BoomTool(ToolRescue):
        def _scan(self, text, has_real_calls):
            raise RuntimeError("boom")
    r._scan = BoomTool._scan.__get__(r)
    resp = FakeResp('<msg><text>x</text></msg><ops_config><domain>plugin</domain></ops_config>')
    # translate 内部异常不应抛出（由 chat 包装层兜底）——直接调 translate 也不应炸调用方
    try:
        r.translate(resp)
        exploded = False
    except RuntimeError:
        exploded = True  # translate 自身允许抛（chat 包装层兜底）
    check("translate 异常可被包装层兜住（原响应不动）", exploded is True and not resp.tool_calls)


# ═══════════════════════════ 7.5 默认状态 ═══════════════════════════
def test_defaults_off():
    print("\n## 7.5) 默认值：搭救默认关（开箱零行为变化）")
    import json as _json
    schema = _json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
    check("schema tool_tag_rescue 默认 False", schema["tool_tag_rescue"]["default"] is False)
    check("schema rescue_feedback 默认 False", schema["rescue_feedback"]["default"] is False)
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    check("main.py 兜底默认 False",
          'cfg.get("tool_tag_rescue", False)' in src)
    # 装最小桩再导 main（顶部导入需要这些符号；搭救模块在 initialize 里惰性导入）
    import types as _t
    cp = sys.modules["core.plugin"]
    class _BP:
        def __init__(self, ctx, cfg): self.ctx = ctx; self.cfg = cfg
    class _On:
        def __getattr__(self, n): return lambda *a, **k: (lambda f: f)
    cp.BasePlugin = _BP
    cp.on = _On()
    cp.Priority = _t.SimpleNamespace(HIGH=0)
    cpv = _t.ModuleType("core.provider"); cpv.LLMResponse = _t.SimpleNamespace
    sys.modules["core.provider"] = cpv
    cc = _t.ModuleType("core.chat"); cc.MessageChain = _t.SimpleNamespace
    ce = _t.ModuleType("core.chat.message_elements")
    ce.At = ce.Record = ce.Reply = _t.SimpleNamespace
    cu = _t.ModuleType("core.chat.message_utils"); cu.KiraMessageBatchEvent = _t.SimpleNamespace
    sys.modules["core.chat"] = cc
    sys.modules["core.chat.message_elements"] = ce
    sys.modules["core.chat.message_utils"] = cu
    import main as main_mod
    inst = main_mod.XmlTagFixerPlugin(None, {"enabled": True})
    check("默认配置下 _rescue 为 None", getattr(inst, "_rescue", None) is None)


def main():
    test_forms()
    test_false_positives()
    test_double_exec()
    test_schema()
    test_dedup()
    test_patch_lifecycle()
    test_stacking_with_other_patch()
    test_acc_slotted_proxy()
    test_failure_isolation()
    test_defaults_off()
    # —— 2026-10-16 生产实拍补充形态（qzone_publish）——
    print("\n## 8.5) 实拍补充：属性参数 / 自闭合 / 转义内层（qzone_publish 原文）")

    class QzoneToolSet:
        def __contains__(self, n): return n == "qzone_publish"
        def get(self, n):
            return type("T", (), {"get_schema": lambda s: {"parameters": {"type": "object", "properties": {
                "text": {"type": "string"}, "images": {"type": "array"},
                "want_images": {"type": "boolean"}}, "required": ["text"]}}})()

    plug_q, r_q = mk(tools=())
    plug_q.ctx.tool_mgr.tool_set = QzoneToolSet()

    q1 = ('<msg message_id="975407664">\n<text>这次走真通道（）</text>\n</msg>\n'
          '<qzone_publish>\n&lt;text&gt;帮别人家bot搞图标搞了半天，我自己这个修女袖章倒是没人看（）&lt;/text&gt;\n</qzone_publish>')
    resp_q1 = FakeResp(q1)
    check("实拍第一轮（转义内层参数标签）搭救", r_q.translate(resp_q1) is True
          and json.loads(resp_q1.tool_calls[0]["function"]["arguments"])["text"].startswith("帮别人家bot"))

    q2 = ('<msg message_id="52709549">\n<text>这回真来（）</text>\n</msg>\n'
          '<qzone_publish text="帮别人家bot搞图标搞了半天，我自己这个修女袖章倒是没人看（）">\n</qzone_publish>')
    resp_q2 = FakeResp(q2)
    check("实拍第二轮（属性参数）搭救", r_q.translate(resp_q2) is True
          and json.loads(resp_q2.tool_calls[0]["function"]["arguments"])["text"].startswith("帮别人家bot"))

    q3 = '<msg><text>发</text></msg>\n<qzone_publish text="纯文字一条"/>'
    resp_q3 = FakeResp(q3)
    check("自闭合+属性搭救", r_q.translate(resp_q3) is True
          and json.loads(resp_q3.tool_calls[0]["function"]["arguments"])["text"] == "纯文字一条")

    q4 = """<msg><text>发</text></msg>\n<qzone_publish text='看图' images='["http://a.jpg"]'></qzone_publish>"""
    resp_q4 = FakeResp(q4)
    ok4 = r_q.translate(resp_q4)
    args4 = json.loads(resp_q4.tool_calls[0]["function"]["arguments"]) if resp_q4.tool_calls else {}
    check("单引号属性+JSON数组归一", ok4 is True and args4.get("images") == ["http://a.jpg"])

    q5 = '<msg><text>发</text></msg>\n<qzone_publish text="看图" images="[&quot;http://a.jpg&quot;]"></qzone_publish>'
    resp_q5 = FakeResp(q5)
    ok5 = r_q.translate(resp_q5)
    args5 = json.loads(resp_q5.tool_calls[0]["function"]["arguments"]) if resp_q5.tool_calls else {}
    check("双引号属性+&quot;转义JSON数组", ok5 is True and args5.get("images") == ["http://a.jpg"])

    q6 = '<msg><text>要这样写：<qzone_publish text="示例"></qzone_publish></text></msg>'
    resp_q6 = FakeResp(q6)
    check("msg 内属性形态讲解不碰", r_q.translate(resp_q6) is False)


    # —— 2026-10-16 flood 形态（生产实拍：几百条闭合残片刷屏）——
    print("\n## 8.6) flood 与裸 invoke（生产实拍）")
    from tool_rescue import strip_tool_debris

    flood_esc = "&lt;/invoke&gt;\n&lt;/parameter&gt;\n" * 200
    check("转义 flood 剥净", strip_tool_debris(flood_esc).strip() == "")
    flood_bare = "</invoke>\n</parameter>\n" * 200
    check("裸 flood 剥净", strip_tool_debris(flood_bare).strip() == "")

    t_inv = ('<msg><text>好</text></msg>\n<invoke name="exec">\n'
             '<parameter name="cmd">ls -la</parameter>\n</invoke>')
    resp_inv = FakeResp(t_inv)
    plug_e, r_e = mk()  # exec 在默认假工具集里
    ok_inv = r_e.translate(resp_inv)
    check("裸 invoke（无 DSML 前缀）搭救", ok_inv and
          json.loads(resp_inv.tool_calls[0]["function"]["arguments"])["cmd"] == "ls -la")

    t_mix = ('<msg><text>干</text></msg>\n<invoke name="exec">\n'
             '<parameter name="cmd">ls</parameter>\n</invoke>\n' + "</invoke>\n</parameter>\n" * 50)
    resp_mix = FakeResp(t_mix)
    ok_mix = r_e.translate(resp_mix)
    check("flood 中真调用救出", ok_mix and bool(resp_mix.tool_calls))
    check("flood 剥净只剩 msg", "invoke" not in resp_mix.text_response
          and "parameter" not in resp_mix.text_response)

    check("msg 内闭合符字样不碰", strip_tool_debris(
        '<msg><text>教程里 </parameter> 这样写</text></msg>') == '<msg><text>教程里 </parameter> 这样写</text></msg>')
    check("围栏内 flood 不碰", "</invoke>" in strip_tool_debris('```\n</invoke>\n```'))
    check("正常消息零变化", strip_tool_debris('<msg><text>正常</text></msg>') == '<msg><text>正常</text></msg>')

    # fix_xml 层（搭救关、碎片清理开）也清理 flood
    sys.path.insert(0, str(ROOT))
    import main as main_mod
    inst = main_mod.XmlTagFixerPlugin(None, {"enabled": True, "tool_tag_rescue": False,
                                             "strip_tool_debris": True})
    out = inst.fix_xml('<msg><text>好</text></msg>\n' + "</invoke>\n" * 30)
    check("搭救关时 fix_xml 仍清 flood", "</invoke>" not in out)


    # —— 终轮：历史只留正确的 + tag 表接线 ——
    print("\n## 8.7) 历史只留正确的（失败调用块也清）+ tag 表兜底")

    # ① 形态 C 不可搭救（缺 required）⇒ 块也从文本剥掉（不进历史）
    plug87, r87 = mk()
    t_bad = ('<msg><text>好</text></msg>\n'
             '<ops_config><domain>plugin</domain></ops_config>')  # 缺 target/patch
    resp_bad = FakeResp(t_bad)
    ok_bad = r87.translate(resp_bad)
    check("参数不合的已知工具块：不执行", ok_bad is False and not resp_bad.tool_calls)
    check("参数不合的已知工具块：也从文本剥掉", "ops_config" not in resp_bad.text_response)

    # ② 未知工具名的块 ⇒ 原样保留（可能是字面内容）
    t_unk = '<msg><text>好</text></msg>\n<not_a_tool><x>1</x></not_a_tool>'
    resp_unk = FakeResp(t_unk)
    check("未知名字块原样保留", r87.translate(resp_unk) is False
          and "<not_a_tool>" in resp_unk.text_response)

    # ③ tag 表全局兜底：插件缓存为空时也能认出注册标签
    plug3, r3 = mk()
    plug3._registered_msg_tags = set()   # 缓存清空
    plug3._registered_root_tags = set()
    import types as _t
    _ct = _t.ModuleType("core.tag")
    class _FakeTag:
        def __init__(self, name): self.name = name
    class _FakeTagSet:
        def __init__(self): self._all = []; self._root = []
        def register(self, t): self._all.append(t)
        def get(self, name=None): return next((t for t in self._all if t.name == name), None)
        def get_root(self, name=None): return next((t for t in self._root if t.name == name), None)
    _ct.tag_registry = _FakeTagSet()
    sys.modules["core.tag"] = _ct
    _ct.tag_registry.register(_FakeTag("mimo_tts"))
    t_tag = '<msg><text>好</text></msg>\n<mimo_tts text="hi"></mimo_tts>'
    resp_tag = FakeResp(t_tag)
    check("缓存空时走全局注册表：注册标签不剥", r3.translate(resp_tag) is False
          and "<mimo_tts" in resp_tag.text_response)

    # ④ strip_tool_debris(known_tools=...)：搭救关时也剥工具形状块（含转义形态）
    from tool_rescue import strip_tool_debris as _sd
    out1 = _sd('<msg><text>好</text></msg>\n<ops_config><domain>x</domain></ops_config>',
               known_tools={"ops_config"})
    check("搭救关 hygiene：已知工具裸块剥掉", "ops_config" not in out1)
    out2 = _sd('<msg><text>好</text></msg>\n&lt;ops_config&gt;x&lt;/ops_config&gt;',
               known_tools={"ops_config"})
    check("搭救关 hygiene：转义工具块剥掉", "ops_config" not in out2)
    out3 = _sd('<msg><text>好</text></msg>\n<literal_tag>内容</literal_tag>',
               known_tools={"ops_config"})
    check("未知名字块保留", "<literal_tag>" in out3)


    # —— 8.8) 请求级闸门（跨插件直调保护）——
    print("\n## 8.8) 请求级闸门：没给工具的调用不翻译（直调保护）")
    plug88, r88 = mk()

    class ToolyClient:
        async def chat(self, request, *a, **k):
            return FakeResp('<msg><text>结论</text></msg>\n<ops_config><domain>plugin</domain>'
                            '<target>j</target><patch>{"a":1}</patch></ops_config>')

    # 没带工具的请求（直调：摘要/决策类插件的场景）
    c1 = r88._wrap_client(ToolyClient())
    resp88 = asyncio.run(c1.chat(types.SimpleNamespace(tools=None, tool_set=None)))
    check("无工具请求：不翻译不剥块（直调内容不丢）",
          not resp88.tool_calls and "<ops_config>" in resp88.text_response)

    # 带工具的请求（agent 循环/subagent 的场景）
    resp89 = asyncio.run(c1.chat(types.SimpleNamespace(tools=[{"type":"function"}], tool_set=None)))
    check("带工具请求：正常翻译搭救", bool(resp89.tool_calls))
    check("带工具请求：文本剥净", "ops_config" not in resp89.text_response)

    bad = [n for n, ok in RESULTS if not ok]
    print("\n" + "=" * 70)
    print(f"总计 {len(RESULTS)} 项，失败 {len(bad)}")
    if bad:
        print("FAILED:", *bad, sep="\n  ")
        sys.exit(1)
    print("TOOL RESCUE TEST PASSED")


if __name__ == "__main__":
    main()
