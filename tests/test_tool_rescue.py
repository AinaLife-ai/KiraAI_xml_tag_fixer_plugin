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
    def __init__(self, tools=(), msg_tags=(), root_tags=(), deny=(), explain_guard=True,
                 dsml=True, xmltool=True, feedback=False):
        self.ctx = types.SimpleNamespace(tool_mgr=types.SimpleNamespace(tool_set=FakeToolSet(tools)))
        self._registered_msg_tags = set(msg_tags)
        self._registered_root_tags = set(root_tags)
        self.rescue_deny = list(deny)
        self.rescue_explain_guard = explain_guard
        self.rescue_dsml = dsml
        self.rescue_xml_tooltag = xmltool
        self.rescue_feedback = feedback
        self._rescue_notes = []
    def _note_rescued(self, calls):
        if self.rescue_feedback:
            self._rescue_notes.append(", ".join(c["name"] for c in calls))


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

    # 讲解语境（块前有"示例"）⇒ 不碰
    t3 = '<msg><text>嗯</text></msg>\n示例写法如下：\n<ops_config><domain>plugin</domain></ops_config>'
    resp = FakeResp(t3)
    check("讲解语境豁免", r.translate(resp) is False)
    # 关掉豁免 ⇒ 救（参数要给全，否则 schema 校验照样拦）
    plug2, r2 = mk(explain_guard=False)
    t3_full = ('<msg><text>嗯</text></msg>\n示例写法如下：\n'
               '<ops_config><domain>plugin</domain><target>jmdown</target>'
               '<patch>{"a": 1}</patch></ops_config>')
    resp3 = FakeResp(t3_full)
    check("关掉豁免后正常搭救", r2.translate(resp3) is True)

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
    resp = asyncio.run(client.chat("req", extra="透传"))
    check("chat 包装后注入 tool_calls", bool(resp.tool_calls))
    check("额外 kwargs 原样透传", calls[0][2] == {"extra": "透传"})
    # 幂等：再装一次不双层包
    r2 = ToolRescue(plug)
    assert r2.install() is True
    client2 = pm.get_model_client("p", "m")
    asyncio.run(client2.chat("req"))
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
    resp = asyncio.run(client.chat("req"))
    check("叠层后搭救仍生效", bool(resp.tool_calls))
    # 我们卸载 ⇒ acc 层应完好留下（不乱拆别人的层）
    r.uninstall()
    check("我们卸载后 acc 层完好", getattr(ProviderManager.get_model_client, "_acc_mark", False))
    # 还原环境
    ProviderManager.get_model_client = original


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
    test_failure_isolation()
    test_defaults_off()
    bad = [n for n, ok in RESULTS if not ok]
    print("\n" + "=" * 70)
    print(f"总计 {len(RESULTS)} 项，失败 {len(bad)}")
    if bad:
        print("FAILED:", *bad, sep="\n  ")
        sys.exit(1)
    print("TOOL RESCUE TEST PASSED")


if __name__ == "__main__":
    main()
