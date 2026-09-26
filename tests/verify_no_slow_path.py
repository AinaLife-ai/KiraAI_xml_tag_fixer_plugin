"""验证「插件修复成功后，框架的慢速 LLM 修复不会被触发」。

框架机制（core/plugin/builtin_plugins/kira-ai/main.py:on_llm_resp）：
    try:
        ET.fromstring(f"<root>{xml_data}</root>")
    except ET.ParseError as e:
        # ★ 额外一次 LLM 调用：慢、费 token、有被"修坏"的风险
        llm_resp = await client.chat(LLMRequest(
            system_prompt=[Prompt(XML_FIX_PROMPT.format(exc=str(e)))],
            user_prompt=[Prompt(xml_data)]))
        resp.text_response = llm_resp.text_response

钩子执行顺序（core/plugin/plugin_handlers.py:EventHandlerRegistry.register）：
    self._handlers[eh.event_type].sort(reverse=True)   # 优先级降序 ⇒ 高优先级先跑
    本插件  @on.llm_response(priority=Priority.HIGH) = 50   → 先
    内置    @on.llm_response()                      = 0    → 后
⇒ 本插件先修好，内置插件的 fromstring 就成功，慢速修复被跳过。

用法： python3 tests/verify_no_slow_path.py
"""
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import XmlTagFixerPlugin  # noqa: E402

PRIORITY_HIGH = 50      # 本插件
PRIORITY_MEDIUM = 0     # 内置 kira-ai 的 on_llm_resp


def slow_repair_would_run(xml_text: str) -> bool:
    """内置插件的判定：解析失败 ⇒ 会调一次 LLM 来修。"""
    try:
        ET.fromstring("<root>" + xml_text + "</root>")
        return False
    except Exception:
        return True


def run_chain(raw: str, use_plugin: bool):
    """按框架的优先级降序跑 ON_LLM_RESPONSE 钩子链。"""
    handlers = []
    if use_plugin:
        handlers.append((PRIORITY_HIGH, "xml_tag_fixer"))
    handlers.append((PRIORITY_MEDIUM, "builtin_kira_ai"))
    handlers.sort(key=lambda h: h[0], reverse=True)   # 框架的 sort(reverse=True)

    text = raw
    slow = False
    for _, name in handlers:
        if name == "xml_tag_fixer":
            text = XmlTagFixerPlugin(None, {"enabled": True}).fix_xml(text)
        else:
            slow = slow_repair_would_run(text)
    return text, slow


CASES = [
    ("正常输出", "<msg><text>你好</text></msg>"),
    ("裸文本", "你好呀"),
    ("忘包 text", "<msg>你好</msg>"),
    ("双尖括号", "<<msg><text>你好</text></msg>"),
    ("截图场景 反斜杠转义", "好嘞，狐去搜搜百度今天的热搜榜！ <" + chr(92) + "/msg>"),
    ("截图场景 加外层",
     "<msg><text>好嘞，狐去搜搜百度今天的热搜榜！<" + chr(92) + "/msg></text></msg>"),
    ("未闭合 msg", "<msg><text>你好<" + chr(92) + "/msg>"),
    ("嵌套 msg 草稿", "<msg><text>等等 <msg><text>草稿</text></msg></text></msg>"),
    ("text 里错写闭合名", "<msg><text>热搜榜！</msg></text></msg>"),
    ("裸闭合标签", "</text>"),
    ("非法控制字符", "<msg><text>a\x01b</text></msg>"),
    ("多未闭合 root 标签", '<record url="a"><record url="b">'),
    ("同名嵌套", "<foo>a<foo>b</foo>c</foo><msg><text>hi</text></msg>"),
    ("运算符号", "<msg><text>比较 a<<b 与 1<<2</text></msg>"),
    ("emoji 与链接", "<msg><text>看 https://x.com/?a=1&b=2 🐍</text></msg>"),
    ("markdown 列表", "<msg><text>1. 一\n2. 二</text></msg>"),
    ("代码围栏", "<msg><text>```py\nprint(1)\n```</text></msg>"),
]


def main():
    print("=" * 76)
    print(f"{'场景':<24}{'不装插件':<18}{'装本插件':<18}")
    print("-" * 76)
    n_without = n_with = 0
    for name, raw in CASES:
        _, slow_without = run_chain(raw, use_plugin=False)
        _, slow_with = run_chain(raw, use_plugin=True)
        n_without += slow_without
        n_with += slow_with
        a = "触发慢速修复" if slow_without else "正常发送"
        b = "触发慢速修复" if slow_with else "正常发送"
        mark = "  ← 已拦截" if (slow_without and not slow_with) else ""
        print(f"{name:<24}{a:<18}{b:<18}{mark}")

    print("=" * 76)
    print(f"共 {len(CASES)} 场景｜不装插件触发慢速修复 {n_without} 次｜装本插件触发 {n_with} 次")

    ok = (n_with == 0)
    print()
    if ok:
        print("✓ 通过：装了本插件后，框架的慢速 LLM 修复一次都不会被触发")
    else:
        print(f"✗ 失败：仍有 {n_with} 个场景会走框架的慢速 LLM 修复")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
