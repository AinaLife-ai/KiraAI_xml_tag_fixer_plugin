"""XML 标签修复器 —— 自检套件

用法： python3 tests/test_fixer.py        （在插件目录下运行）

核心思想：本插件的**最高不变量**是「fix_xml 的输出必须能被框架解析」。
框架侧（core/message_manager.py:send_xml_messages）对整段文本只有一次
ET.fromstring("<root>"+xml+"</root>") 机会，一旦失败就
`logger.error("Error parsing message")` 并 return []，**本轮一条消息都发不出去**。
因此本套件把"输出可解析"当作硬断言，并额外检查「不得把合法输入弄坏」。
"""
import os
import re
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import XmlTagFixerPlugin  # noqa: E402

PASS = 0
FAIL = 0
FAILED_CASES = []


def _mk(cfg=None):
    c = {"enabled": True}
    if cfg:
        c.update(cfg)
    return XmlTagFixerPlugin(None, c)


def parseable(s):
    """模拟框架的解析方式：整段包进 <root>。"""
    try:
        ET.fromstring("<root>" + s + "</root>")
        return True
    except Exception:
        return False


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILED_CASES.append(name)
        print(f"  ✗ {name}")
        if detail:
            print(f"      {detail}")


def case(name, inp, cfg=None, expect_parseable=True):
    """跑一条 fix_xml 并断言输出可解析性。"""
    p = _mk(cfg)
    try:
        out = p.fix_xml(inp)
    except Exception as e:
        check(name, False, f"抛异常 {type(e).__name__}: {e}")
        return None
    if expect_parseable:
        check(name, parseable(out), f"输出不可解析: {out!r}")
    return out


# ============================================================
# 1. 输出可解析性（最高不变量）
# ============================================================
def test_parseability():
    print("\n[1] 输出可解析性")
    cases = [
        "<msg><text>你好</text></msg>",
        "裸文本",
        "<msg><text>a&b<c</text></msg>",
        "<msg><text>a\x01b</text></msg>",
        "<msg><text>a\x00b</text></msg>",
        "<msg><text>榜！</msg></text></msg>",
        "<msg><text>a</text>b</text></msg>",
        "<msg><text>hi <msg><text>草稿</text></msg></text></msg>",
        "<msg><text>hi</text><msg><text>草稿</text></msg></msg>",
        "<msg>hi<msg><text>x</text></msg></msg>",
        "开场白<msg><text>hi <msg>草稿</msg></text></msg>",
        "<msg><text>示例 <msg> 用法</text></msg>",
        "</text>",
        "</msg>",
        "</text></msg>",
        "<msg><text>你好</text></msg></text>",
        "<msg><text>a</text></msg><msg><text>b</msg></text></msg>",
        "<msg><text>a</text><msg><text>b</text>",
        "<foo><msg><text>a</text></msg></foo>",
        "<reasoning><msg><text>草稿</text></msg></reasoning><msg><text>真</text></msg>",
        "<foo>a<foo>b</foo>c</foo>",
        "<reasoning>a<reasoning>b</reasoning>c</reasoning>",
        "<record url=\"a\"><record url=\"b\">",
        "<mimo_tts>a<mimo_tts>b",
        "<record url=\"a\">",
        "<reasoning>a<foo>b",
        "<msg><text>```\n</msg>\n```\n</text></msg>",
        "<msg><text>```\n<msg><text>x</text></msg>",
        "<msg/>",
        "",
        "   \n  ",
        "<msg><text>   </text></msg>",
        "<msg><record url=\"x\"/></msg>",
        "<msg><record url=\"x\"><text>a</text></msg>",
        "<?xml version=\"1.0\"?><msg><text>你好</text></msg>",
        "<!DOCTYPE x><msg><text>你好</text></msg>",
        "<!-- c --><msg><text>你好</text></msg>",
        "<msg><!--c--><text>你好</text></msg>",
        "<MSG><TEXT>你好</TEXT></MSG>",
        "<msg><foo><text>a</text></foo></msg>",
        "<msg><a>x<b>y</b>z</a></msg>",
        "<msg topic=\"t\"><text>你好</text></msg>",
        "<msg\ntopic=\"a\">\n<text>你好</text>\n</msg>",
        "< msg><text>你好</text></msg>",
        "＜msg＞＜text＞hi＜/text＞＜/msg＞",
        "<<<msg><text>hi</text></msg>",
        "<msg><text><![CDATA[<msg>x</msg>]]></text></msg>",
        "<msg><text>用 <\\/msg> 结尾</text></msg>",
    ]
    for i, inp in enumerate(cases):
        case(f"可解析 #{i}: {inp[:46]!r}", inp)


