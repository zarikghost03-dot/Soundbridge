"""Временная диагностика: какие атрибуты config.Settings/Config требуют модули."""
import glob
import re

attr = re.compile(r"\bsettings\.([a-zA-Z_]+)")
cfgattr = re.compile(r"\b(?:cfg|config)\.([a-zA-Z_]+)")
imp = re.compile(r"^(from \.[\w]+ import [\w, ()]+\S*|import \.[\w]+)", re.M)
cls = re.compile(r"^(class (\w+)[^\n]*|\s*def (\w+)\(([^\n]*)\))")

files = sorted(glob.glob("soundbridge/*.py") + glob.glob("scripts/*.py"))
for f in files:
    src = open(f, encoding="utf-8").read()
    settings_used = sorted(set(attr.findall(src)))
    cfg_used = sorted(set(cfgattr.findall(src)))
    print(f)
    if settings_used:
        print("    settings.*:", ", ".join(settings_used))
    if cfg_used:
        print("    cfg.*     :", ", ".join(cfg_used))

print()
print("=== config.py структура ===")
src = open("soundbridge/config.py", encoding="utf-8").read()
for line in src.splitlines():
    if cls.match(line) or re.match(r"^\w+ = ", line):
        print("   ", line.rstrip())

print()
print("=== кто что импортирует из пакетов ===")
for f in files:
    src = open(f, encoding="utf-8").read()
    found = imp.findall(src)
    local = [x for x in found if " import " in x and ("." in x.split()[1] if len(x.split()) > 1 else False)]
    print(f, "->", "; ".join(local) if local else "-")
