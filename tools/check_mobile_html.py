from pathlib import Path
import tempfile
p = Path(r"D:\mil-ai\yehorivka-vehicle-passability.html")
t = p.read_text(encoding="utf-8")
s = t.index("<script>") + 8
e = t.index("</script>")
js = t[s:e]
out = Path(tempfile.gettempdir()) / "yehorivka_mobile.js"
out.write_text(js, encoding="utf-8")
print("js chars", len(js))
for needle in ("function showTip", "addEventListener('pointerdown'", "mInspect", "zoomCtl", "pinnedTip", "function setMode"):
    print(needle, t.count(needle))
print("js", out)
maps = Path(r"D:\mil-ai\android\YehorivkaMap\app\src\main\assets\maps").glob("*.html")
print("map_pages", len(list(maps)))
