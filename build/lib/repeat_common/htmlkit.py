"""
Building blocks for the self-contained HTML pages that explain a process and show its outputs on a map.

The pages need no server and no internet: the data are embedded as JSON, the map is drawn on a canvas, the diagrams are
inline SVG built from a node and edge list, and the charts are SVG built from the same embedded data. Colours follow the
dataviz method (validated palette: blue / orange / aqua for categories, one blue ramp for magnitude, blue-red diverging for
residuals), with a light and a dark theme.
"""
from __future__ import annotations

import html
import json

import numpy as np

# --------------------------------------------------------------------------------------
# geometry packing (keeps the page small: simplified, rounded, flat coordinate lists)
# --------------------------------------------------------------------------------------


def _r(x, nd=4):
    return round(float(x), nd)


def _rings(geom, tol, nd=4):
    """Polygon or MultiPolygon -> list of polygons, each a list of rings, each a flat [lon, lat, ...] list."""
    g = geom.simplify(tol, preserve_topology=True) if tol else geom
    polys = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
    out = []
    for p in polys:
        if p.is_empty:
            continue
        rs = [list(p.exterior.coords)] + [list(i.coords) for i in p.interiors]
        out.append([[_r(v, nd) for xy in r for v in xy[:2]] for r in rs])
    return out


def pack_polygons(gdf, cols, tol=0.0008):
    """[{'id':..., 'polys': [...], 'p': {col: value}}] for a GeoDataFrame in EPSG:4326."""
    items = []
    for _, r in gdf.iterrows():
        p = {}
        for c in cols:
            v = r.get(c)
            if v is None or (isinstance(v, float) and not np.isfinite(v)):
                p[c] = None
            elif isinstance(v, (np.integer,)):
                p[c] = int(v)
            elif isinstance(v, (np.floating, float)):
                p[c] = round(float(v), 4)
            else:
                p[c] = str(v) if not isinstance(v, (int, bool)) else v
        items.append({"polys": _rings(r.geometry, tol), "p": p})
    return items


def pack_lines(gdf, cls_col=None, cls_map=None, tol=0.0005):
    """[[class, [lon, lat, ...]], ...] for line features."""
    out = []
    for _, r in gdf.iterrows():
        g = r.geometry
        if g is None or g.is_empty:
            continue
        g = g.simplify(tol, preserve_topology=False)
        parts = list(g.geoms) if g.geom_type.startswith("Multi") else [g]
        k = (cls_map or {}).get(str(r.get(cls_col)), 0) if cls_col else 0
        for ln in parts:
            if len(ln.coords) >= 2:
                out.append([k, [_r(v) for xy in ln.coords for v in xy[:2]]])
    return out


def pack_points(gdf, nd=4):
    return [[_r(g.x, nd), _r(g.y, nd)] for g in gdf.geometry if g is not None and not g.is_empty]


def to_json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=lambda o: None if o is None else (float(o) if isinstance(o, (np.floating,)) else int(o) if isinstance(o, np.integer) else str(o))).replace("</", "<\\/")


# --------------------------------------------------------------------------------------
# flow diagram (inline SVG)
# --------------------------------------------------------------------------------------


