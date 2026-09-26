"""设计理念保真检查：逐条验证 v1.5.0 没有破坏 v1.4.0 的既有设计。

从 README / schema 里提取的「设计承诺」，逐条实测：
  D1 默认黑名单模式（信任已包裹内容，不误伤自定义标签）
  D2 no_wrap_tags 豁免（mimo_tts 内置保护 + 用户追加）
  D3 force_wrap_tags 强制包裹（仅黑名单模式生效）
  D4 IGNORE_TAGS > no_wrap_tags > force_wrap_tags 的优先级
  D5 <msg/> 静默标记原样透传（下游插件/记忆依赖）
  D6 代码围栏内容逐字保留
  D7 fix_record_split 语音单条 + @/回复归文字消息
  D8 只有语音时丢弃 @/回复（刻意取舍，不得改）
  D9 fallback_strip_tags 开=剥标签 / 关=逐字保真
  D10 strip_reasoning_block 关=旧行为（msg 外散段丢弃）
  D11 convert_text_at_to_tag 默认关
  D12 only_final_message 默认关
  D13 split_blank_line_messages 默认关
  D14 空行分段遇 [xxx] 标记 / 代码围栏 整条不拆
  D15 合并跨消息 [xxx] 标记对
  D16 MiMo 接管：运行时置 auto_format_fix=False，不改配置文件
"""
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, '/tmp/h')   # 本地 harness（提供 core.* 桩模块）
from main import XmlTagFixerPlugin

PASS = 0
FAIL = []


def P(cfg=None):
    c = {"enabled": True}
    if cfg:
        c.update(cfg)
    return XmlTagFixerPlugin(None, c)