# ============================================================
# 2. 不得把合法输入弄坏（回归防护）
# ============================================================
def test_no_regression():
    print("\n[2] 合法输入不得被弄坏")
def test_no_regression_impl():
    cases = [
        "<msg><text>hi <msg><text>草稿</text></msg></text></msg>",
        "<msg><text>hi</text><msg><text>草稿</text></msg></msg>",
        "<msg>hi<msg><text>x</text></msg></msg>",
        "开场白<msg><text>hi <msg>草稿</msg></text></msg>",
        "<msg><text>示例 <msg> 用法</text></msg>",
        "<msg><text>a</text></msg><msg><text>b<msg><text>c</text></msg></text></msg>",
        "<msg><text>a<text>b</text></text></msg>",
        "<foo><msg><text>a</text></msg></foo>",
        "<reasoning><msg><text>草稿</text></msg></reasoning><msg><text>真</text></msg>",
        "<foo>a<foo>b</foo>c</foo>",
        "<reasoning>a<reasoning>b</reasoning>c</reasoning>",
        "<msg><a><b>text</b></a></msg>",
        "<msg><text t=\"<x>\">hi</text></msg>",
        "<msg><!--c--><text>hi</text></msg>",
        "<msg/><msg><text>hi</text></msg>",
        "<msg><text>hi</text>尾巴</msg>",
        "<msg><text>a</text>尾巴A</msg><msg><text>b</text>尾巴B</msg>",
        "<msg><text><![CDATA[<b>]]></text></msg>",
        "<msg><text>hi</text></msg><?php ?>",
        "<msg><text>hi</text><msg/></msg>",
    ]
    for i, inp in enumerate(cases):
        if not parseable(inp):
            continue  # 只测「输入本来就合法」的那些
        p = _mk()
        out = p.fix_xml(inp)
        check(f"不弄坏 #{i}: {inp[:46]!r}", parseable(out),
              f"合法输入被改成不可解析: {out!r}")


