import py_compile, os, sys
root = r"D:\AI\workspace\gptplus-simulator\src"
ok = True
for f in os.listdir(root):
    if f.endswith(".py"):
        p = os.path.join(root, f)
        try:
            py_compile.compile(p, doraise=True)
            print(f"OK   {f}")
        except py_compile.PyCompileError as e:
            print(f"FAIL {f}: {e}")
            ok = False
sys.exit(0 if ok else 1)
