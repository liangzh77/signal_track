from __future__ import annotations

import html
import json
from datetime import datetime

from .analytics import project_performance
from .db import Repository


def render_dashboard(repo: Repository) -> str:
    projects = repo.list_project_rows()
    checks = repo.list_daily_checks(limit=20)
    publish_events = repo.list_publish_events(limit=1)
    last_publish = publish_events[0] if publish_events else None
    performances = {int(row["id"]): project_performance(repo, int(row["id"])) for row in projects}
    active = sum(1 for row in projects if row["status"] in {"active", "needs_review"})
    exits = sum(1 for row in projects if row["status"] == "exit_signal")
    needs_review = sum(1 for row in projects if row["needs_review"])
    returns = [perf.return_pct for perf in performances.values() if perf.return_pct is not None]
    avg_return = sum(returns) / len(returns) if returns else None
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    project_rows = "\n".join(render_project_row(row, performances[int(row["id"])]) for row in projects) or (
        "<tr><td colspan='8' class='empty'>暂无跟踪项目</td></tr>"
    )
    source_cards = render_source_summary(projects, performances)
    source_filter = render_source_filter(projects)
    detail_cards = "\n".join(render_project_detail(repo, row, performances[int(row["id"])]) for row in projects)
    check_items = "\n".join(
        render_check_item(row)
        for row in checks
    ) or "<li class='empty'>暂无检查记录</li>"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Signal Track 投资信号看板</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0D0F0E;
      --surface: rgba(245,247,244,.055);
      --surface-raised: rgba(245,247,244,.085);
      --border: rgba(231,238,232,.14);
      --border-strong: rgba(231,238,232,.24);
      --text: #F1F5EF;
      --muted: #AEB9B0;
      --faint: #727D75;
      --cyan: #44D7C8;
      --amber: #D8B35D;
      --green: #58D68D;
      --red: #FF6B6B;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background:
        linear-gradient(rgba(255,255,255,.025) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,.025) 1px, transparent 1px),
        var(--bg);
      background-size: 32px 32px;
      color: var(--text);
      font-family: Geist, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .shell {{ max-width: 1440px; margin: 0 auto; padding: 24px; }}
    .topbar {{
      display: flex; align-items: end; justify-content: space-between; gap: 16px;
      padding: 18px 0 22px;
    }}
    h1 {{ margin: 0; font-size: 28px; line-height: 36px; letter-spacing: 0; }}
    .stamp {{ color: var(--muted); font-size: 13px; }}
    .metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }}
    .source-summary {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }}
    .source-filter {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 16px; }}
    .source-chip {{ color: var(--muted); background: rgba(245,247,244,.055); border: 1px solid var(--border); border-radius: 999px; min-height: 32px; padding: 0 12px; cursor: pointer; font: inherit; }}
    .source-chip:hover, .source-chip.active {{ color: var(--cyan); border-color: rgba(68,215,200,.55); background: rgba(68,215,200,.08); }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      box-shadow: 0 1px 0 rgba(255,255,255,.06) inset, 0 16px 48px rgba(0,0,0,.24);
      backdrop-filter: blur(18px);
    }}
    .metric {{ padding: 16px; }}
    .metric span {{ color: var(--muted); font-size: 12px; }}
    .metric strong {{ display: block; margin-top: 8px; font-size: 28px; line-height: 32px; font-variant-numeric: tabular-nums; }}
    .source-card {{ padding: 14px; display: grid; gap: 10px; }}
    .source-card h3 {{ margin: 0; font-size: 14px; line-height: 20px; }}
    .source-card-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }}
    .source-stat span {{ color: var(--muted); display: block; font-size: 11px; line-height: 16px; }}
    .source-stat strong {{ display: block; font-size: 16px; line-height: 22px; font-variant-numeric: tabular-nums; }}
    .grid {{ display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 16px; align-items: start; }}
    .details {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin-top: 16px; }}
    .panel {{ overflow: hidden; }}
    .panel-header {{ display: flex; justify-content: space-between; align-items: center; padding: 14px 16px; border-bottom: 1px solid var(--border); }}
    .panel-header h2 {{ margin: 0; font-size: 18px; line-height: 26px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 11px 12px; border-bottom: 1px solid rgba(231,238,232,.08); text-align: left; vertical-align: middle; }}
    th {{ color: var(--muted); font-weight: 600; position: sticky; top: 0; background: rgba(13,15,14,.92); }}
    td.num {{ text-align: right; font-variant-numeric: tabular-nums; font-family: "IBM Plex Mono", "Geist Mono", monospace; }}
    .symbol {{ color: var(--cyan); font-family: "IBM Plex Mono", "Geist Mono", monospace; }}
    .positive {{ color: var(--green); }}
    .negative {{ color: var(--red); }}
    .muted {{ color: var(--muted); }}
    .pill {{ display: inline-flex; align-items: center; height: 22px; padding: 0 8px; border: 1px solid var(--border-strong); border-radius: 999px; font-size: 12px; }}
    .pill.active {{ color: var(--cyan); border-color: rgba(68,215,200,.55); }}
    .pill.needs_review {{ color: var(--amber); border-color: rgba(216,179,93,.55); background: rgba(216,179,93,.08); }}
    .pill.exit_signal {{ color: var(--red); border-color: rgba(255,107,107,.6); background: rgba(255,107,107,.1); }}
    .rail {{ padding: 14px 16px; }}
    .rail ul {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 10px; }}
    .rail li {{ display: grid; gap: 4px; padding: 10px 0; border-bottom: 1px solid rgba(231,238,232,.08); }}
    .rail span, .rail em {{ color: var(--muted); font-size: 12px; font-style: normal; }}
    .rule-hit {{ color: var(--amber); font-size: 12px; line-height: 18px; }}
    .empty {{ color: var(--faint); padding: 20px; }}
    .detail-card {{ padding: 16px; }}
    .detail-top {{ display: flex; justify-content: space-between; gap: 12px; align-items: start; margin-bottom: 12px; }}
    .detail-top h3 {{ margin: 0; font-size: 15px; line-height: 22px; }}
    .chart {{ width: 100%; height: 120px; margin: 8px 0 14px; border: 1px solid rgba(231,238,232,.08); border-radius: 8px; background: rgba(255,255,255,.025); }}
    .logic-grid {{ display: grid; gap: 10px; }}
    .logic-block {{ border-left: 2px solid rgba(68,215,200,.45); padding-left: 10px; color: var(--muted); font-size: 13px; line-height: 20px; }}
    .logic-block.system_logic {{ border-left-color: rgba(216,179,93,.65); }}
    .logic-evidence {{ display: grid; gap: 5px; margin-top: 8px; padding-top: 8px; border-top: 1px solid rgba(231,238,232,.08); }}
    .logic-evidence span {{ color: var(--faint); font-size: 11px; line-height: 16px; text-transform: uppercase; }}
    .logic-evidence-item {{ color: var(--amber); font-size: 12px; line-height: 18px; }}
    .leg-list {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0; }}
    .leg {{ border: 1px solid var(--border); border-radius: 999px; padding: 4px 8px; color: var(--muted); font-size: 12px; }}
    .leg-curves {{ display: grid; gap: 10px; margin: 10px 0 14px; }}
    .leg-curve {{ border: 1px solid rgba(231,238,232,.08); border-radius: 8px; padding: 10px; background: rgba(255,255,255,.022); }}
    .leg-curve-head {{ display: flex; justify-content: space-between; gap: 10px; align-items: center; margin-bottom: 8px; }}
    .leg-curve-head strong {{ font-size: 12px; line-height: 18px; }}
    .mini-chart {{ width: 100%; height: 72px; margin: 0; border: 0; border-radius: 6px; background: rgba(255,255,255,.025); }}
    .check-log {{ display: grid; gap: 8px; margin: 10px 0 14px; }}
    .check-log h4 {{ margin: 0; font-size: 12px; line-height: 18px; color: var(--muted); }}
    .check-log-item {{ border: 1px solid rgba(231,238,232,.08); border-radius: 8px; padding: 10px; background: rgba(255,255,255,.018); }}
    .check-log-top {{ display: flex; justify-content: space-between; gap: 10px; margin-bottom: 5px; font-size: 12px; }}
    .check-log-summary {{ color: var(--muted); font-size: 12px; line-height: 18px; }}
    .research-items {{ display: grid; gap: 8px; margin: 10px 0 14px; }}
    .research-items h4 {{ margin: 0; font-size: 12px; line-height: 18px; color: var(--muted); }}
    .research-item {{ display: grid; grid-template-columns: 120px 1fr 88px; gap: 8px; align-items: start; border: 1px solid rgba(231,238,232,.08); border-radius: 8px; padding: 9px 10px; background: rgba(255,255,255,.018); font-size: 12px; line-height: 18px; }}
    .research-item strong {{ color: var(--cyan); font-weight: 600; }}
    .research-item span {{ color: var(--muted); }}
    .research-item em {{ color: var(--amber); font-style: normal; text-align: right; }}
    @media (max-width: 900px) {{
      .shell {{ padding: 16px; }}
      .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .source-summary {{ grid-template-columns: 1fr; }}
      .grid {{ grid-template-columns: 1fr; }}
      .details {{ grid-template-columns: 1fr; }}
      .topbar {{ align-items: start; flex-direction: column; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="topbar">
      <div>
        <h1>Signal Track 投资信号看板</h1>
        <div class="stamp">最后生成：{escape(now)}</div>
      </div>
      <div class="stamp">{render_publish_stamp(last_publish)}</div>
    </section>
    <section class="source-filter" data-source-filter>{source_filter}</section>
    <section class="metrics">
      <div class="card metric"><span>全部项目</span><strong>{len(projects)}</strong></div>
      <div class="card metric"><span>活跃/复核</span><strong>{active}</strong></div>
      <div class="card metric"><span>平仓信号</span><strong>{exits}</strong></div>
      <div class="card metric"><span>平均收益</span><strong>{format_return(avg_return)}</strong></div>
    </section>
    <section class="source-summary">{source_cards}</section>
    <section class="grid">
      <div class="card panel">
        <div class="panel-header"><h2>跟踪项目</h2><span class="muted">按更新时间排序</span></div>
        <table>
          <thead><tr><th>状态</th><th>信息源</th><th>项目</th><th>标的</th><th>方向</th><th>逻辑分</th><th>收益</th><th>复核</th></tr></thead>
          <tbody>{project_rows}</tbody>
        </table>
      </div>
      <aside class="card rail">
        <div class="panel-header"><h2>每日检查</h2></div>
        <ul>{check_items}</ul>
      </aside>
    </section>
    <section class="details">{detail_cards}</section>
  </main>
  <script>
    (() => {{
      const chips = Array.from(document.querySelectorAll('[data-source-filter] button'));
      const rows = Array.from(document.querySelectorAll('tr[data-source]'));
      const cards = Array.from(document.querySelectorAll('article.detail-card[data-source]'));
      const setFilter = (source) => {{
        chips.forEach((chip) => chip.classList.toggle('active', chip.dataset.source === source));
        [...rows, ...cards].forEach((node) => {{
          node.hidden = source !== 'all' && node.dataset.source !== source;
        }});
      }};
      chips.forEach((chip) => chip.addEventListener('click', () => setFilter(chip.dataset.source || 'all')));
      setFilter('all');
    }})();
  </script>
</body>
</html>"""


def render_source_filter(projects) -> str:
    sources = sorted({str(row["source_name"] or "manual") for row in projects})
    buttons = [
        "<button type='button' class='source-chip active' data-source='all'>全部</button>"
    ]
    buttons.extend(
        f"<button type='button' class='source-chip' data-source='{escape(source)}'>{escape(source)}</button>"
        for source in sources
    )
    return "".join(buttons)


def render_source_summary(projects, performances: dict[int, object]) -> str:
    if not projects:
        return "<div class='card source-card empty'>暂无信息源统计</div>"
    grouped: dict[str, dict[str, object]] = {}
    for row in projects:
        source = str(row["source_name"] or "manual")
        group = grouped.setdefault(
            source,
            {"count": 0, "active": 0, "exits": 0, "needs_review": 0, "returns": []},
        )
        group["count"] = int(group["count"]) + 1
        if row["status"] in {"active", "needs_review"}:
            group["active"] = int(group["active"]) + 1
        if row["status"] == "exit_signal":
            group["exits"] = int(group["exits"]) + 1
        if row["needs_review"]:
            group["needs_review"] = int(group["needs_review"]) + 1
        performance = performances.get(int(row["id"]))
        if performance and performance.return_pct is not None:
            group["returns"].append(performance.return_pct)

    cards = []
    for source, group in sorted(grouped.items(), key=lambda item: (-int(item[1]["count"]), item[0])):
        returns = group["returns"]
        avg_return = sum(returns) / len(returns) if returns else None
        cards.append(
            "<article class='card source-card'>"
            f"<h3>{escape(source)}</h3>"
            "<div class='source-card-grid'>"
            f"<div class='source-stat'><span>项目</span><strong>{group['count']}</strong></div>"
            f"<div class='source-stat'><span>活跃</span><strong>{group['active']}</strong></div>"
            f"<div class='source-stat'><span>信号</span><strong>{group['exits']}</strong></div>"
            f"<div class='source-stat'><span>均值</span><strong class='{return_css(avg_return)}'>{format_return(avg_return)}</strong></div>"
            "</div>"
            f"<div class='muted'>待复核 {group['needs_review']}</div>"
            "</article>"
        )
    return "\n".join(cards)


def render_project_row(row, performance) -> str:
    status = escape(row["status"])
    review = "是" if row["needs_review"] else "否"
    return_class = return_css(performance.return_pct)
    return (
        f"<tr data-source='{escape(row['source_name'])}'>"
        f"<td><span class='pill {status}'>{status}</span></td>"
        f"<td>{escape(row['source_name'])}</td>"
        f"<td>{escape(row['title'])}</td>"
        f"<td><span class='symbol'>{escape(row['symbols'] or '')}</span><br><span class='muted'>{escape(row['instrument_names'] or '')}</span></td>"
        f"<td>{escape(row['direction'])}</td>"
        f"<td class='num'>{float(row['logic_score']):.1f}</td>"
        f"<td class='num {return_class}'>{format_return(performance.return_pct)}</td>"
        f"<td>{review}</td>"
        "</tr>"
    )


def render_check_item(row) -> str:
    rules = json.loads(row["triggered_rules"] or "[]")
    rule_html = "".join(f"<div class='rule-hit'>{escape(rule)}</div>" for rule in rules)
    return (
        "<li>"
        f"<span>{escape(row['check_date'])}</span>"
        f"<strong>{escape(row['title'])}</strong>"
        f"<em>{escape(row['conclusion'])}</em>"
        f"{rule_html}"
        "</li>"
    )


def render_project_detail(repo: Repository, row, performance) -> str:
    logic_blocks = repo.list_logic_blocks(int(row["id"]))
    logic_html = "\n".join(render_logic_block(block) for block in logic_blocks)
    check_log = render_project_check_log(repo.list_daily_checks(project_id=int(row["id"]), limit=5))
    research_items = render_research_items(repo.list_research_items(project_id=int(row["id"]), limit=8))
    legs = "\n".join(
        f"<span class='leg'>{escape(leg.symbol)} · {leg.weight:.0%} · {format_return(leg.return_pct)}</span>"
        for leg in performance.legs
    )
    leg_curves = "\n".join(render_leg_curve(leg) for leg in performance.legs)
    return (
        f"<article class='card detail-card' data-source='{escape(row['source_name'])}'>"
        "<div class='detail-top'>"
        f"<div><h3>{escape(row['title'])}</h3><div class='muted'>{escape(row['source_name'])} · {escape(row['symbols'] or '')}</div></div>"
        f"<strong class='{return_css(performance.return_pct)}'>{format_return(performance.return_pct)}</strong>"
        "</div>"
        f"{render_sparkline(performance.points)}"
        f"<div class='leg-list'>{legs}</div>"
        f"<div class='leg-curves'>{leg_curves}</div>"
        f"{research_items}"
        f"{check_log}"
        f"<div class='logic-grid'>{logic_html}</div>"
        "</article>"
    )


def render_research_items(items) -> str:
    if not items:
        return "<div class='research-items'><h4>研究验证项</h4><div class='check-log-item empty'>暂无研究验证项</div></div>"
    rows = []
    for item in items:
        rows.append(
            "<div class='research-item'>"
            f"<strong>{escape(item['item_type'])}</strong>"
            f"<span>{escape(item['content'])}</span>"
            f"<em>{escape(item['status'])}</em>"
            "</div>"
        )
    return "<div class='research-items'><h4>研究验证项</h4>" + "".join(rows) + "</div>"


def render_logic_block(block) -> str:
    evidence_html = render_logic_evidence(block["evidence"])
    return (
        f"<div class='logic-block {escape(block['logic_type'])}'>"
        f"<strong>{logic_label(block['logic_type'])}</strong><br>"
        f"{escape(block['content'])}"
        f"{evidence_html}"
        "</div>"
    )


def render_logic_evidence(raw_evidence: str | None) -> str:
    evidence = parse_logic_evidence(raw_evidence)
    if not evidence:
        return ""
    items = "".join(f"<div class='logic-evidence-item'>{escape(item)}</div>" for item in evidence)
    return f"<div class='logic-evidence'><span>Evidence / verification</span>{items}</div>"


def parse_logic_evidence(raw_evidence: str | None) -> list[str]:
    if not raw_evidence:
        return []
    try:
        parsed = json.loads(raw_evidence)
    except json.JSONDecodeError:
        return [raw_evidence]
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item).strip()]
    if isinstance(parsed, dict):
        return [f"{key}: {value}" for key, value in parsed.items()]
    return [str(parsed)]


def render_project_check_log(checks) -> str:
    if not checks:
        return "<div class='check-log'><h4>项目检查日志</h4><div class='check-log-item empty'>暂无检查记录</div></div>"
    items = []
    for row in checks:
        rules = json.loads(row["triggered_rules"] or "[]")
        rule_html = "".join(f"<div class='rule-hit'>{escape(rule)}</div>" for rule in rules)
        items.append(
            "<div class='check-log-item'>"
            "<div class='check-log-top'>"
            f"<strong>{escape(row['check_date'])}</strong>"
            f"<span>{escape(row['conclusion'])}</span>"
            "</div>"
            f"<div class='check-log-summary'>{escape(row['summary'])}</div>"
            f"{rule_html}"
            "</div>"
        )
    return "<div class='check-log'><h4>项目检查日志</h4>" + "".join(items) + "</div>"


def render_leg_curve(leg) -> str:
    return (
        "<div class='leg-curve'>"
        "<div class='leg-curve-head'>"
        f"<strong>{escape(leg.symbol)} · {escape(leg.name)} · {leg.weight:.0%}</strong>"
        f"<span class='{return_css(leg.return_pct)}'>{format_price(leg.latest_price)} · {format_return(leg.return_pct)}</span>"
        "</div>"
        f"{render_sparkline(leg.price_points, css_class='mini-chart', label=f'{leg.symbol} 价格曲线', show_zero=False)}"
        "</div>"
    )


def render_sparkline(
    points: list[tuple[str, float]],
    css_class: str = "chart",
    label: str = "收益曲线",
    show_zero: bool = True,
) -> str:
    if len(points) < 2:
        return f"<div class='{escape(css_class)} empty'>暂无价格曲线。运行 check --provider 或 fetch-bars 后显示。</div>"
    width = 640
    height = 120
    values = [value for _, value in points]
    minimum = min(values)
    maximum = max(values)
    span = maximum - minimum or 1
    step = width / (len(points) - 1)
    coords = []
    for index, (_, value) in enumerate(points):
        x = index * step
        y = height - ((value - minimum) / span * (height - 18)) - 9
        coords.append(f"{x:.1f},{y:.1f}")
    zero_line = ""
    if show_zero:
        zero_y = height - ((0 - minimum) / span * (height - 18)) - 9
        zero_y = max(8, min(height - 8, zero_y))
        zero_line = f"<line x1='0' y1='{zero_y:.1f}' x2='640' y2='{zero_y:.1f}' stroke='rgba(231,238,232,.18)' />"
    return (
        f"<svg class='{escape(css_class)}' viewBox='0 0 640 120' role='img' aria-label='{escape(label)}'>"
        f"{zero_line}"
        f"<polyline points='{' '.join(coords)}' fill='none' stroke='#44D7C8' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round' />"
        "</svg>"
    )


def format_return(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:+.2%}"


def format_price(value: float | None) -> str:
    if value is None:
        return "--"
    if abs(value) >= 100:
        return f"{value:.2f}"
    return f"{value:.3f}".rstrip("0").rstrip(".")


def return_css(value: float | None) -> str:
    if value is None:
        return "muted"
    if value >= 0:
        return "positive"
    return "negative"


def logic_label(value: str) -> str:
    if value == "source_logic":
        return "原始信号逻辑"
    if value == "system_logic":
        return "系统补充逻辑"
    return value


def render_publish_stamp(row) -> str:
    if not row:
        return "Card based layered dashboard · Futuristic minimalism"
    status = row["status_code"] or "--"
    url = row["url"] or ""
    if url:
        return f"最近发布：<a href='{escape(url)}'>{escape(url)}</a> · {escape(status)}"
    return f"最近发布状态：{escape(status)}"


def escape(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)