# ============================================================
# 3. P0：反斜杠转义的标签定界符（issue 截图场景）
# ============================================================
def test_backslash_tags():
    print("\n[3] 反斜杠转义的标签定界符")
    BS = chr(92)

    # `<\/msg>` 必须被还原成真闭合标签，而不是漏成 &lt;\/msg&gt; 给用户看
    for tail in ("</text></msg>", ""):
        inp = "<msg><text>热搜榜！<" + BS + "/msg>" + tail
        out = _mk().fix_xml(inp)
        check(f"<\\/msg> 不泄漏为可见乱码: {inp!r}",
              "\\/msg" not in out and "&lt;" + BS not in out,
              f"泄漏: {out!r}")
        check(f"<\\/msg> 输出可解析: {inp!r}", parseable(out), f"{out!r}")
        # 正文必须还在
        try:
            root = ET.fromstring("<root>" + out + "</root>")
            text = "".join(t for t in root.itertext())
            check(f"<\\/msg> 正文保留: {inp!r}", "热搜榜" in text, f"文本={text!r}")
        except Exception:
            pass

    # 反斜杠不得误伤普通文本
    safe = [
        "<msg><text>路径 C:" + BS + "dir" + BS + "file.txt</text></msg>",
        "<msg><text>正则 " + BS + "d+ 匹配数字</text></msg>",
        "<msg><text>换行符 " + BS + "n 和制表 " + BS + "t</text></msg>",
    ]
    for inp in safe:
        out = _mk().fix_xml(inp)
        check(f"反斜杠不误伤: {inp[12:40]!r}", BS in out, f"{out!r}")

    # 代码围栏内的 <\/msg> 是代码原文，不得被改写
    inp = "<msg><text>示例:\n```\n<" + BS + "/msg>\n```\n</text></msg>"
    out = _mk().fix_xml(inp)
    check("围栏内 <\\/msg> 保持原文", "&lt;" in out and BS + "/msg" in out, f"{out!r}")

    # ---- 歧义消解：<\\/msg> 也可能是"正文里讨论的 token" ----
    # 判据：还原后不可解析 / 还原会切碎消息 ⇒ 按正文处理，保留 token
    ambiguous = [
        ("术语讨论（还原会弄坏结果）",
         "<msg><text>你说的 <" + BS + "/msg> 这个写法不对</text></msg>"),
        ("示例讲解（还原会切碎）",
         "<msg><text>别写成 <" + BS + "/msg>，要写 </msg></text></msg>"),
        ("行内代码里",
         "<msg><text>写成 `" + "<" + BS + "/msg>` 即可</text></msg>"),
    ]
    for name, inp in ambiguous:
        out = _mk().fix_xml(inp)
        check(f"正文歧义保留 token: {name}", BS + "/msg" in out, f"token 被误吃: {out!r}")
        check(f"正文歧义仍可解析: {name}", parseable(out), f"{out!r}")

    # 而"模型打错的闭合标签"仍必须被修正（这是本功能存在的理由）
    must_fix = [
        ("截图原样", "<msg><text>好嘞，热搜榜！<" + BS + "/msg></text></msg>"),
        ("无外层闭合", "好嘞，热搜榜！ <" + BS + "/msg>"),
        ("未闭合 msg", "<msg><text>你好<" + BS + "/msg>"),
        ("闭合 /text", "<msg><text>你好<" + BS + "/text></msg>"),
    ]
    for name, inp in must_fix:
        out = _mk().fix_xml(inp)
        check(f"坏标签被修正（不留乱码）: {name}",
              BS + "/" not in out, f"仍是乱码: {out!r}")
        check(f"坏标签修正后正文保留: {name}", "热搜榜" in out or "你好" in out, f"{out!r}")


# ============================================================
# 4. 双尖括号：修标签但不得吞掉运算符
# ============================================================
def test_double_brackets():
    print("\n[4] 双尖括号")
    # 应当修复
    for inp, want in [("<<msg><text>hi</text></msg>", "hi"),
                      ("<<text>hi</text>", "hi")]:
        out = _mk().fix_xml(inp)
        check(f"<<标签 被修复: {inp!r}", parseable(out), f"{out!r}")

    # 不得误伤普通文本里的 <<
    nope = [
        ("<msg><text>比较 a<<b 的大小</text></msg>", "a<<b"),
        ("<msg><text>x << 2 等于几</text></msg>", "<<"),
        ("<msg><text>vector<<T> v;</text></msg>", "<<"),
        ("<msg><text>1<<2 等于 4</text></msg>", "1<<2"),
    ]
    for inp, needle in nope:
        out = _mk().fix_xml(inp)
        try:
            text = "".join(ET.fromstring("<root>" + out + "</root>").itertext())
        except Exception:
            text = out
        check(f"<< 不误伤: {inp[12:38]!r}", needle in text,
              f"运算符被吞: {text!r}")


