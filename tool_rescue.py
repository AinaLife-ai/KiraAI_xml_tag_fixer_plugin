"""工具调用搭救（tool-call rescue）：把模型写在输出文本里的工具调用翻译成框架原生 tool_calls。

背景：DeepSeek V4 系等模型在「XML 输出契约 + 原生工具通道」混合提示下，
会把工具调用写成文本 —— 自造 XML 标签（<ops_config>…）或漏出内部 DSML 标记
（<｜DSML｜invoke> 及其退化形态）。框架 parse_xml 对未知顶层标签静默丢弃 ⇒
调用永远不会发生，模型也收不到失败信号，于是反复重犯。

本模块在**响应边界**（模型客户端 chat 返回处、框架锁存 has_tool_calls 之前）
把这类文本调用翻译成标准 tool_calls 注入响应：之后的执行、结果回灌、
多步续跑、消息历史全部走框架原生路径（模型完全无感）。

接管方式（与 acc 同一套纪律）：
- 包装 `ProviderManager.get_model_client`（两代框架签名一致），在返回的每个
  模型客户端实例上包一层 `chat`（与框架自己的 `_guard_model_call` 同款手法）。
- 签名一律透传（*args/**kwargs），幂等标记，卸载只在「当前实现是我们装的」时还原。

不误伤原则（每条都有测试）：
- 已注册输出标签（含插件自定义根标签）永不碰；
- `<msg>` 内、代码围栏内的内容永不碰（讲解/示例场景）；
- 块前有「示例/例如/写法/格式如下」等讲解语境时不碰（可关）；
- 本轮已有真实 tool_calls 时只剥泄漏残片、绝不重复执行（防双执行）；
- 参数按工具 JSON schema 校验+类型归一，不合法不执行；
- 同轮同 (工具+参数) 幂等去重。
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Optional

from core.plugin import logger

# ── DSML 形态（DeepSeek 内部工具标记；上游 vllm#48931/#51914 实锤会漏成文本）────
# 管符变体：ASCII | 与全角 ｜ 可混用且可重复（退化形态常见双管 ｜｜）
_P = r"[|｜]+"
_DSML_ANY = re.compile(rf"</?{_P}DSML{_P}", re.I)
# 标准 invoke：<｜DSML｜invoke name="X">…</｜DSML｜invoke>（闭合符可退化）
_DSML_INVOKE = re.compile(
    rf"<(?:{_P}DSML{_P})?invoke\s+name=\"([^\"]+)\"[^>]*>(.*?)(?:</(?:{_P}DSML{_P}\s*)?invoke\s*>|</(?:{_P}DSML{_P}\s*)?calls\s*>|$)",
    re.S)
# 参数：<｜DSML｜parameter name="k">v</…> 或退化裸 <parameter name="k">v</…>
_DSML_PARAM = re.compile(
    rf"<(?:{_P}DSML{_P})?parameter\s+name=\"([^\"]+)\"[^>]*?>(.*?)(?:</(?:{_P}DSML{_P}\s*)?parameter\s*>|</{_P}DSML{_P}\s*invoke|</{_P}DSML{_P}\s*calls|$)",
    re.S)
# 退化 invoke 头：<exec"> / <exec> / <exec >（名字粘连、可带残引号）
_DEGEN_HEAD = re.compile(r"<([a-zA-Z_][\w-]*)[\"']?\s*>")

# ── 纯 XML 化调用：<toolname>…</toolname>（顶层、msg 外）──────────────────────
# <tool …>inner</tool> 或自闭合 <tool …/>：属性与内层都可能是参数载体
_XML_TOOL = re.compile(
    r"<(?P<name>[a-zA-Z_]\w*)(?P<attrs>(?:\s+[\w-]+(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'))?)*)\s*(?:/>|>(?P<inner>.*?)</(?P=name)>)",
    re.S)
_XML_PARAM = re.compile(r"<([a-zA-Z_]\w*)>(.*?)</\1>", re.S)
# 属性参数：name="value" 或 name='value'（值经 unescape + JSON 归一）
_XML_ATTR = re.compile(r"""([a-zA-Z_][\w-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")

# 工具调用碎片（永远不可能是正文内容）：闭合符/标记，裸或实体转义形态。
# 用于"只剥不执行"的兜底清理（比如模型退化刷出几百条 </invoke> 的 flood）。
_DEBRIS_NAMES = r"(?:invoke|parameter|tool_calls|calls|function_calls|functions)"
_TOOL_DEBRIS = re.compile(
    # 整行形态（连行尾换行一起吃，flood 剥完不留几百个空行）
    rf"^[ \t]*(?:</{_DEBRIS_NAMES}\s*>|&lt;/{_DEBRIS_NAMES}\s*&gt;)[ \t]*\n?"
    # 行内形态
    rf"|(?:</{_DEBRIS_NAMES}\s*>|&lt;/{_DEBRIS_NAMES}\s*&gt;)",
    re.M)
# 转义形态的工具块（&lt;toolname&gt;…&lt;/toolname&gt;）
_XML_TOOL_ESC = re.compile(r"&lt;([a-zA-Z_]\w*)&gt;.*?&lt;/\1&gt;", re.S)

# ── 保护区（绝不碰）────────────────────────────────────────────────────────
_MSG_REGION = re.compile(r"<msg\b.*?</msg>|<msg\s*/>", re.S)
_FENCE_REGION = re.compile(r"```.*?```", re.S)

def _protected_spans(text: str) -> list:
    spans = [ (m.start(), m.end()) for m in _MSG_REGION.finditer(text) ]
    spans += [ (m.start(), m.end()) for m in _FENCE_REGION.finditer(text) ]
    return spans


def strip_tool_debris(text: str, known_tools=None) -> str:
    """剥除工具调用碎片（invoke/parameter/calls 族的闭合符与 DSML 标记，裸或转义形态）。

    这些碎片永远不可能是正文内容（它们是工具调用语法的 token）；模型退化时会
    刷出几百条（flood）。msg/代码围栏内的内容绝不碰（那是给用户看的）。
    只剥不执行 —— 本函数不做任何调用识别。

    传 known_tools（注册工具名集合）时，连"工具形状的块"（<已注册工具名>…</>，
    含转义形态）也一并剥掉 —— 搭救关时它们不会执行，留在文本里只会污染历史。
    """
    has_markup = ("invoke" in text or "parameter" in text
                  or "DSML" in text or "tool_calls" in text)
    if not has_markup and not known_tools:
        return text
    if not text:
        return text
    spans = _protected_spans(text)
    cuts = []
    for dm in _TOOL_DEBRIS.finditer(text):
        if not _in_protected(spans, dm.start(), dm.end()):
            cuts.append((dm.start(), dm.end()))
    for dm in _DSML_ANY.finditer(text):
        if not _in_protected(spans, dm.start(), dm.end()):
            cuts.append((dm.start(), dm.end()))
    if known_tools:
        for m in _XML_TOOL.finditer(text):
            name = m.group("name")
            if name not in known_tools:
                continue
            if _in_protected(spans, m.start(), m.end()):
                continue
            cuts.append((m.start(), m.end()))
        for m in _XML_TOOL_ESC.finditer(text):
            name = m.group(1)
            if name not in known_tools:
                continue
            if _in_protected(spans, m.start(), m.end()):
                continue
            cuts.append((m.start(), m.end()))
    if not cuts:
        return text
    out = text
    for s, e in sorted(set(cuts), reverse=True):
        if _overlaps_protected(spans, s, e):
            continue
        out = out[:s] + out[e:]
    return out


def _in_protected(spans, s, e) -> bool:
    return any(s >= a and e <= b for a, b in spans)


def _overlaps_protected(spans, s, e) -> bool:
    """剥除区间与保护区有交集 ⇒ 不可剥（病态嵌套时保 msg 内容不丢）。"""
    return any(s < b and e > a for a, b in spans)



def _unescape(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&"))


class ToolRescue:
    """工具调用搭救引擎（挂到 ProviderManager.get_model_client）。"""

    _MARK = "_xtf_rescue_patch"

    def __init__(self, plugin):
        self.plugin = plugin
        self._patched_cls = None
        self._installed = False

    # ───────────────────────────── 安装 / 卸载 ─────────────────────────────
    def install(self) -> bool:
        """包装 ProviderManager.get_model_client（探测式；两代签名一致）。"""
        if self._installed:
            return True
        try:
            from core.provider.provider_manager import ProviderManager
        except Exception:
            logger.warning("[xml_tag_fixer] 找不到 ProviderManager，工具搭救未启用")
            return False
        current = ProviderManager.get_model_client
        if getattr(current, self._MARK, False):
            # 已装过（上次 terminate 没卸干净）：认领即可，不再包第二层
            self._patched_cls = ProviderManager
            self._installed = True
            logger.info("[xml_tag_fixer] 工具搭救接管点已存在，本次实例认领")
            return True
        original = current
        mgr = self

        def patched_get_model_client(p_self, *args, **kwargs):
            client = original(p_self, *args, **kwargs)
            try:
                return mgr._wrap_client(client)
            except Exception:
                # 铁律：搭救失败绝不拖垮模型解析（调用方把任何异常都报成
                # "Default LLM model not configured"，会把排查方向带偏）
                logger.exception("[xml_tag_fixer] 客户端包装失败，按未包装放行")
                return client

        patched_get_model_client._xtf_rescue_original = original
        setattr(patched_get_model_client, self._MARK, True)
        ProviderManager.get_model_client = patched_get_model_client
        self._patched_cls = ProviderManager
        self._installed = True
        logger.info("[xml_tag_fixer] 工具调用搭救已接管（ProviderManager.get_model_client → chat 翻译层）")
        return True

    def uninstall(self) -> None:
        cls = self._patched_cls
        if cls is not None and getattr(cls.get_model_client, self._MARK, False):
            cls.get_model_client = cls.get_model_client._xtf_rescue_original
            logger.info("[xml_tag_fixer] 工具搭救已卸载还原")
        self._patched_cls = None
        self._installed = False

    def _wrap_client(self, client, _depth: int = 0):
        """在模型客户端实例上包 chat（实例级，仿框架 _guard_model_call）。

        代理穿透：acc 的流式引擎会把 client 换成 `LLMClientProxy`
        （`__slots__` 类 ⇒ 实例上不能 setattr）——穿到它包的真客户端上包；
        真客户端的实例属性遮蔽其类方法，代理的 `chat` 里调 `wrapped.chat`
        依然会命中我们的包装 ⇒ 搭救与流式引擎两不误。
        包不了就原样放行（宁可不搭救，绝不可让模型解析报错）。
        """
        if client is None or _depth > 2:
            return client
        chat = getattr(client, "chat", None)
        if not callable(chat):
            return client
        inner = getattr(client, "_wrapped", None)
        if getattr(client, "_xtf_rescue_wrapped", False) or \
                (inner is not None and getattr(inner, "_xtf_rescue_wrapped", False)):
            return client
        original = chat  # 已是绑定方法
        mgr = self

        async def chat_rescued(request, *args, **kwargs):
            resp = await original(request, *args, **kwargs)
            try:
                # 请求级闸门：这次调用压根没给模型工具（直调场景：摘要/决策等）
                # ⇒ 文本里的工具形状块不是调用尝试 ⇒ 不动（防直调内容被误剥丢文）
                if getattr(request, "tools", None) or getattr(request, "tool_set", None):
                    mgr.translate(resp)
            except Exception:
                # 搭救失败绝不影响原响应
                logger.exception("[xml_tag_fixer] 工具搭救翻译异常（已忽略，原样放行）")
            return resp

        try:
            client.chat = chat_rescued
            client._xtf_rescue_wrapped = True
        except (AttributeError, TypeError):
            # __slots__ / 代理对象 ⇒ 穿到内层真客户端上包，但**返回外层对象**
            # （外层可能是 acc 的流式代理 —— 返回内层会把人家的引擎旁路掉）。
            if inner is not None:
                self._wrap_client(inner, _depth + 1)
                return client
            logger.debug("[xml_tag_fixer] 客户端不可实例包装（%s），跳过搭救",
                         type(client).__name__)
            return client
        return client

    # ───────────────────────────── 翻译主入口 ─────────────────────────────
    def translate(self, resp) -> bool:
        """若响应文本里有「文本形态的工具调用」→ 剥掉并注入合成 tool_calls。
        返回是否有注入。resp 就地修改（它是刚产出的新对象）。"""
        if resp is None:
            return False
        text = getattr(resp, "text_response", None) or ""
        if not text:
            return False
        has_real = bool(getattr(resp, "tool_calls", None))
        calls, cleaned, changed = self._scan(text, has_real_calls=has_real)
        if changed:
            resp.text_response = cleaned
        if not calls:
            return False
        synth = [{
            "id": f"rescued-{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {"name": c["name"], "arguments": json.dumps(c["args"], ensure_ascii=False)},
        } for c in calls]
        resp.tool_calls = list(resp.tool_calls or []) + synth
        logger.info("[xml_tag_fixer] 搭救 %d 个文本态工具调用 → 原生 tool_calls: %s",
                    len(synth), ", ".join(c["name"] for c in calls))
        self.plugin._note_rescued(calls)  # 反馈记录（默认关）
        return True

    # ───────────────────────────── 扫描识别 ─────────────────────────────
    def _scan(self, text: str, has_real_calls: bool):
        """返回 (calls, cleaned_text, changed)。calls 元素：{"name", "args"}。"""
        spans = _protected_spans(text)
        calls: list[dict] = []
        cuts: list = []  # (start, end) 待剥除区间

        # 形态 A/B：DSML（完整块 / 退化块 / 裸 invoke 无 DSML 前缀）
        _has_tool_markup = bool(
            _DSML_ANY.search(text) or _TOOL_DEBRIS.search(text)
            or re.search(r"<invoke\s+name=", text))
        if self.plugin.rescue_dsml and _has_tool_markup:
            for m in _DSML_INVOKE.finditer(text):
                if _in_protected(spans, m.start(), m.end()):
                    continue
                name = m.group(1)
                inner = m.group(2)
                args = self._parse_dsml_params(inner)
                if self._eligible(name, args, text, m.start()):
                    calls.append({"name": name, "args": args})
                    cuts.append((m.start(), m.end()))
                else:
                    # 失败的调用尝试（已注册工具但参数不合法）= 碎片：整块剥除
                    #（框架本就静默丢弃，留在文本里只会污染历史）
                    tail = text[m.end():]
                    if _TOOL_DEBRIS.search(tail) or _DSML_PARAM.search(tail) or _DSML_ANY.search(tail):
                        cuts.append((m.start(), m.end()))
                    else:
                        cuts.append((m.start(), m.end() - len(inner)))  # 孤立头只剥头
            # 退化形态：无 invoke 前缀的 <工具名"> 头 + 裸 parameter
            if not calls:
                for hm in _DEGEN_HEAD.finditer(text):
                    name = hm.group(1)
                    if not self._tool_known(name):
                        continue
                    tail = text[hm.end():]
                    pm = list(_DSML_PARAM.finditer(tail))
                    if not pm:
                        continue
                    if _in_protected(spans, hm.start(), hm.end()):
                        continue
                    end = hm.end() + pm[-1].end()
                    # 吞掉尾随的 DSML 闭合残片
                    closer = _DSML_ANY.search(text, end)
                    if closer and closer.start() - end < 200:
                        end = closer.end()
                    args = self._parse_dsml_params(tail[:pm[-1].end()])
                    if self._eligible(name, args, text, hm.start()):
                        calls.append({"name": name, "args": args})
                        cuts.append((hm.start(), end))

            # DSML 残片 + 裸闭合残片一律剥除——无论是否搭救成功
            for dm in _DSML_ANY.finditer(text):
                if not _in_protected(spans, dm.start(), dm.end()):
                    cuts.append((dm.start(), dm.end()))
            for dm in _TOOL_DEBRIS.finditer(text):
                if not _in_protected(spans, dm.start(), dm.end()):
                    cuts.append((dm.start(), dm.end()))


        # 形态 C：纯 XML 化调用（ops_config 那种）
        if self.plugin.rescue_xml_tooltag:
            for m in _XML_TOOL.finditer(text):
                if _in_protected(spans, m.start(), m.end()):
                    continue
                name = m.group("name")
                if not self._tool_known(name) or self._tag_known(name):
                    continue
                args = self._parse_xml_params(m.group("inner") or "", m.group("attrs") or "")
                if args is None:
                    continue
                if self._eligible(name, args, text, m.start()):
                    calls.append({"name": name, "args": args})
                # 已注册工具的形状块：救不救都剥掉（救=已执行；没救=失败的调用尝试，
                # 群里本来看不到，留在文本里只污染历史）
                cuts.append((m.start(), m.end()))

        # 幂等去重（同轮同 工具+参数 只留一个）
        deduped = []
        seen = set()
        for c in calls:
            key = (c["name"], json.dumps(c["args"], sort_keys=True, ensure_ascii=False))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(c)

        # 防双执行：本轮已有真调用 ⇒ 只剥残片，绝不注入
        if has_real_calls:
            deduped = []

        if not cuts and len(deduped) == len(calls):
            return deduped, text, False
        cleaned = text
        for s, e in sorted(set(cuts), reverse=True):
            if _overlaps_protected(spans, s, e):
                continue  # 病态嵌套：宁可留下，绝不误删 msg/围栏内容
            cleaned = cleaned[:s] + cleaned[e:]
        return deduped, cleaned, cleaned != text

    # ───────────────────────────── 判定与解析 ─────────────────────────────
    def _tool_known(self, name: str) -> bool:
        if name in (self.plugin.rescue_deny or []):
            return False
        ts = getattr(self.plugin.ctx, "tool_mgr", None)
        tool_set = getattr(ts, "tool_set", None)
        if tool_set is None:
            return False
        try:
            return name in tool_set
        except Exception:
            return False

    def _tag_known(self, name: str) -> bool:
        """注册输出标签（msg 级 + 根级）豁免判定。

        主判据：on_llm_request 缓存的本请求 tag_set 名单；
        兜底：全局 `core.tag.tag_registry` 实时查（缓存还没建立时也安全）。
        """
        if (name in self.plugin._registered_msg_tags
                or name in self.plugin._registered_root_tags):
            return True
        try:
            from core.tag import tag_registry
            return (tag_registry.get(name=name) is not None
                    or tag_registry.get_root(name=name) is not None)
        except Exception:
            return False

    def _eligible(self, name: str, args: Optional[dict], text: str, pos: int) -> bool:
        if args is None:
            return False
        return self._validate_schema(name, args)

    def _schema_of(self, name: str) -> Optional[dict]:
        try:
            tool = self.plugin.ctx.tool_mgr.tool_set.get(name)
            schema = tool.get_schema() if hasattr(tool, "get_schema") else None
            return (schema or {}).get("parameters") or {}
        except Exception:
            return {}

    def _validate_schema(self, name: str, args: dict) -> bool:
        schema = self._schema_of(name)
        if not schema:
            return True  # 拿不到 schema 就放行（工具自己兜底）
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        if any(r not in args for r in required):
            logger.warning("[xml_tag_fixer] 搭救参数缺 required，放弃: %s %s", name, args)
            return False
        for k, v in list(args.items()):
            spec = props.get(k)
            if spec is None:
                continue  # 额外键宽容保留
            fixed = self._coerce(v, spec)
            if fixed is _SKIP:
                logger.warning("[xml_tag_fixer] 搭救参数类型不归一，放弃: %s.%s", name, k)
                return False
            args[k] = fixed
        return True

    @staticmethod
    def _coerce(v, spec):
        t = spec.get("type")
        if t in ("object", "array"):
            if isinstance(v, str):
                try:
                    return json.loads(v)
                except Exception:
                    return _SKIP
            return v
        if t == "integer":
            try:
                return int(float(str(v).strip()))
            except Exception:
                return _SKIP
        if t == "number":
            try:
                return float(str(v).strip())
            except Exception:
                return _SKIP
        if t == "boolean":
            if isinstance(v, bool):
                return v
            s = str(v).strip().lower()
            if s in ("true", "1", "yes"): return True
            if s in ("false", "0", "no"): return False
            return _SKIP
        return v

    def _parse_dsml_params(self, inner: str) -> dict:
        args = {}
        for pm in _DSML_PARAM.finditer(inner):
            val = _unescape(pm.group(2)).strip()
            args[pm.group(1)] = _jsonish(val)
        return args

    def _parse_xml_params(self, inner: str, attrs: str = "") -> Optional[dict]:
        args: dict = {}
        # 属性形态：<qzone_publish text="…">（模型把参数写进属性，第二轮实拍形态）
        if attrs:
            for am in _XML_ATTR.finditer(attrs):
                args[am.group(1)] = _jsonish(_unescape(am.group(2) if am.group(2) is not None else am.group(3) or ""))
        inner = (inner or "").strip()
        if not inner:
            return args
        # JSON 形态：<ops_config>{"domain": ...}</ops_config>
        if inner.startswith("{"):
            try:
                obj = json.loads(_unescape(inner))
                if not isinstance(obj, dict):
                    return None
                args.update(obj)
                return args
            except Exception:
                return None
        # 子标签形态（可能整体被转义过）
        src = _unescape(inner) if "&lt;" in inner else inner
        found = False
        for pm in _XML_PARAM.finditer(src):
            args[pm.group(1)] = _jsonish(pm.group(2).strip())
            found = True
        if not found and not args:
            return None
        return args


class _Skip: ...
_SKIP = _Skip()


def _jsonish(v: str):
    """看起来是 JSON 就解析，否则原样字符串。"""
    s = v.strip()
    if s[:1] in "{[":
        try:
            return json.loads(s)
        except Exception:
            pass
    return v
