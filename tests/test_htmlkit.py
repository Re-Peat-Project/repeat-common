"""The page scripts run in a browser; this checks the number formatter in them with Node, which is where a real bug was found
(whole numbers lost their trailing zeros: 90 showed as 9).

    python -m tests.test_htmlkit      (needs node; skipped if it is not installed)
"""
import re
import shutil
import subprocess

from repeat_common.htmlkit import JS_COMMON


def test_fmt_keeps_whole_numbers():
    node = shutil.which("node")
    if not node:
        print("skipped: node not installed")
        return
    line = next(ln for ln in JS_COMMON.splitlines() if ln.startswith("const fmt="))
    code = line + """
const cases=[[90,0,'90'],[70,0,'70'],[180,0,'180'],[40,0,'40'],[0,0,'0'],[100,0,'100'],[0.5,2,'0.5'],[2.50,2,'2.5'],[3.14159,2,'3.14'],[12345.6,2,'12,346'],[null,2,'n/a'],[NaN,2,'n/a'],[-0.25,2,'-0.25'],[20,1,'20']];
let bad=cases.filter(c=>fmt(c[0],c[1])!==c[2]).map(c=>c[0]+' -> '+fmt(c[0],c[1])+' (wanted '+c[2]+')');
if(bad.length){console.log(bad.join('; '));process.exit(1)}console.log('ok');"""
    r = subprocess.run([node, "-e", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


if __name__ == "__main__":
    test_fmt_keeps_whole_numbers()
    print("ok test_fmt_keeps_whole_numbers")