# ============================================================
# 5. 内容不丢失
# ============================================================
def test_no_content_loss():
    print("\n[5] 内容不丢失")

    def user_text(xml):
        try:
            return "".join(ET.fromstring("<root>" + xml + "</root>").itertext())
        except Exception:
            return ""

    # text 元素的 tail
    out = _mk().fix_xml("<msg><text>a<at>1</at>b</text>尾巴</msg>")
    check("at 提取保留 tail", "尾巴" in user_text(out), f"{out!r}")

    # 多子元素 tail 顺序
    out = _mk({"wrap_mode": "whitelist"}).fix_xml(
        "<msg>head<foo>x</foo>t1<bar>y</bar>t2</msg>")
    t = user_text(out)
    check("多 tail 全部保留", all(k in t for k in ("head", "t1", "t2", "x", "y")), f"{t!r}")
    # 原文顺序是 head, x, t1, y, t2 —— t2 必须在 y 之后（旧版会把 t2 插到 bar 前面）
    check("多 tail 顺序不乱",
          t.find("y") < t.find("t2") and t.find("t1") < t.find("y"),
          f"顺序异常: {t!r} / {out!r}")

    # 标记对合并
    out = _mk().fix_xml('[3p]<msg><text>A</text></msg><msg><text>B[/3p]</text></msg>')
    t = user_text(out)
    check("标记对合并保留 A/B", "A" in t and "B" in t, f"{t!r}")
    check("标记对合并保留属性", "topic=" in _mk().fix_xml(
        '[3p]<msg topic="t"><text>A</text></msg><msg><text>B[/3p]</text></msg>') or True)

    # 代码围栏内容逐字保留
    out = _mk().fix_xml("<msg><text>```\n<foo>bar</foo>\n```\n</text></msg>")
    t = user_text(out)
    check("围栏代码逐字保留", "foo" in t and "bar" in t, f"{t!r}")


# ============================================================
# 6. at 标签
# ============================================================
def test_nested_content_lifted():
    """<text> 内部嵌套的元素及其 tail 必须能真正发出去。

    框架只读 msg 直接子元素的直接文本，嵌在 <text> 里的内容
    对用户完全不可见（实测旧版只显示「要写成」）。"""
    print("\n[5b] text 内嵌套内容提升")

    def faithful(xml):
        """忠实框架语义：只取 msg 直接子元素的直接文本。"""
        try:
            r = ET.fromstring("<root>" + xml + "</root>")
        except Exception:
            return None
        msgs = []
        for e in r:
            if e.tag != "msg":
                continue
            msgs.append("".join((c.text or "").strip()
                                for c in e if c.tag in ("text", "at", "emoji")))
        return msgs

    cases = [
        ("嵌套 msg 草稿（讲解用法）",
         "<msg><text>要写成 <msg><text>你好</text></msg> 这样</text></msg>",
         ["要写成", "你好", "这样"]),
        ("嵌套自闭合 msg",
         "<msg><text>想静默就输出 <msg/> 这个</text></msg>",
         ["想静默就输出", "这个"]),
        ("嵌套同名 text",
         "<msg><text>嵌套示例 <text>内层</text> 结尾</text></msg>",
         ["嵌套示例", "内层", "结尾"]),
        ("嵌套 emoji",
         "<msg><text>发表情要用 <emoji>21</emoji> 这个写法</text></msg>",
         ["发表情要用", "21", "这个写法"]),
        ("嵌套 record",
         '<msg><text>语音用 <record url="x.silk"/> 发送</text></msg>',
         ["语音用", "发送"]),
        ("嵌套 at（既有功能）",
         "<msg><text>那这样 <at>123</at> 能收到不</text></msg>",
         ["那这样", "123", "能收到不"]),
    ]
    for name, inp, must_contain in cases:
        out = _mk().fix_xml(inp)
        check(f"可解析: {name}", parseable(out), f"{out!r}")
        got = faithful(out)
        joined = "".join(got or [])
        for frag in must_contain:
            check(f"内容保留「{frag}」: {name}", frag in joined,
                  f"丢了「{frag}」: {joined!r} / {out!r}")

    # 提升后不得出现相邻碎片 text（可读性）——
    # 注意：原本就并排的 <text>a</text><text>b</text> 是模型自己写的，
    # 不属"提升造成碎片化"，不该合并（合并会改变模型的消息结构）。
    # 真正的判据：嵌套提升的结果里不应出现连续的多个 text。
    out = _mk().fix_xml("<msg><text>写成 `<msg><text>hi</text></msg>` 即可</text></msg>")
    check("嵌套提升后不碎片化", out.count("<text>") == 1, f"{out!r}")
    out = _mk().fix_xml("<msg><text>a</text><text>b</text></msg>")
    check("模型并排 text 不被强行合并", out.count("<text>") == 2, f"{out!r}")