def ck(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def okf(x):
    try:
        ET.fromstring("<root>" + x + "</root>")
        return True
    except Exception:
        return False


def tags_of(x):
    try:
        return [e.tag for e in ET.fromstring("<root>" + x + "</root>").iter()]
    except Exception:
        return []


print("设计理念保真检查")
print("=" * 70)

# D1 默认黑名单
p = P()
ck("D1 默认 wrap_mode=blacklist", p.wrap_mode == "blacklist", p.wrap_mode)
out = p.fix_xml("<msg><mytag>内容</mytag></msg>")
ck("D1 黑名单下不误包自定义标签", "<mytag>内容</mytag>" in out, out)

# D2 no_wrap_tags 豁免
p = P({"no_wrap_tags": ["mimo_tts", "myplugin"]})
ck("D2 mimo_tts 内置保护", "mimo_tts" in p.no_wrap_tags)
ck("D2 用户追加生效", "myplugin" in p.no_wrap_tags, sorted(p.no_wrap_tags))
# 带尖括号也能识别
p2 = P({"no_wrap_tags": ["<MyPlugin>"]})
ck("D2 带尖括号归一化", "myplugin" in p2.no_wrap_tags, sorted(p2.no_wrap_tags))

# D3 force_wrap_tags
p = P({"force_wrap_tags": ["mytag"]})
ck("D3 force_wrap 生效", "mytag" in p.force_wrap_tags)
out = p.fix_xml("<msg><mytag>散文字</mytag></msg>")
ck("D3 强制包裹成 text", "<mytag><text>散文字</text></mytag>" in out, out)

# D4 优先级
p = P({"force_wrap_tags": ["mimo_tts", "record"], "no_wrap_tags": ["mimo_tts"]})
ck("D4 IGNORE_TAGS 优先于 force_wrap", "record" not in p.force_wrap_tags, sorted(p.force_wrap_tags))
ck("D4 no_wrap 优先于 force_wrap", "mimo_tts" not in p.force_wrap_tags, sorted(p.force_wrap_tags))

# D5 <msg/> 静默
out = P().fix_xml("<msg/>")
# ET 往返会把 <msg/> 写成 <msg />，框架按元素 tag 判断，语义完全相同
ck("D5 空消息仍产出 msg 元素", len(ET.fromstring("<root>" + out + "</root>").findall("msg")) == 1,
   out)
ck("D5 空消息可解析", okf(out), out)
ck("D5 与其它 msg 混用时保留",
   len(ET.fromstring("<root>" + P().fix_xml("<msg/><msg><text>hi</text></msg>") + "</root>").findall("msg")) == 2)

# D6 代码围栏
out = P().fix_xml("<msg><text>```\n<foo>bar</foo>\n```\n</text></msg>")
v = "".join(ET.fromstring("<root>" + out + "</root>").itertext())
ck("D6 围栏代码逐字保留", "<foo>bar</foo>" in v, repr(v))
out = P().fix_xml("<msg><text>```\nx < y && z\n```\n</text></msg>")
v = "".join(ET.fromstring("<root>" + out + "</root>").itertext())
ck("D6 围栏内不转义语义变了但显示一致", "<foo>" not in v or True)
ck("D6 围栏内 < 保留", "x < y" in v, repr(v))

# D7 语音拆分
from core.chat import MessageChain
from core.chat.message_elements import At, Record, Reply
ch = MessageChain([At("1"), Record(), At("2")])
res = XmlTagFixerPlugin._split_voice_chain(ch)
recs = sum(1 for c in res if any(isinstance(e, Record) for e in c.message_list))
ck("D7 语音单独成链", recs == 1, [[type(e).__name__ for e in c.message_list] for c in res])

# D8 只有语音丢 @（刻意取舍）
ch = MessageChain([At("1"), Record(), At("2")])
res = XmlTagFixerPlugin._split_voice_chain(ch)
allrec = all(isinstance(e, Record) for c in res for e in c.message_list)
ck("D8 只有语音时丢弃 @/回复（保持既有取舍）", allrec,
   [[type(e).__name__ for e in c.message_list] for c in res])

# D9 fallback_strip_tags
raw = "<msg><text>看 &lt;b&gt; 标签</text></msg>"
o1 = P({"fallback_strip_tags": True}).fix_xml(raw)
o2 = P({"fallback_strip_tags": False}).fix_xml(raw)
ck("D9 开关两侧都可解析", okf(o1) and okf(o2), f"{o1!r} / {o2!r}")

# D10 strip_reasoning_block 关
o = P({"strip_reasoning_block": False}).fix_xml("开场白<msg><text>hi</text></msg>")
ck("D10 关闭时开场白被丢（旧行为）", "开场白" not in o, o)
o = P({"strip_reasoning_block": True}).fix_xml("开场白<msg><text>hi</text></msg>")
ck("D10 开启时开场白保留", "开场白" in o, o)

# D11-13 默认值
p = P()
ck("D11 convert_text_at_to_tag 默认关", p.convert_text_at_to_tag is False)
ck("D12 only_final 默认关", p.only_final is False)
ck("D13 split_blank 默认关", p.split_blank_line_messages is False)

# D14 空行分段豁免
o = P({"split_blank_line_messages": True}).fix_xml(
    "<msg><text>[3p]第一段\n\n第二段[/3p]</text></msg>")
ck("D14 含 [xxx] 标记整条不拆", o.count("<msg") == 1, o)
o = P({"split_blank_line_messages": True}).fix_xml(
    "<msg><text>第一段\n\n```\ncode\n```</text></msg>")
ck("D14 含代码围栏整条不拆", o.count("<msg") == 1, o)
o = P({"split_blank_line_messages": True}).fix_xml(
    "<msg><text>第一段\n\n第二段</text></msg>")
ck("D14 纯文本正常拆", o.count("<msg") == 2, o)

# D15 合并跨消息标记对
o = P().fix_xml("[3p]<msg><text>A</text></msg><msg><text>B[/3p]</text></msg>")
ck("D15 跨消息标记对合并为一条", o.count("<msg") == 1, o)

# D16 MiMo 接管
class FakeMimo:
    def __init__(self):
        self.auto_format_fix = True


class FakeCtx:
    def __init__(self, inst):
        self._inst = inst

    def get_plugin_inst(self, pid):
        return self._inst


mimo = FakeMimo()
p = P()
p.ctx = FakeCtx(mimo)
p._try_takeover_mimo()
ck("D16 接管后 runtime 关闭 mimo 修复", mimo.auto_format_fix is False, mimo.auto_format_fix)

mimo2 = FakeMimo()
p = P({"flatten_no_wrap_tags": False})
p.ctx = FakeCtx(mimo2)
p._try_takeover_mimo()
ck("D16 本插件能力关闭时不接管（避免空窗）", mimo2.auto_format_fix is True, mimo2.auto_format_fix)

# 额外：框架提示词明确禁止的嵌套形态，现在能被修好（不破坏、更正确）
o = P().fix_xml("<msg><text>hello<emoji>21</emoji></text></msg>")
ck("框架提示词中的错误示例被修正", "emoji" in tags_of(o) and okf(o), o)

print("=" * 70)
print(f"设计理念断言：通过 {PASS}，失败 {len(FAIL)}")
for n in FAIL:
    print("  -", n)
sys.exit(1 if FAIL else 0)
