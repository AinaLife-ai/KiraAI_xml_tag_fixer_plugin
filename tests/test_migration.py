"""配置迁移测试：验证「只迁移一次 + 原子写回 + 不丢配置」。

覆盖：
  M1 老用户（配置里没有该键）      → 迁移为 false + 打标记
  M2 已显式配置过的用户             → 尊重原值，只打标记
  M3 已迁移过（有标记）             → 完全不动（用户改回 true 也不再干预）
  M4 配置文件不存在（全新安装）     → 不创建、不迁移（schema 默认即 false）
  M5 配置文件损坏（非法 JSON）      → 中止，不覆盖
  M6 其它键必须原样保留             → 逐键比对
  M7 写回后配置仍完整可读           → 二次校验
  M8 原子性：写入过程不产生半截文件 → 检查临时文件已清理
  M9 幂等：连跑 5 次结果不变
  M10 迁移后用户手改 true 不被回滚
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, '/tmp/h')   # 本地 harness（提供 core.* 桩模块）
from main import migrate_config_once, _MIGRATION_KEY, _STRIP_KEY, _MIGRATION_VERSION

PASS = 0
FAIL = []


def ck(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def sandbox(initial=None, raw=None):
    d = tempfile.mkdtemp(prefix="xmlfix_mig_")
    path = os.path.join(d, "xml_tag_fixer.json")
    if raw is not None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
    elif initial is not None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(initial, f, ensure_ascii=False, indent=4)
    return d, path


def read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


print("配置迁移测试")
print("=" * 70)

# M1 老用户：有其它键、没有 strip_reasoning_block、没有标记
d, p = sandbox({"enabled": True, "wrap_mode": "blacklist",
                "no_wrap_tags": ["mimo_tts"], "force_wrap_tags": []})
before = read(p)
r = migrate_config_once(p)
after = read(p)
ck("M1 迁移被执行", r is not None, r)
ck("M1 键被设为 false", after.get(_STRIP_KEY) is False, after.get(_STRIP_KEY))
ck("M1 标记已写入", after.get(_MIGRATION_KEY) == _MIGRATION_VERSION, after.get(_MIGRATION_KEY))
ck("M1 其它键全部保留", all(after.get(k) == v for k, v in before.items()),
   f"{before} -> {after}")
ck("M1 键数只增不改", len(after) == len(before) + 2, f"{len(before)} -> {len(after)}")
shutil.rmtree(d)

# M2 已显式配置过的用户
d, p = sandbox({"enabled": True, _STRIP_KEY: True, "wrap_mode": "blacklist"})
r = migrate_config_once(p)
after = read(p)
ck("M2 尊重用户选择（保持 true）", after.get(_STRIP_KEY) is True, after.get(_STRIP_KEY))
ck("M2 标记已写入", after.get(_MIGRATION_KEY) == _MIGRATION_VERSION)
ck("M2 未改动其它键", after.get("wrap_mode") == "blacklist")
shutil.rmtree(d)

# M3 已迁移过 → 完全不动
d, p = sandbox({"enabled": True, _STRIP_KEY: True, _MIGRATION_KEY: _MIGRATION_VERSION})
snapshot = read(p)
r = migrate_config_once(p)
after = read(p)
ck("M3 已迁移过时什么都不做", r is None, r)
ck("M3 文件内容未变", after == snapshot, f"{snapshot} -> {after}")
shutil.rmtree(d)

# M3b 迁移后用户改回 true，再跑迁移不得回滚
d, p = sandbox({"enabled": True, _STRIP_KEY: False, _MIGRATION_KEY: _MIGRATION_VERSION})
cfg = read(p)
cfg[_STRIP_KEY] = True          # 用户手动改回
with open(p, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=4)
migrate_config_once(p)
ck("M3b 用户改回 true 不被回滚", read(p)[_STRIP_KEY] is True, read(p)[_STRIP_KEY])
shutil.rmtree(d)

# M4 不存在 → 不创建
d = tempfile.mkdtemp(prefix="xmlfix_mig_")
p = os.path.join(d, "xml_tag_fixer.json")
r = migrate_config_once(p)
ck("M4 全新安装不迁移", r is None, r)
ck("M4 不创建文件", not os.path.exists(p))
shutil.rmtree(d)

# M5 损坏 JSON → 中止且不覆盖
d, p = sandbox(raw='{"enabled": true, "wrap_mode": BROKEN')
before_raw = open(p, encoding="utf-8").read()
r = migrate_config_once(p)
ck("M5 损坏文件时中止", r is None, r)
ck("M5 损坏文件未被覆盖", open(p, encoding="utf-8").read() == before_raw)
shutil.rmtree(d)

# M8 原子性：无残留临时文件
d, p = sandbox({"enabled": True})
migrate_config_once(p)
leftovers = [f for f in os.listdir(d) if f != "xml_tag_fixer.json"]
ck("M8 无临时文件残留", not leftovers, leftovers)
shutil.rmtree(d)

# M9 幂等
d, p = sandbox({"enabled": True, "wrap_mode": "whitelist"})
migrate_config_once(p)
snap = read(p)
for _ in range(5):
    migrate_config_once(p)
ck("M9 连跑 5 次结果不变（幂等）", read(p) == snap, f"{snap} -> {read(p)}")
shutil.rmtree(d)

# M7 复杂嵌套配置完整保留
complex_cfg = {
    "enabled": True, "wrap_mode": "blacklist",
    "no_wrap_tags": ["mimo_tts", "foo", "<Bar>"],
    "force_wrap_tags": ["x", "y"],
    "text_at_exclude_domains": ["com", "cn", "zz"],
    "convert_text_at_to_tag": False,
    "nested_like": {"a": 1, "b": [1, 2, {"c": 3}]},
    "unicode": "中文测试 🐍",
}
d, p = sandbox(complex_cfg)
migrate_config_once(p)
after = read(p)
ck("M7 复杂配置逐键保留",
   all(after.get(k) == v for k, v in complex_cfg.items()),
   f"{complex_cfg}\n-> {after}")
ck("M7 新增键恰好 2 个", len(after) == len(complex_cfg) + 2)
shutil.rmtree(d)

# M6 写入失败 → 不得崩、不得损坏原文件
# 用「目标路径改成目录」稳定触发写入失败（root 下 chmod 不生效，不可靠）
d, p = sandbox({"enabled": True})
os.unlink(p)
os.mkdir(p)          # 目标变成目录 ⇒ os.replace 必然失败
try:
    r = migrate_config_once(p)
    ck("M6 写入失败时安全返回", r is None, r)
    ck("M6 不抛异常、不损坏", os.path.isdir(p))
finally:
    shutil.rmtree(d)

# M6b 正常路径可迁移（回归确认）
d = tempfile.mkdtemp(prefix="xmlfix_mig_")
p = os.path.join(d, "sub", "xml_tag_fixer.json")
os.makedirs(os.path.dirname(p), exist_ok=True)
with open(p, "w", encoding="utf-8") as f:
    json.dump({"enabled": True}, f)
r = migrate_config_once(p)
ck("M6b 正常路径可迁移", r is not None, r)
ck("M6b 结果正确", read(p).get(_STRIP_KEY) is False, read(p))
shutil.rmtree(d)

print("=" * 70)
print(f"迁移断言：通过 {PASS}，失败 {len(FAIL)}")
for n in FAIL:
    print("  -", n)
sys.exit(1 if FAIL else 0)