def flow_svg(nodes: dict, edges: list, width: int, height: int, aria: str, lanes: list | None = None) -> str:
    """Inline SVG of boxes and labelled arrows.

    nodes: id -> {x, y, w, h, title, lines: [str], kind: input|cloud|proc|out|gate|gatefail}
    edges: (src, dst, label[, {'from': 'r'|'l'|'t'|'b', 'to': ..., 'dy': int, 'dyf': int, 'dyt': int, 'lx': int, 'ly': int, 'dashed': bool}])
    lanes: [(x, width, label)] column headings drawn behind the boxes
    """
    def anchor(n, side):
        x, y, w, h = n["x"], n["y"], n["w"], n["h"]
        return {"r": (x + w, y + h / 2), "l": (x, y + h / 2), "t": (x + w / 2, y), "b": (x + w / 2, y + h)}[side]

    def esc(s):
        return html.escape(str(s), quote=True)

    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{esc(aria)}" class="flow" xmlns="http://www.w3.org/2000/svg">',
           '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
           '<path d="M0 0 L10 5 L0 10 z" fill="currentColor"/></marker></defs>']
    for x, w, label in lanes or []:
        out.append(f'<rect x="{x}" y="6" width="{w}" height="{height - 12}" rx="10" class="lane"/>'
                   f'<text x="{x + w / 2}" y="26" text-anchor="middle" class="lane-t">{esc(label)}</text>')
    for e in edges:
        s, d, label = e[0], e[1], e[2]
        o = e[3] if len(e) > 3 else {}
        a = anchor(nodes[s], o.get("from", "r"))
        b = anchor(nodes[d], o.get("to", "l"))
        dy = o.get("dy", 0)                                           # dy moves both ends; dyf / dyt move the start / end only
        a, b = (a[0], a[1] + dy + o.get("dyf", 0)), (b[0], b[1] + dy + o.get("dyt", 0))
        if o.get("from", "r") in ("t", "b") or o.get("to", "l") in ("t", "b"):
            cy = (a[1] + b[1]) / 2
            path = f"M{a[0]:.0f} {a[1]:.0f} C{a[0]:.0f} {cy:.0f} {b[0]:.0f} {cy:.0f} {b[0]:.0f} {b[1]:.0f}"
        else:
            cx = (a[0] + b[0]) / 2
            path = f"M{a[0]:.0f} {a[1]:.0f} C{cx:.0f} {a[1]:.0f} {cx:.0f} {b[1]:.0f} {b[0]:.0f} {b[1]:.0f}"
        out.append(f'<path d="{path}" class="edge{" dashed" if o.get("dashed") else ""}" marker-end="url(#arrow)"/>')
        if label:
            mx, my = (a[0] + b[0]) / 2 + o.get("lx", 0), (a[1] + b[1]) / 2 + o.get("ly", -5)
            out.append(f'<text x="{mx:.0f}" y="{my:.0f}" text-anchor="middle" class="elab" style="stroke:var(--surface)">{esc(label)}</text>')
    for nid, n in nodes.items():
        x, y, w, h = n["x"], n["y"], n["w"], n["h"]
        kind = n.get("kind", "proc")
        out.append(f'<g class="node n-{kind}" data-id="{esc(nid)}" tabindex="0" role="button" aria-label="{esc(n["title"])}">'
                   f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8"/>')
        out.append(f'<text x="{x + 10}" y="{y + 19}" class="nt">{esc(n["title"])}</text>')
        for i, ln in enumerate(n.get("lines", [])[:3]):
            out.append(f'<text x="{x + 10}" y="{y + 35 + i * 14}" class="ns">{esc(ln)}</text>')
        out.append("</g>")
    out.append("</svg>")
    return "".join(out)


# --------------------------------------------------------------------------------------
# page shell, styles, scripts
# --------------------------------------------------------------------------------------

CSS = """
:root{color-scheme:light;--bg:#f9f9f7;--surface:#fcfcfb;--fg:#0b0b0b;--muted:#52514e;--faint:#898781;--grid:#e1e0d9;--axis:#c3c2b7;
--border:rgba(11,11,11,.12);--blue:#2a78d6;--orange:#eb6834;--aqua:#1baf7a;--good:#006300;--bad:#c43232;--warn:#a05f00;--wash:rgba(42,120,214,.08)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;--bg:#0d0d0d;--surface:#1a1a19;--fg:#f0efec;--muted:#c3c2b7;
--faint:#898781;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.12);--blue:#3987e5;--orange:#d95926;--aqua:#199e70;--good:#3cc23c;--bad:#ee6a6a;--warn:#f0a93a;--wash:rgba(57,135,229,.14)}}
:root[data-theme=dark]{color-scheme:dark;--bg:#0d0d0d;--surface:#1a1a19;--fg:#f0efec;--muted:#c3c2b7;--faint:#898781;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.12);--blue:#3987e5;--orange:#d95926;--aqua:#199e70;--good:#3cc23c;--bad:#ee6a6a;--warn:#f0a93a;--wash:rgba(57,135,229,.14)}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--blue)}
header.top{padding:28px 16px 8px;max-width:1240px;margin:0 auto}
header.top h1{margin:0 0 4px;font-size:26px;line-height:1.2}
header.top p{margin:0;color:var(--muted);max-width:78ch}
.facts{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 0;padding:0;list-style:none}
.facts li{background:var(--surface);border:1px solid var(--border);border-radius:999px;padding:3px 12px;font-size:13px;color:var(--muted)}
.facts b{color:var(--fg)}
nav.bar{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 92%,transparent);backdrop-filter:blur(6px);border-bottom:1px solid var(--border)}
nav.bar div{max-width:1240px;margin:0 auto;padding:6px 16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
nav.bar a{text-decoration:none;color:var(--muted);font-size:14px}nav.bar a:hover{color:var(--fg)}
nav.bar button{margin-left:auto}
main{max-width:1240px;margin:0 auto;padding:8px 16px 60px}
section{margin:34px 0}section>h2{font-size:20px;margin:0 0 6px}section>p.lede{margin:0 0 14px;color:var(--muted);max-width:80ch}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px}
.grid2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px}
.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
@media (max-width:900px){.grid2,.grid3{grid-template-columns:minmax(0,1fr)}}
figure{margin:0}figcaption{margin-top:8px;font-size:13px;color:var(--muted);max-width:90ch}
svg.flow{width:100%;height:auto;display:block;color:var(--fg)}
.lane{fill:var(--wash);stroke:none}.lane-t{fill:var(--faint);font-size:12px;letter-spacing:.06em;text-transform:uppercase}
.edge{fill:none;stroke:currentColor;stroke-width:1.4;opacity:.6}.edge.dashed{stroke-dasharray:5 4}
.elab{fill:var(--muted);font-size:11px;stroke-width:4px;paint-order:stroke}
.node{cursor:pointer;outline:none}.node rect{fill:var(--surface);stroke:currentColor;stroke-width:1.2;opacity:1}
.node:hover rect,.node:focus rect,.node.sel rect{stroke:var(--blue);stroke-width:2.4}
.node .nt{fill:var(--fg);font-size:12.5px;font-weight:600}.node .ns{fill:var(--muted);font-size:11px}
.n-input rect{stroke:var(--axis)}.n-cloud rect{stroke:var(--axis);stroke-dasharray:5 3}
.n-proc rect{fill:var(--wash);stroke:var(--blue)}.n-out rect{stroke:var(--orange);stroke-width:1.8}
.n-gate rect{fill:var(--surface);stroke:var(--good);stroke-width:2}.n-gatefail rect{fill:var(--surface);stroke:var(--bad);stroke-width:2.4}
#node-detail{margin-top:10px;min-height:64px}#node-detail h3{margin:0 0 4px;font-size:15px}#node-detail p{margin:3px 0;color:var(--muted)}
#node-detail code,.card code,td code{font:12.5px ui-monospace,Consolas,monospace;background:var(--wash);padding:1px 5px;border-radius:4px}
.exp{display:flex;gap:8px;align-items:center;margin-top:8px;font-size:12.5px;color:var(--muted)}.exp button{padding:2px 10px;font-size:12.5px}
.legend-row{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--muted);margin-top:8px}
.sw{display:inline-block;width:14px;height:10px;border:1.5px solid var(--axis);border-radius:3px;margin-right:5px;vertical-align:-1px}
.sw.d{border-style:dashed}.sw.p{border-color:var(--blue);background:var(--wash)}.sw.o{border-color:var(--orange)}.sw.g{border-color:var(--good)}.sw.f{border-color:var(--bad)}
.maprow{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:14px}
@media (max-width:980px){.maprow{grid-template-columns:minmax(0,1fr)}}
.mapbox{position:relative;border:1px solid var(--border);border-radius:12px;background:var(--surface);overflow:hidden}
canvas.map{display:block;width:100%;height:560px;cursor:grab;touch-action:none}canvas.map:active{cursor:grabbing}
.controls{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;margin-bottom:10px;font-size:13.5px}
.controls label{display:inline-flex;gap:6px;align-items:center;color:var(--muted)}
select,button{font:inherit;background:var(--surface);color:var(--fg);border:1px solid var(--border);border-radius:8px;padding:4px 10px}
button{cursor:pointer}button:hover{border-color:var(--blue)}
.legend{position:absolute;left:12px;bottom:12px;background:color-mix(in srgb,var(--surface) 92%,transparent);border:1px solid var(--border);border-radius:8px;padding:6px 10px;font-size:12px;color:var(--muted);max-width:260px}
.legend .bar{height:10px;width:200px;border-radius:3px;margin:4px 0 2px}.legend .rng{display:flex;justify-content:space-between}
.legend .cat{display:flex;gap:6px;align-items:center;margin:2px 0}.legend .cat i{display:inline-block;width:14px;height:10px;border-radius:2px;border:1px solid var(--axis)}
.hint{position:absolute;right:10px;top:8px;font-size:11.5px;color:var(--faint)}
#tip{position:fixed;pointer-events:none;z-index:20;background:var(--surface);border:1px solid var(--border);box-shadow:0 4px 18px rgba(0,0,0,.18);border-radius:8px;padding:6px 10px;font-size:12.5px;display:none;max-width:280px}
#tip b{color:var(--fg)}#tip span{color:var(--muted)}
.side h3{margin:0 0 6px;font-size:15px}.side table{width:100%;border-collapse:collapse;font-size:12.5px}
.side td{padding:2px 0;border-bottom:1px solid var(--grid)}.side td:last-child{text-align:right;font-variant-numeric:tabular-nums}
table.data{border-collapse:collapse;width:100%;font-size:13px}table.data th,table.data td{padding:4px 8px;border-bottom:1px solid var(--grid);text-align:right;font-variant-numeric:tabular-nums}
table.data th{color:var(--muted);font-weight:600;cursor:default}table.data td:first-child,table.data th:first-child{text-align:left}
.tablewrap{overflow-x:auto}details>summary{cursor:pointer;color:var(--muted);font-size:13px;margin-top:6px}
.chart svg{width:100%;height:auto;display:block;color:var(--fg)}.chart .ax{stroke:var(--axis)}.chart .gr{stroke:var(--grid)}
.chart text{fill:var(--muted);font-size:11px}.chart .ttl{fill:var(--fg);font-size:12.5px;font-weight:600}
.note{border-left:3px solid var(--warn);padding:2px 12px;margin:8px 0;color:var(--muted)}
.note.bad{border-color:var(--bad)}.note.good{border-color:var(--good)}.note b{color:var(--fg)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 14px;font-size:13.5px}.kv div:nth-child(odd){color:var(--muted)}
footer{max-width:1240px;margin:0 auto;padding:0 16px 40px;color:var(--faint);font-size:12.5px}footer code{font-size:12px}
.badge{display:inline-block;border-radius:999px;padding:1px 9px;font-size:12px;border:1px solid currentColor}
.badge.bad{color:var(--bad)}.badge.good{color:var(--good)}.badge.warn{color:var(--warn)}
@media (forced-colors:active){.node rect{stroke:CanvasText}}
"""

JS_COMMON = r"""
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const isDark=()=>{const t=document.documentElement.dataset.theme;return t?t==='dark':matchMedia('(prefers-color-scheme:dark)').matches};
const hex=h=>{h=h.replace('#','');return[0,2,4].map(i=>parseInt(h.substr(i,2),16))};
const mix=(a,b,t)=>{const x=hex(a),y=hex(b);return'rgb('+x.map((v,i)=>Math.round(v+(y[i]-v)*t)).join(',')+')'};
// one-hue blue ramp for magnitude: light to dark on a light page, dark to light on a dark page (more = more contrast with the surface)
const ramp=t=>{t=Math.max(0,Math.min(1,t));return isDark()?mix('#184f95','#cde2fb',t):mix('#cde2fb','#0d366b',t)};
// a second magnitude scale (stations drawn over a blue map) takes the next hue: orange, same light-to-dark logic
const rampO=t=>{t=Math.max(0,Math.min(1,t));return isDark()?mix('#7a2a0c','#fbd9c8',t):mix('#fbd9c8','#7a2a0c',t)};
// diverging blue - neutral - red for signed differences
const div=t=>{t=Math.max(-1,Math.min(1,t));const mid=isDark()?'#383835':'#f0efec';return t<0?mix(mid,isDark()?'#3987e5':'#2a78d6',-t):mix(mid,isDark()?'#e66767':'#e34948',t)};
const fmt=(v,nd=2)=>{if(v==null||!isFinite(v))return'n/a';if(Math.abs(v)>=1000)return Math.round(v).toLocaleString('en');let t=(+v).toFixed(nd);return t.indexOf('.')>=0?t.replace(/\.?0+$/,''):t};
const tip=document.getElementById('tip');
function showTip(html,e){tip.innerHTML=html;tip.style.display='block';const w=tip.offsetWidth,h=tip.offsetHeight;let x=e.clientX+14,y=e.clientY+14;
  if(x+w>innerWidth-8)x=e.clientX-w-14;if(y+h>innerHeight-8)y=e.clientY-h-14;tip.style.left=x+'px';tip.style.top=y+'px'}
function hideTip(){tip.style.display='none'}
document.addEventListener('mouseover',e=>{const t=e.target.closest&&e.target.closest('[data-tip]');if(t)showTip(t.dataset.tip,e)});
document.addEventListener('mousemove',e=>{const t=e.target.closest&&e.target.closest('[data-tip]');if(t)showTip(t.dataset.tip,e)});
document.addEventListener('mouseout',e=>{const t=e.target.closest&&e.target.closest('[data-tip]');if(t)hideTip()});
function toggleTheme(){const r=document.documentElement;r.dataset.theme=isDark()?'light':'dark';window.dispatchEvent(new Event('themechange'))}
// ---- flow diagram: click a box for its details
function wireFlow(details){
  const panel=$('#node-detail');
  const show=id=>{const d=details[id];if(!d)return;$$('.node').forEach(n=>n.classList.toggle('sel',n.dataset.id===id));
    panel.innerHTML='<h3>'+d.title+'</h3>'+d.body.map(p=>'<p>'+p+'</p>').join('')};
  $$('.node').forEach(n=>{n.addEventListener('click',()=>show(n.dataset.id));n.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();show(n.dataset.id)}})});
}
// ---- export the flow diagrams for slides: standalone SVG (light theme, styles inlined) or PNG
const EXPORT_PROPS=['fill','stroke','stroke-width','stroke-dasharray','opacity','font-size','font-weight','letter-spacing','text-transform','text-anchor','paint-order','stroke-linejoin'];
function standaloneSvg(svg,bg){
  const root=document.documentElement,prev=root.dataset.theme;root.dataset.theme='light';        // slides are light: always export the light theme
  const clone=svg.cloneNode(true),src=[svg,...svg.querySelectorAll('*')],dst=[clone,...clone.querySelectorAll('*')];
  src.forEach((el,i)=>{const cs=getComputedStyle(el),d=dst[i];let st='';
    EXPORT_PROPS.forEach(p=>{const v=cs.getPropertyValue(p);if(v)st+=p+':'+v+';'});
    if(el.tagName==='text'||el.tagName==='tspan')st+='font-family:Segoe UI,Helvetica,Arial,sans-serif;';
    d.setAttribute('style',st);d.removeAttribute('class');d.removeAttribute('tabindex');d.removeAttribute('data-id');d.removeAttribute('data-tip');
    d.removeAttribute('role');if(d!==clone)d.removeAttribute('aria-label')});
  clone.setAttribute('xmlns','http://www.w3.org/2000/svg');clone.removeAttribute('class');
  const vb=svg.viewBox.baseVal;clone.setAttribute('width',vb.width);clone.setAttribute('height',vb.height);
  clone.querySelectorAll('[stroke="currentColor"]').forEach(e=>e.setAttribute('stroke',getComputedStyle(svg).color));
  if(bg){const r=document.createElementNS('http://www.w3.org/2000/svg','rect');r.setAttribute('width',vb.width);r.setAttribute('height',vb.height);r.setAttribute('fill',css('--bg'));clone.insertBefore(r,clone.firstChild)}
  if(prev===undefined)delete root.dataset.theme;else root.dataset.theme=prev;
  return new XMLSerializer().serializeToString(clone)}
function download(name,blob){const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;document.body.appendChild(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove()},500)}
function exportPng(svg,name){const xml=standaloneSvg(svg,true),vb=svg.viewBox.baseVal,im=new Image();
  im.onload=()=>{const c=document.createElement('canvas');c.width=vb.width*2;c.height=vb.height*2;c.getContext('2d').drawImage(im,0,0,c.width,c.height);c.toBlob(b=>download(name+'.png',b))};
  im.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(xml)}
function addExports(){$$('figure svg.flow').forEach((svg,i)=>{const sec=svg.closest('section'),name=((sec&&sec.id)||'diagram')+(i?'-'+(i+1):'');
  const row=document.createElement('div');row.className='exp';row.innerHTML='<span>Export for slides:</span>';
  [['SVG','svg',true],['SVG, transparent','svg',false],['PNG','png',true]].forEach(([lab,kind,bg])=>{const b=document.createElement('button');b.type='button';b.textContent=lab;
    b.onclick=()=>kind==='png'?exportPng(svg,name):download(name+'.svg',new Blob([standaloneSvg(svg,bg)],{type:'image/svg+xml'}));row.appendChild(b)});
  svg.closest('figure').after(row)})}
addExports();
// ---- small SVG charts (scatter, bars, series), each with hover tips; a table view is added by the caller
const SV=(w,h,inner)=>'<svg viewBox="0 0 '+w+' '+h+'" role="img">'+inner+'</svg>';
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
function niceTicks(lo,hi,n){const span=hi-lo||1,step0=span/n,mag=Math.pow(10,Math.floor(Math.log10(step0))),f=step0/mag,step=(f<1.5?1:f<3?2:f<7?5:10)*mag;
  const out=[];for(let v=Math.ceil(lo/step)*step;v<=hi+1e-9;v+=step)out.push(+v.toFixed(10));return out}
function chartScatter(el,pts,o){ // pts:[{x,y,tip,fill?}]
  const W=o.w||520,H=o.h||360,m={l:46,r:14,t:30,b:40};const xs=pts.map(p=>p.x),ys=pts.map(p=>p.y);
  const x0=o.x0??Math.min(...xs),x1=o.x1??Math.max(...xs),y0=o.y0??Math.min(...ys),y1=o.y1??Math.max(...ys);
  const X=v=>m.l+(v-x0)/(x1-x0||1)*(W-m.l-m.r),Y=v=>H-m.b-(v-y0)/(y1-y0||1)*(H-m.t-m.b);let s='<text class="ttl" x="'+m.l+'" y="16">'+esc(o.title)+'</text>';
  niceTicks(y0,y1,5).forEach(v=>{s+='<line class="gr" x1="'+m.l+'" x2="'+(W-m.r)+'" y1="'+Y(v)+'" y2="'+Y(v)+'"/><text x="'+(m.l-6)+'" y="'+(Y(v)+4)+'" text-anchor="end">'+fmt(v,1)+'</text>'});
  niceTicks(x0,x1,6).forEach(v=>{s+='<text x="'+X(v)+'" y="'+(H-m.b+15)+'" text-anchor="middle">'+fmt(v,1)+'</text>'});
  s+='<line class="ax" x1="'+m.l+'" x2="'+(W-m.r)+'" y1="'+(H-m.b)+'" y2="'+(H-m.b)+'"/><line class="ax" x1="'+m.l+'" x2="'+m.l+'" y1="'+m.t+'" y2="'+(H-m.b)+'"/>';
  (o.lines||[]).forEach(l=>{s+='<line x1="'+X(l[0])+'" y1="'+Y(l[1])+'" x2="'+X(l[2])+'" y2="'+Y(l[3])+'" stroke="currentColor" stroke-opacity=".35" stroke-dasharray="4 3"/>'});
  pts.forEach(p=>{s+='<circle cx="'+X(p.x)+'" cy="'+Y(p.y)+'" r="'+(o.r||5)+'" fill="'+(p.fill||'var(--blue)')+'" fill-opacity=".85" stroke="var(--surface)" stroke-width="1.5" data-tip="'+esc(p.tip||'')+'"/>'});
  s+='<text x="'+((m.l+W-m.r)/2)+'" y="'+(H-6)+'" text-anchor="middle">'+esc(o.xl||'')+'</text><text transform="translate(12 '+((m.t+H-m.b)/2)+') rotate(-90)" text-anchor="middle">'+esc(o.yl||'')+'</text>';
  el.innerHTML=SV(W,H,s)}
function chartBars(el,rows,o){ // rows:[{label,value,tip,fill?,mark?}] horizontal bars, optional reference lines o.refs=[{v,label}]
  const W=o.w||520,rh=o.rh||24,m={l:o.ml||150,r:46,t:(o.refs&&o.refs.length)?46:30,b:26},H=m.t+m.b+rows.length*rh;const lo=o.lo??Math.min(0,...rows.map(r=>r.value)),hi=o.hi??Math.max(...rows.map(r=>r.value));
  const X=v=>m.l+(v-lo)/(hi-lo||1)*(W-m.l-m.r);let s='<text class="ttl" x="6" y="16">'+esc(o.title)+'</text>';
  niceTicks(lo,hi,5).forEach(v=>{s+='<line class="gr" x1="'+X(v)+'" x2="'+X(v)+'" y1="'+m.t+'" y2="'+(H-m.b)+'"/><text x="'+X(v)+'" y="'+(H-8)+'" text-anchor="middle">'+fmt(v,2)+'</text>'});
  rows.forEach((r,i)=>{const y=m.t+i*rh+4,x0=X(Math.min(0,r.value)),x1=X(Math.max(0,r.value));
    s+='<text x="'+(m.l-8)+'" y="'+(y+rh/2+1)+'" text-anchor="end">'+esc(r.label)+'</text><rect x="'+x0+'" y="'+y+'" width="'+Math.max(2,x1-x0)+'" height="'+(rh-9)+'" rx="3" fill="'+(r.fill||'var(--blue)')+'" data-tip="'+esc(r.tip||'')+'"/>';
    s+='<text x="'+(r.value>=0?x1+5:x0-5)+'" y="'+(y+rh/2+1)+'" text-anchor="'+(r.value>=0?'start':'end')+'">'+fmt(r.value,2)+'</text>'});
  (o.refs||[]).forEach(f=>{s+='<line x1="'+X(f.v)+'" x2="'+X(f.v)+'" y1="'+(m.t-4)+'" y2="'+(H-m.b)+'" stroke="var(--orange)" stroke-width="1.6" stroke-dasharray="5 3"/><text x="'+X(f.v)+'" y="'+(m.t-8)+'" text-anchor="middle" style="fill:var(--orange)">'+esc(f.label)+'</text>'});
  s+='<line class="ax" x1="'+X(0)+'" x2="'+X(0)+'" y1="'+m.t+'" y2="'+(H-m.b)+'"/>';el.innerHTML=SV(W,H,s)}
function chartSeries(el,ser,o){ // ser:[{name,pts:[[x,y]],color,dash?,band?:[[x,lo,hi]]}] x numeric (day index) with o.xlabel(x)
  ser=ser.map(se=>({...se,pts:se.pts.filter(p=>isFinite(p[1])),band:se.band&&se.band.filter(b=>isFinite(b[1])&&isFinite(b[2]))}));   // days without readings leave a gap, not NaN
  const W=o.w||760,H=o.h||330,m={l:46,r:14,t:30,b:30};let xs=[],ys=[];ser.forEach(s=>{s.pts.forEach(p=>{xs.push(p[0]);ys.push(p[1])});(s.band||[]).forEach(b=>{ys.push(b[1],b[2])})});
  const x0=Math.min(...xs),x1=Math.max(...xs),y0=o.y0??Math.min(...ys),y1=o.y1??Math.max(...ys);
  const X=v=>m.l+(v-x0)/(x1-x0||1)*(W-m.l-m.r),Y=v=>m.t+(v-y0)/(y1-y0||1)*(H-m.t-m.b);let s='<text class="ttl" x="'+m.l+'" y="16">'+esc(o.title)+'</text>'; // y axis points down: depth grows downward
  niceTicks(y0,y1,6).forEach(v=>{s+='<line class="gr" x1="'+m.l+'" x2="'+(W-m.r)+'" y1="'+Y(v)+'" y2="'+Y(v)+'"/><text x="'+(m.l-6)+'" y="'+(Y(v)+4)+'" text-anchor="end">'+fmt(v,0)+'</text>'});
  (o.xticks||[]).forEach(t=>{s+='<line class="gr" x1="'+X(t[0])+'" x2="'+X(t[0])+'" y1="'+m.t+'" y2="'+(H-m.b)+'"/><text x="'+X(t[0])+'" y="'+(H-10)+'" text-anchor="middle">'+esc(t[1])+'</text>'});
  (o.refs||[]).forEach(f=>{s+='<line x1="'+m.l+'" x2="'+(W-m.r)+'" y1="'+Y(f.v)+'" y2="'+Y(f.v)+'" stroke="var(--orange)" stroke-width="1.4" stroke-dasharray="5 3"/><text x="'+(W-m.r-4)+'" y="'+(Y(f.v)-4)+'" text-anchor="end" style="fill:var(--orange)">'+esc(f.label)+'</text>'});
  ser.forEach(se=>{ if(se.band){const up=se.band.map(b=>X(b[0])+','+Y(b[1])).join(' '),dn=se.band.slice().reverse().map(b=>X(b[0])+','+Y(b[2])).join(' ');s+='<polygon points="'+up+' '+dn+'" fill="'+se.color+'" fill-opacity=".14"/>'}
    s+='<polyline fill="none" stroke="'+se.color+'" stroke-width="2" '+(se.dash?'stroke-dasharray="'+se.dash+'"':'')+' points="'+se.pts.map(p=>X(p[0])+','+Y(p[1])).join(' ')+'"/>'});
  // hover: a vertical hit strip per x with all series values
  const byX={};ser.forEach(se=>se.pts.forEach(p=>{(byX[p[0]]=byX[p[0]]||[]).push(se.name+': '+fmt(p[1],0)+' cm')}));
  Object.keys(byX).forEach(k=>{const xx=X(+k);s+='<rect x="'+(xx-2)+'" y="'+m.t+'" width="4" height="'+(H-m.t-m.b)+'" fill="transparent" data-tip="'+esc('<b>'+(o.xlabel?o.xlabel(+k):k)+'</b><br>'+byX[k].join('<br>'))+'"/>'});
  s+='<line class="ax" x1="'+m.l+'" x2="'+(W-m.r)+'" y1="'+(H-m.b)+'" y2="'+(H-m.b)+'"/><text transform="translate(12 '+((m.t+H-m.b)/2)+') rotate(-90)" text-anchor="middle">'+esc(o.yl||'')+'</text>';
  let lx=m.l;ser.forEach(se=>{s+='<line x1="'+lx+'" x2="'+(lx+18)+'" y1="'+(H-m.b+0)+'" y2="'+(H-m.b+0)+'" stroke="'+se.color+'" stroke-width="3" transform="translate(0 -'+(H-m.b-m.t-8)+')"/>';
    s+='<text x="'+(lx+22)+'" y="'+(m.t+12)+'" style="fill:var(--fg)">'+esc(se.name)+'</text>';lx+=Math.max(120,se.name.length*6.6+34)});
  el.innerHTML=SV(W,H,s)}
function tableView(el,head,rows){el.innerHTML='<details><summary>Table view</summary><div class="tablewrap"><table class="data"><thead><tr>'+head.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(c=>'<td>'+esc(c)+'</td>').join('')+'</tr>').join('')+'</tbody></table></div></details>'}
"""

JS_MAP = r"""
function MapView(canvas,bounds){
  this.c=canvas;this.layers=[];this.view={k:1,x:0,y:0};this.sel=null;this.hover=null;this.onhover=null;this.onclick=null;
  this.cx=(bounds[0]+bounds[2])/2;this.cy=(bounds[1]+bounds[3])/2;this.cos=Math.cos(this.cy*Math.PI/180);this.bw=(bounds[2]-bounds[0])*this.cos;this.bh=bounds[3]-bounds[1];
  const self=this;let drag=null,moved=false;
  new ResizeObserver(()=>self.resize()).observe(canvas);
  canvas.addEventListener('wheel',e=>{e.preventDefault();const r=canvas.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top,k0=self.view.k,k1=Math.max(.6,Math.min(80,k0*Math.exp(-e.deltaY*.0015)));
    const f=k1/k0;self.view.x=mx-(mx-self.W/2-self.view.x)*f-self.W/2;self.view.y=my-(my-self.H/2-self.view.y)*f-self.H/2;self.view.k=k1;self.draw()},{passive:false});
  canvas.addEventListener('pointerdown',e=>{drag={x:e.clientX,y:e.clientY,vx:self.view.x,vy:self.view.y};moved=false;canvas.setPointerCapture(e.pointerId)});
  canvas.addEventListener('pointermove',e=>{if(drag){const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.abs(dx)+Math.abs(dy)>3)moved=true;self.view.x=drag.vx+dx;self.view.y=drag.vy+dy;self.draw();hideTip();return}
    const r=canvas.getBoundingClientRect(),h=self.pick(e.clientX-r.left,e.clientY-r.top);if(h!==self.hover&&!(h&&self.hover&&h.item===self.hover.item)){self.hover=h;self.draw()}
    if(h&&self.onhover){const t=self.onhover(h);if(t)showTip(t,e);else hideTip()}else hideTip()});
  canvas.addEventListener('pointerup',e=>{const was=drag;drag=null;if(!moved&&was){const r=canvas.getBoundingClientRect(),h=self.pick(e.clientX-r.left,e.clientY-r.top);self.sel=h&&h.layer.selectable?h:null;self.draw();if(self.onclick)self.onclick(self.sel)}});
  canvas.addEventListener('pointerleave',()=>{self.hover=null;hideTip();self.draw()});
  canvas.addEventListener('dblclick',()=>{self.view={k:1,x:0,y:0};self.draw()});
  this.resize();
}
MapView.prototype.resize=function(){const c=this.c,d=devicePixelRatio||1;this.W=c.clientWidth;this.H=c.clientHeight;c.width=this.W*d;c.height=this.H*d;this.d=d;
  this.base=Math.min(this.W/this.bw,this.H/this.bh)*.94;this.draw()};
MapView.prototype.addLayer=function(l){l.visible=l.visible!==false;this.layers.push(l);
  const u=(lon,lat)=>[(lon-this.cx)*this.cos,-(lat-this.cy)];
  if(l.type==='poly')l.items.forEach(it=>{it._r=it.polys.map(p=>p.map(r=>{const o=new Float64Array(r.length);for(let i=0;i<r.length;i+=2){const q=u(r[i],r[i+1]);o[i]=q[0];o[i+1]=q[1]}return o}))});
  else if(l.type==='line')l.items.forEach(it=>{const r=it.pts,o=new Float64Array(r.length);for(let i=0;i<r.length;i+=2){const q=u(r[i],r[i+1]);o[i]=q[0];o[i+1]=q[1]}it._r=o});
  else l.items.forEach(it=>{it._r=u(it.x,it.y)});
  return l};
MapView.prototype.sx=function(ux){return ux*this.base*this.view.k+this.W/2+this.view.x};
MapView.prototype.sy=function(uy){return uy*this.base*this.view.k+this.H/2+this.view.y};
MapView.prototype.draw=function(){const c=this.ctx||(this.ctx=this.c.getContext('2d')),d=this.d||1;c.setTransform(d,0,0,d,0,0);c.clearRect(0,0,this.W,this.H);
  const sx=u=>this.sx(u),sy=u=>this.sy(u);
  for(const l of this.layers){if(!l.visible)continue;
    if(l.type==='poly'){for(const it of l.items){const st=l.style(it,this);c.beginPath();
      for(const poly of it._r)for(const r of poly){c.moveTo(sx(r[0]),sy(r[1]));for(let i=2;i<r.length;i+=2)c.lineTo(sx(r[i]),sy(r[i+1]));c.closePath()}
      if(st.fill){c.fillStyle=st.fill;c.fill('evenodd')}
      const isSel=this.sel&&this.sel.item===it,isHov=this.hover&&this.hover.item===it;c.lineWidth=isSel?3:isHov?2:(st.lw||1);c.strokeStyle=isSel?css('--fg'):(st.stroke||css('--axis'));c.stroke()}}
    else if(l.type==='line'){for(const it of l.items){const st=l.style(it);if(!st)continue;c.beginPath();const r=it._r;c.moveTo(sx(r[0]),sy(r[1]));for(let i=2;i<r.length;i+=2)c.lineTo(sx(r[i]),sy(r[i+1]));
      c.lineWidth=st.lw;c.strokeStyle=st.stroke;c.setLineDash(st.dash||[]);c.stroke();c.setLineDash([])}}
    else{for(const it of l.items){const st=l.style(it);if(!st)continue;const x=sx(it._r[0]),y=sy(it._r[1]);if(x<-10||y<-10||x>this.W+10||y>this.H+10)continue;
      const isSel=this.sel&&this.sel.item===it,isHov=this.hover&&this.hover.item===it;c.beginPath();c.arc(x,y,(st.r||3)+(isSel||isHov?2:0),0,6.2832);
      if(st.fill){c.fillStyle=st.fill;c.fill()}c.lineWidth=st.ring||1;c.strokeStyle=isSel?css('--fg'):(st.stroke||css('--surface'));c.stroke()}}}
};
MapView.prototype.pick=function(mx,my){
  for(let li=this.layers.length-1;li>=0;li--){const l=this.layers[li];if(!l.visible||!l.pickable)continue;
    if(l.type==='point'){let best=null,bd=1e9;for(const it of l.items){const x=this.sx(it._r[0]),y=this.sy(it._r[1]),dd=(x-mx)**2+(y-my)**2,rr=((l.style(it)||{}).r||3)+4;if(dd<rr*rr&&dd<bd){bd=dd;best=it}}if(best)return{layer:l,item:best}}
    else if(l.type==='poly'){for(const it of l.items){for(const poly of it._r){const r=poly[0];let inside=false;for(let i=0,j=r.length-2;i<r.length;j=i,i+=2){const xi=this.sx(r[i]),yi=this.sy(r[i+1]),xj=this.sx(r[j]),yj=this.sy(r[j+1]);
      if(((yi>my)!==(yj>my))&&(mx<(xj-xi)*(my-yi)/(yj-yi)+xi))inside=!inside}if(inside)return{layer:l,item:it}}}}}
  return null};
function legendCont(el,title,lo,hi,fmtf,reverse,diverging,fn){fn=fn||ramp;const stops=[];for(let i=0;i<=8;i++){const t=i/8;stops.push(diverging?div(t*2-1):fn(reverse?1-t:t))}
  el.innerHTML='<div>'+esc(title)+'</div><div class="bar" style="background:linear-gradient(90deg,'+stops.join(',')+')"></div><div class="rng"><span>'+fmtf(lo)+'</span><span>'+fmtf(hi)+'</span></div>'}
function legendCat(el,title,items){el.innerHTML='<div>'+esc(title)+'</div>'+items.map(i=>'<div class="cat"><i style="background:'+i[0]+'"></i>'+esc(i[1])+'</div>').join('')}
"""


def page(title: str, lede: str, facts: list[tuple[str, str]], nav: list[tuple[str, str]], sections: str, data: dict,
         script: str, footer: str = "", extra_css: str = "") -> str:
    facts_html = "".join(f"<li>{html.escape(k)} <b>{html.escape(v)}</b></li>" for k, v in facts)
    nav_html = "".join(f'<a href="#{a}">{html.escape(b)}</a>' for a, b in nav)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}{extra_css}</style></head>
<body>
<header class="top"><h1>{html.escape(title)}</h1><p>{lede}</p><ul class="facts">{facts_html}</ul></header>
<nav class="bar"><div>{nav_html}<button type="button" onclick="toggleTheme()" aria-label="Switch light and dark theme">Theme</button></div></nav>
<main>{sections}</main>
<footer>{footer}</footer>
<div id="tip" role="tooltip"></div>
<script>{JS_COMMON}{JS_MAP}</script>
<script>const DATA={to_json(data)};</script>
<script>{script}</script>
</body></html>
"""