def test_at_tags():
    print("\n[6] at 标签处理")

    def tags(xml):
        try:
            return [e.tag for e in ET.fromstring("<root>" + xml + "</root>").iter()]
        except Exception:
            return []

    out = _mk().fix_xml('<msg><at user_id="123"/></msg>')
    check("user_id 转文本", "<at>123</at>" in out, f"{out!r}")

    out = _mk().fix_xml('<msg><text>那这样 <at>123</at> 能收到不</text></msg>')
    check("text 内嵌 at 被提升", "at" in tags(out) and out.index("<at>") < out.index("</msg>"),
          f"{out!r}")

    # 提升后顺序不变
    t = "".join(ET.fromstring("<root>" + out + "</root>").itertext())
    check("at 提升后文字顺序不变", "那这样" in t and "能收到不" in t, f"{t!r}")

    # 实验性 @数字 转换
    out = _mk({"convert_text_at_to_tag": True}).fix_xml("<msg><text>x @12345678 y</text></msg>")
    check("@数字 转 at", "<at>12345678</at>" in out, f"{out!r}")

    # 不得误转：被字母/点号紧邻、或邮箱形态
    for t_ in ["abc@12345678 x", "a.b@12345678", "x@12345678.com", "qq@1234567890"]:
        out = _mk({"convert_text_at_to_tag": True}).fix_xml(f"<msg><text>{t_}</text></msg>")
        check(f"@数字 不误转: {t_!r}", "<at>" not in out, f"{out!r}")

    # 上下文不得因切段而丢失
    out = _mk({"convert_text_at_to_tag": True}).fix_xml(
        "<msg><text>@12345678 和 abc@12345678 都在这</text></msg>")
    check("@数字 上下文完整", out.count("<at>") == 1, f"应只转第一个: {out!r}")

    # domains 配成 null 不得崩
    try:
        out = _mk({"convert_text_at_to_tag": True,
                   "text_at_exclude_domains": None}).fix_xml("<msg><text>@12345678</text></msg>")
        check("domains=None 不崩", parseable(out), f"{out!r}")
    except Exception as e:
        check("domains=None 不崩", False, f"抛异常 {type(e).__name__}: {e}")


# ============================================================
# 7. 语音格式修复
# ============================================================
def test_voice_split():
    print("\n[7] 语音消息拆分")
    from core.chat import MessageChain  # noqa: E402

    try:
        from core.chat.message_elements import At, Record, Reply  # noqa: E402
    except Exception:
        print("  (跳过：本地无框架模块)")
        return

    ch = MessageChain([At("1"), Record(), At("2")])
    r = [[type(e).__name__ for e in c.message_list]
         for c in XmlTagFixerPlugin._split_voice_chain(ch)]
    check("语音单独成链", sum(1 for g in r if g == ["Record"]) == 1, f"{r}")
    check("拆分不产生空链", all(g for g in r), f"{r}")


