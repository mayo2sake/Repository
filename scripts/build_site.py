import argparse
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import List, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.e-typing.ne.jp/ranking/"
LIST_URL = urljoin(BASE_URL, "ranking_list.asp?im=0")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"

@dataclass
class RankingInfo:
    period_key: str
    label: str
    no: str
    url: str


def fetch_text(session, url, method="get", data=None):
    res = session.post(url, data=data, timeout=30) if method == "post" else session.get(url, timeout=30)
    res.raise_for_status()
    res.encoding = res.apparent_encoding or "utf-8"
    return res.text


def parse_ranking_list(html: str) -> List[RankingInfo]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for i, link in enumerate(soup.select("ul.ranking a[href]")):
        href = link.get("href", "")
        m = re.search(r"[?&]no=(\d+)", href)
        if not m:
            continue
        key = "last_week" if i == 0 else "week_before" if i == 1 else f"history_{i + 1}"
        label = "先週" if i == 0 else "先々週" if i == 1 else f"過去{i + 1}"
        out.append(RankingInfo(key, label, m.group(1), urljoin(BASE_URL, href)))
    return out


def parse_page(html: str) -> Tuple[str, int, List[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    section = soup.select_one("section#ranking")
    title = section.select_one("h1").get_text(" ", strip=True) if section else ""
    m = re.search(r"ps_page_max\s*=\s*(\d+)", html)
    page_max = int(m.group(1)) if m else 1
    rows = []
    for item in soup.select("ul.ranking > li"):
        if "head" in item.get("class", []):
            continue
        rank = item.select_one(".rank")
        user = item.select_one(".user")
        score = item.select_one(".score")
        if not (rank and user and score):
            continue
        rank_text = rank.get_text(strip=True)
        rows.append({
            "rank": int(re.sub(r"\D", "", rank_text) or "0"),
            "rank_text": rank_text,
            "user": user.get_text(" ", strip=True),
            "score": int(re.sub(r"\D", "", score.get_text(strip=True)) or "0"),
        })
    return title, page_max, rows


def scrape(info: RankingInfo, wait: float):
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    title, page_max, rows = parse_page(fetch_text(session, info.url))
    for page in range(2, page_max + 1):
        _, _, page_rows = parse_page(fetch_text(session, urljoin(BASE_URL, "trysc.asp"), "post", {"f_pg": str(page), "f_pg_sz": ""}))
        rows.extend(page_rows)
        if wait:
            time.sleep(wait)
    summary = {
        "label": info.label,
        "title": title,
        "no": info.no,
        "source_url": info.url,
        "page_count": page_max,
        "row_count": len(rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return summary, rows


def build_stats(rows):
    scores = [r["score"] for r in rows]
    n = len(scores)
    avg = sum(scores) / n if n else 0.0
    std = math.sqrt(sum((s - avg) ** 2 for s in scores) / n) if n else 0.0
    return {
        "participant_count": n,
        "average": avg,
        "stddev": std,
        "hensachi_60": avg + std,
        "hensachi_70": avg + std * 2,
        "hensachi_80": avg + std * 3,
        "hensachi_90": avg + std * 4,
        "max_score": max(scores) if scores else 0,
        "min_score": min(scores) if scores else 0,
    }


def distribution(rows, width=50):
    scores = [r["score"] for r in rows]
    if not scores:
        return []
    start = min(scores) // width * width
    end = (max(scores) // width + 1) * width
    return [{"label": f"{a}-{a + width - 1}", "count": sum(1 for s in scores if a <= s <= a + width - 1)} for a in range(start, end, width)]


def bar_svg(labels, values, color="#2f80ed"):
    w, h = 860, 360
    left, right, top, bottom = 52, 24, 20, 72
    cw, ch = w - left - right, h - top - bottom
    mv = max(values) if values else 1
    step = cw / max(len(values), 1)
    bw = max(6, step * 0.68)
    parts = [f'<svg class="chart-svg" viewBox="0 0 {w} {h}" role="img"><line x1="{left}" y1="{top+ch}" x2="{w-right}" y2="{top+ch}" class="axis"/><line x1="{left}" y1="{top}" x2="{left}" y2="{top+ch}" class="axis"/><text x="{left-8}" y="{top+12}" text-anchor="end">{int(mv)}</text>']
    for i, v in enumerate(values):
        bh = 0 if mv == 0 else ch * v / mv
        x = left + i * step + (step - bw) / 2
        y = top + ch - bh
        lab = escape(labels[i])
        parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{bw:.2f}" height="{bh:.2f}" fill="{color}"><title>{lab}: {v}</title></rect>')
        if len(values) <= 24:
            parts.append(f'<text x="{x+bw/2:.2f}" y="{h-34}" text-anchor="end" transform="rotate(-45 {x+bw/2:.2f} {h-34})">{lab}</text>')
    return "".join(parts) + "</svg>"


def line_svg(manifest):
    ordered = list(reversed(manifest))
    if not ordered:
        return '<div class="empty">データがありません</div>'
    w, h = 860, 360
    left, right, top, bottom = 52, 28, 28, 56
    cw, ch = w - left - right, h - top - bottom
    avg = [x["stats"]["average"] for x in ordered]
    h70 = [x["stats"]["hensachi_70"] for x in ordered]
    mn, mx = min(avg + h70), max(avg + h70)
    if mn == mx:
        mn -= 1; mx += 1
    def pts(vals):
        denom = max(len(vals) - 1, 1)
        return " ".join(f"{left + cw*i/denom:.2f},{top + ch - ch*(v-mn)/(mx-mn):.2f}" for i, v in enumerate(vals))
    labels = []
    for i, item in enumerate(ordered):
        if len(ordered) <= 12 or i in (0, len(ordered) - 1):
            x = left + cw * i / max(len(ordered) - 1, 1)
            labels.append(f'<text x="{x:.2f}" y="{h-22}" text-anchor="middle">第{escape(item["no"])}回</text>')
    return f'<svg class="chart-svg" viewBox="0 0 {w} {h}" role="img"><line x1="{left}" y1="{top+ch}" x2="{w-right}" y2="{top+ch}" class="axis"/><line x1="{left}" y1="{top}" x2="{left}" y2="{top+ch}" class="axis"/><text x="{left-8}" y="{top+12}" text-anchor="end">{mx:.0f}</text><text x="{left-8}" y="{top+ch}" text-anchor="end">{mn:.0f}</text><polyline points="{pts(avg)}" fill="none" stroke="#2f80ed" stroke-width="3"/><polyline points="{pts(h70)}" fill="none" stroke="#dc2626" stroke-width="3"/><circle cx="{left}" cy="14" r="5" fill="#2f80ed"/><text x="{left+10}" y="18">平均</text><circle cx="{left+72}" cy="14" r="5" fill="#dc2626"/><text x="{left+82}" y="18">偏差値70</text>{''.join(labels)}</svg>'


def stat_cards(stats):
    items = [
        ("参加人数", f"{stats['participant_count']:,}", ""),
        ("平均", f"{stats['average']:.2f}", ""),
        ("標準偏差", f"{stats['stddev']:.2f}", ""),
        ("最高スコア", f"{stats['max_score']:,}", ""),
        ("偏差値60", f"{stats['hensachi_60']:.2f}", " stat-break"),
        ("偏差値70", f"{stats['hensachi_70']:.2f}", ""),
        ("偏差値80", f"{stats['hensachi_80']:.2f}", ""),
        ("偏差値90", f"{stats['hensachi_90']:.2f}", ""),
    ]
    return "\n".join(f'<div class="stat{cls}"><span>{label}</span><strong>{value}</strong></div>' for label, value, cls in items)


def page(title, body, root="."):
    return f'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)}</title><link rel="stylesheet" href="{root}/assets/style.css"></head>
<body><header class="site-header"><a class="brand" href="{root}/index.html">e-typing 腕試し統計</a></header><main>{body}</main></body></html>
'''


def ranking_html(summary, rows):
    stats = build_stats(rows)
    dist = distribution(rows)
    svg = bar_svg([d["label"] for d in dist], [d["count"] for d in dist])
    top = rows[:20]
    table = "".join(f'<tr><td>{escape(r["rank_text"])}</td><td>{escape(str(r["user"]))}</td><td>{r["score"]}</td></tr>' for r in top)
    body = f'''<section class="hero"><p class="eyebrow">第{escape(summary['no'])}回</p><h1>{escape(summary['title'])}</h1></section>
<section class="stats-grid">{stat_cards(stats)}</section>
<section class="chart-grid single"><article class="panel"><h2>スコア分布</h2>{svg}</article></section>
<section class="panel"><h2>上位一覧</h2><table><thead><tr><th>順位</th><th>ユーザー名</th><th>スコア</th></tr></thead><tbody>{table}</tbody></table></section>'''
    return page(summary["title"], body, "..")


def index_html(manifest):
    latest = manifest[0] if manifest else None
    latest_block = ""
    if latest:
        latest_block = f'''<section class="hero"><p class="eyebrow">最新公開</p><h1>{escape(latest['title'])}</h1><div class="actions"><a class="button" href="rankings/{escape(latest['no'])}.html">詳細を見る</a></div></section>
<section class="stats-grid">{stat_cards(latest['stats'])}</section>'''
    rows = "".join(f'<tr><td>第{escape(x["no"])}回</td><td><a href="rankings/{escape(x["no"])}.html">{escape(x["title"])}</a></td><td>{x["stats"]["average"]:.2f}</td><td>{x["stats"]["stddev"]:.2f}</td></tr>' for x in manifest)
    body = f'''{latest_block}<section class="panel"><h2>平均スコアの推移</h2>{line_svg(manifest)}</section>
<section class="panel"><h2>公開履歴</h2><table><thead><tr><th>回</th><th>ランキング</th><th>平均</th><th>標準偏差</th></tr></thead><tbody>{rows}</tbody></table></section>'''
    return page("e-typing 腕試し統計", body, ".")


def write_assets(docs_dir):
    css = '''
:root { color-scheme: light; --bg:#f6f8fb; --panel:#fff; --text:#172033; --muted:#5c677d; --line:#dfe5ee; --blue:#2f80ed; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
.site-header { height:56px; display:flex; align-items:center; padding:0 24px; background:#fff; border-bottom:1px solid var(--line); }
.brand { color:var(--text); font-weight:700; text-decoration:none; }
main { width:min(1120px, calc(100% - 32px)); margin:28px auto 56px; }
.hero { margin-bottom:20px; }
.eyebrow { margin:0 0 6px; color:var(--blue); font-weight:700; }
h1 { margin:0; font-size:clamp(24px,4vw,40px); letter-spacing:0; }
h2 { margin:0 0 16px; font-size:18px; }
.actions { margin-top:16px; }
.button { display:inline-flex; align-items:center; min-height:40px; padding:0 14px; background:var(--blue); color:white; text-decoration:none; border-radius:6px; font-weight:700; }
.stats-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin:20px 0; }
.stat,.panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; }
.stat { padding:14px; }
.stat-break { grid-column-start:1; }
.stat span { display:block; color:var(--muted); font-size:13px; }
.stat strong { display:block; margin-top:6px; font-size:24px; }
.chart-grid { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:16px; margin-bottom:16px; }
.chart-grid.single { grid-template-columns:1fr; }
.panel { padding:18px; margin-bottom:16px; }
.chart-svg { display:block; width:100%; height:auto; overflow:visible; }
.chart-svg text { fill:var(--muted); font-size:12px; }
.axis { stroke:var(--line); stroke-width:1; }
table { width:100%; border-collapse:collapse; }
th,td { padding:10px 8px; border-bottom:1px solid var(--line); text-align:left; }
th { color:var(--muted); font-size:13px; }
@media (max-width:760px) { .stats-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .stat-break { grid-column-start:auto; } .chart-grid { grid-template-columns:1fr; } .site-header { padding:0 16px; } }
'''
    p = docs_dir / "assets" / "style.css"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(css, encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert(manifest, item):
    by_no = {x["no"]: x for x in manifest}
    by_no[item["no"]] = item
    return sorted(by_no.values(), key=lambda x: int(x["no"]), reverse=True)


def build(period, docs_dir, wait):
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    choices = {x.period_key: x for x in parse_ranking_list(fetch_text(session, LIST_URL))[:2]}
    info = choices[period]
    summary, rows = scrape(info, wait)
    stats = build_stats(rows)
    write_json(docs_dir / "data" / f"{summary['no']}.json", {"summary": summary, "stats": stats, "rows": rows})
    write_assets(docs_dir)
    manifest_path = docs_dir / "data" / "rankings.json"
    manifest = upsert(read_json(manifest_path), {"no": summary["no"], "title": summary["title"], "source_url": summary["source_url"], "row_count": summary["row_count"], "page_count": summary["page_count"], "generated_at": summary["generated_at"], "stats": stats})
    write_json(manifest_path, manifest)
    out = docs_dir / "rankings" / f"{summary['no']}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(ranking_html(summary, rows), encoding="utf-8")
    (docs_dir / "index.html").write_text(index_html(manifest), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Build the e-typing ranking GitHub Pages site.")
    parser.add_argument("--period", choices=["last_week", "week_before"], default="last_week")
    parser.add_argument("--docs-dir", default="docs")
    parser.add_argument("--wait", type=float, default=0.1)
    args = parser.parse_args()
    build(args.period, Path(args.docs_dir), args.wait)

if __name__ == "__main__":
    main()
