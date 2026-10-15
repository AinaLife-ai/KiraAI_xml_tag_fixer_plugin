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
    rf"<{_P}DSML{_P}invoke\s+name=\"([^\"]+)\"[^>]*>(.*?)(?:</{_P}DSML{_P}\s*invoke\s*>|</{_P}DSML{_P}calls\s*>|$)",
    re.S)
# 参数：<｜DSML｜parameter name="k">v</…> 或退化裸 <parameter name="k">v</…>
_DSML_PARAM = re.compile(
    rf"<(?:{_P}DSML{_P})?parameter\s+name=\"([^\"]+)\"[^>]*?>(.*?)(?:</(?:{_P}DSML{_P}\s*)?parameter\s*>|</{_P}DSML{_P}\s*invoke|</{_P}DSML{_P}\s*calls|$)",
    re.S)
# 退化 invoke 头：<exec"> / <exec> / <exec >（名字粘连、可带残引号）
_DEGEN_HEAD = re.compile(r"<([a-zA-Z_][\w-]*)[\"']?\s*>")

# ── 纯 XML 化调用：<toolname>…</toolname>（顶层、msg 外）──────────────────────
_XML_TOOL = re.compile(r"<([a-zA-Z_]\w*)>(.*?)</\1>", re.S)
_XML_PARAM = re.compile(r"<([a-zA-Z_]\w*)>(.*?)</\1>", re.S)

# ── 保护区（绝不碰）────────────────────────────────────────────────────────
_MSG_REGION = re.compile(r"<msg\b.*?</msg>|<msg\s*/>", re.S)
_FENCE_REGION = re.compile(r"```.*?```", re.S)

def _protected_spans(text: str) -> list:
    spans = [ (m.start(), m.end()) for m in _MSG_REGION.finditer(text) ]
    spans += [ (m.start(), m.end()) for m in _FENCE_REGION.finditer(text) ]
    return spans


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
            return mgr._wrap_client(client)

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

    def _wrap_client(self, client):
        """在模型客户端实例上包 chat（实例级，仿框架 _guard_model_call）。"""
        if client is None or getattr(client, "_xtf_rescue_wrapped", False):
            return client
        chat = getattr(client, "chat", None)
        if not callable(chat):
            return client
        original = chat  # 已是绑定方法
        mgr = self

        async def chat_rescued(request, *args, **kwargs):
            resp = await original(request, *args, **kwargs)
            try:
                mgr.translate(resp)
            except Exception:
                # 搭救失败绝不影响原响应
                logger.exception("[xml_tag_fixer] 工具搭救翻译异常（已忽略，原样放行）")
            return resp

        client.chat = chat_rescued
        client._xtf_rescue_wrapped = True
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

        # 形态 A/B：DSML（完整块 / 退化块）
        if self.plugin.rescue_dsml and _DSML_ANY.search(text):
            for m in _DSML_INVOKE.finditer(text):
                if _in_protected(spans, m.start(), m.end()):
                    continue
                name = m.group(1)
                inner = m.group(2)
                args = self._parse_dsml_params(inner)
                if self._eligible(name, args, text, m.start()):
                    calls.append({"name": name, "args": args})
                    cuts.append((m.start(), m.end()))
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

            # DSML 残片（闭合符等）一律剥除——无论是否搭救成功
            for dm in _DSML_ANY.finditer(text):
                if not _in_protected(spans, dm.start(), dm.end()):
                    cuts.append((dm.start(), dm.end()))

        # 形态 C：纯 XML 化调用（ops_config 那种）
        if self.plugin.rescue_xml_tooltag:
            for m in _XML_TOOL.finditer(text):
                if _in_protected(spans, m.start(), m.end()):
                    continue
                name = m.group(1)
                if not self._tool_known(name) or self._tag_known(name):
                    continue
                args = self._parse_xml_params(m.group(2))
                if args is None:
                    continue
                if self._eligible(name, args, text, m.start()):
                    calls.append({"name": name, "args": args})
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
        return (name in self.plugin._registered_msg_tags
                or name in self.plugin._registered_root_tags)

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

    def _parse_xml_params(self, inner: str) -> Optional[dict]:
        inner = inner.strip()
        if not inner:
            return {}
        # JSON 形态：<ops_config>{"domain": ...}</ops_config>
        if inner.startswith("{"):
            try:
                obj = json.loads(_unescape(inner))
                return obj if isinstance(obj, dict) else None
            except Exception:
                return None
        # 子标签形态（可能整体被转义过）
        src = _unescape(inner) if "&lt;" in inner else inner
        args = {}
        found = False
        for pm in _XML_PARAM.finditer(src):
            args[pm.group(1)] = _jsonish(pm.group(2).strip())
            found = True
        return args if found else None


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