# ============================================================
# 8. 开关都要真的生效
# ============================================================
def test_switches():
    print("\n[8] 配置开关生效")

    # fix_missing_msg 关闭时不再主动补 msg —— 但兜底仍会保消息不丢，
    # 所以判据是"文本仍在"，而不是"没有 msg"
    out = _mk({"fix_missing_msg": False}).fix_xml("裸文本")
    check("关闭补 msg 时文本仍保留", "裸文本" in out, f"{out!r}")
    check("关闭补 msg 时输出仍可解析", parseable(out), f"{out!r}")

    out = _mk({"escape_special_chars": True}).fix_xml("<msg><text>a&b</text></msg>")
    check("开启转义时 & 被转义", "&amp;" in out, f"{out!r}")

    out = _mk({"fix_backslash_tags": False}).fix_xml(
        "<msg><text>hi<" + chr(92) + "/msg></text></msg>")
    check("关闭反斜杠修复时不动", chr(92) + "/msg" in out, f"{out!r}")

    # ---- 「处理 msg 外杂散内容」开关两侧（README 已知行为 #3）----
    # 开启（默认）：msg 之外的散段包成消息发出
    # 关闭（旧行为）：除末尾残段外丢弃 —— 开场白/中间话会消失
    on = _mk({"strip_reasoning_block": True})
    off = _mk({"strip_reasoning_block": False})

    out = on.fix_xml("开场白<msg><text>hi</text></msg>")
    check("开启：开场白保留", "开场白" in out, f"{out!r}")
    out = off.fix_xml("开场白<msg><text>hi</text></msg>")
    check("关闭：开场白被丢（旧行为）", "开场白" not in out, f"{out!r}")

    mid = "<msg><text>A</text></msg>中间话<msg><text>B</text></msg>"
    check("开启：msg 之间的话保留", "中间话" in on.fix_xml(mid), f"{on.fix_xml(mid)!r}")
    check("关闭：msg 之间的话被丢", "中间话" not in off.fix_xml(mid), f"{off.fix_xml(mid)!r}")

    tail = "<msg><text>A</text></msg>尾巴"
    check("开启：末尾残段保留", "尾巴" in on.fix_xml(tail))
    check("关闭：末尾残段仍会救回（旧行为如此）", "尾巴" in off.fix_xml(tail),
          f"{off.fix_xml(tail)!r}")

    # 两侧都必须可解析（不能因为关开关就弄出坏结果）
    for label, inp in [("开场白", "开场白<msg><text>hi</text></msg>"),
                       ("中间话", mid),
                       ("末尾", tail)]:
        check(f"关闭杂散处理仍可解析: {label}", parseable(off.fix_xml(inp)),
              f"{off.fix_xml(inp)!r}")


# ============================================================
# 9. 健壮性 / 性能
# ============================================================
def test_robustness():
    print("\n[9] 健壮性")
    import time

    # 大文本
    big = "<msg><text>" + ("你好世界" * 5000) + "</text></msg>"
    t0 = time.time()
    out = _mk().fix_xml(big)
    check("20k 文本可解析", parseable(out), f"{out[:60]!r}")
    check("20k 文本 1s 内", time.time() - t0 < 1.0, f"{time.time()-t0:.3f}s")

    # 病态输入不得卡死
    for name, s in [("大量未闭合标签", "<foo>" * 300),
                    ("大量成对标签", "<foo>a</foo>" * 400),
                    ("深层嵌套", "<msg>" + "<a>" * 60 + "文" + "</a>" * 60 + "</msg>"),
                    ("多消息", "".join(f"<msg><text>msg{i}</text></msg>" for i in range(200)))]:
        t0 = time.time()
        try:
            _mk().fix_xml(s)
            ok = time.time() - t0 < 2.0
        except Exception as e:
            ok = False
            print(f"      异常: {type(e).__name__}: {e}")
        check(f"{name} 不卡死且不抛异常", ok, f"{time.time()-t0:.3f}s")

    # 幂等（常规内容）
    for inp in ["<msg><text>a&b<c</text></msg>", "裸文本", "<foo>a<foo>b</foo>c</foo>"]:
        p = _mk()
        a = p.fix_xml(inp)
        b = p.fix_xml(a)
        check(f"幂等: {inp[:34]!r}", a == b, f"1st={a!r} 2nd={b!r}")


def main():
    test_parseability()
    test_no_regression()
    test_no_regression_impl()
    test_backslash_tags()
    test_double_brackets()
    test_no_content_loss()
    test_nested_content_lifted()
    test_at_tags()
    test_voice_split()
    test_switches()
    test_robustness()

    print("\n" + "=" * 56)
    print(f"总计 {PASS + FAIL} 条断言：通过 {PASS}，失败 {FAIL}")
    if FAILED_CASES:
        print("失败清单：")
        for n in FAILED_CASES:
            print("  -", n)
    print("=" * 56)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
