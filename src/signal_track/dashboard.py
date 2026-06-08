from __future__ import annotations

import html
import json
import re
from datetime import datetime
from urllib.parse import quote

from .analytics import project_performance
from .db import Repository
from .input_summary import input_summaries, project_input_history
from .project_report import build_project_report, render_project_report_markdown


def render_dashboard(repo: Repository) -> str:
    projects = repo.list_project_rows()
    checks = repo.list_daily_checks(limit=20)
    recent_inputs = input_summaries(repo, limit=8)
    publish_events = repo.list_publish_events(limit=1)
    last_publish = publish_events[0] if publish_events else None
    performances = {int(row["id"]): project_performance(repo, int(row["id"])) for row in projects}
    latest_checks = {
        int(row["id"]): next(iter(repo.list_daily_checks(project_id=int(row["id"]), limit=1)), None)
        for row in projects
    }
    active = sum(1 for row in projects if row["status"] in {"active", "needs_review"})
    exits = sum(1 for row in projects if row["status"] == "exit_signal")
    needs_review = sum(1 for row in projects if project_needs_review(row))
    returns = [perf.return_pct for perf in performances.values() if perf.return_pct is not None]
    avg_return = sum(returns) / len(returns) if returns else None
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    project_rows = "\n".join(
        render_project_row(row, performances[int(row["id"])], latest_checks[int(row["id"])])
        for row in projects
    ) or (
        "<tr><td colspan='11' class='empty'>暂无跟踪项目</td></tr>"
    )
    source_cards = render_source_summary(projects, performances)
    source_filter = render_source_filter(projects)
    status_filter = render_status_filter(projects)
    direction_filter = render_direction_filter(projects)
    detail_cards = "\n".join(render_project_detail(repo, row, performances[int(row["id"])]) for row in projects)
    input_items = render_recent_inputs(recent_inputs)
    check_items = "\n".join(
        render_check_item(row)
        for row in checks
    ) or "<li class='empty'>暂无检查记录</li>"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>投资信号看板</title>
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
    html {{ overflow-x: hidden; }}
    body {{
      margin: 0;
      background:
        linear-gradient(rgba(255,255,255,.025) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,.025) 1px, transparent 1px),
        var(--bg);
      background-size: 32px 32px;
      color: var(--text);
      font-family: Geist, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      overflow-x: hidden;
    }}
    .shell {{ max-width: 1440px; margin: 0 auto; padding: 24px; }}
    .topbar {{
      display: flex; align-items: end; justify-content: space-between; gap: 16px;
      padding: 18px 0 22px;
    }}
    h1 {{ margin: 0; font-size: 28px; line-height: 36px; letter-spacing: 0; }}
    .stamp {{ color: var(--muted); font-size: 13px; }}
    .top-actions {{ display: flex; align-items: center; justify-content: end; gap: 10px; flex-wrap: wrap; }}
    .nav-link {{ color: var(--cyan); text-decoration: none; border: 1px solid rgba(68,215,200,.45); border-radius: 999px; min-height: 32px; display: inline-flex; align-items: center; padding: 0 12px; font-size: 13px; background: rgba(68,215,200,.07); }}
    .nav-link:hover {{ background: rgba(68,215,200,.12); }}
    .metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }}
    .source-summary {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }}
    .filter-bar {{ display: grid; gap: 10px; margin: 0 0 16px; }}
    .filter-group {{ display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }}
    .filter-label {{ color: var(--faint); min-width: 56px; font-size: 12px; line-height: 18px; }}
    .source-filter {{ display: flex; }}
    .source-chip {{ color: var(--muted); background: rgba(245,247,244,.055); border: 1px solid var(--border); border-radius: 999px; min-height: 32px; padding: 0 12px; cursor: pointer; font: inherit; }}
    .source-chip:hover, .source-chip.active {{ color: var(--cyan); border-color: rgba(68,215,200,.55); background: rgba(68,215,200,.08); }}
    .source-chip[data-filter-type='status'][data-status='needs_review'].active,
    .source-chip[data-filter-type='status'][data-status='exit_signal'].active {{ color: var(--amber); border-color: rgba(216,179,93,.58); background: rgba(216,179,93,.09); }}
    .source-chip[data-filter-type='status'][data-status='exit_signal'].active {{ color: var(--red); border-color: rgba(255,107,107,.58); background: rgba(255,107,107,.1); }}
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
    .table-wrap {{ width: 100%; overflow-x: auto; overscroll-behavior-x: contain; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 11px 12px; border-bottom: 1px solid rgba(231,238,232,.08); text-align: left; vertical-align: middle; }}
    th {{ color: var(--muted); font-weight: 600; position: sticky; top: 0; background: rgba(13,15,14,.92); }}
    td.num {{ text-align: right; font-variant-numeric: tabular-nums; font-family: "IBM Plex Mono", "Geist Mono", monospace; }}
    td.check-cell {{ min-width: 92px; }}
    td.check-cell span:first-child {{ white-space: nowrap; }}
    td.action-cell {{ min-width: 72px; }}
    .symbol {{ color: var(--cyan); font-family: "IBM Plex Mono", "Geist Mono", monospace; }}
    .positive {{ color: var(--red); }}
    .negative {{ color: var(--green); }}
    .muted {{ color: var(--muted); }}
    .pill {{ display: inline-flex; align-items: center; height: 22px; padding: 0 8px; border: 1px solid var(--border-strong); border-radius: 999px; font-size: 12px; }}
    .pill.active {{ color: var(--cyan); border-color: rgba(68,215,200,.55); }}
    .pill.needs_review {{ color: var(--amber); border-color: rgba(216,179,93,.55); background: rgba(216,179,93,.08); }}
    .pill.exit_signal {{ color: var(--red); border-color: rgba(255,107,107,.6); background: rgba(255,107,107,.1); }}
    .rail {{ padding: 14px 16px; }}
    .rail ul {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 10px; }}
    .rail li {{ display: grid; gap: 4px; padding: 10px 0; border-bottom: 1px solid rgba(231,238,232,.08); }}
    .rail span, .rail em {{ color: var(--muted); font-size: 12px; font-style: normal; }}
    .rail-section {{ margin-top: 14px; padding-top: 14px; border-top: 1px solid rgba(231,238,232,.1); }}
    .rail-section-head {{ display: flex; justify-content: space-between; gap: 8px; align-items: center; margin-bottom: 8px; }}
    .rail-section-head h2 {{ margin: 0; font-size: 16px; line-height: 22px; }}
    .recent-inputs {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 10px; }}
    .recent-inputs li {{ border: 1px solid rgba(231,238,232,.08); border-radius: 8px; padding: 10px; background: rgba(255,255,255,.018); }}
    .input-top {{ display: flex; justify-content: space-between; gap: 8px; align-items: start; }}
    .input-action {{ border: 1px solid var(--border-strong); border-radius: 999px; padding: 2px 8px; font-size: 11px; line-height: 16px; }}
    .input-action.close, .input-action.exit_signal {{ color: var(--red); border-color: rgba(255,107,107,.58); background: rgba(255,107,107,.08); }}
    .input-action.update {{ color: var(--amber); border-color: rgba(216,179,93,.58); background: rgba(216,179,93,.08); }}
    .input-action.mixed {{ color: var(--green); border-color: rgba(88,214,141,.58); background: rgba(88,214,141,.08); }}
    .input-action.track {{ color: var(--cyan); border-color: rgba(68,215,200,.55); background: rgba(68,215,200,.07); }}
    .input-preview {{ color: var(--muted); font-size: 12px; line-height: 18px; margin-top: 6px; }}
    .input-meta {{ color: var(--faint); font-size: 11px; line-height: 16px; margin-top: 6px; }}
    .rule-hit {{ color: var(--amber); font-size: 12px; line-height: 18px; }}
    .empty {{ color: var(--faint); padding: 20px; }}
    .detail-card {{ padding: 16px; }}
    .detail-top {{ display: flex; justify-content: space-between; gap: 12px; align-items: start; margin-bottom: 12px; }}
    .detail-top h3 {{ margin: 0; font-size: 15px; line-height: 22px; }}
    .chart {{ width: 100%; height: 120px; margin: 8px 0 14px; border: 1px solid rgba(231,238,232,.08); border-radius: 8px; background: rgba(255,255,255,.025); }}
    .chart-marker circle {{ stroke: rgba(13,15,14,.9); stroke-width: 2; }}
    .chart-marker text {{ font: 600 13px/1 "Geist Mono", monospace; paint-order: stroke; stroke: rgba(13,15,14,.85); stroke-width: 3px; }}
    .chart-marker-curve-start circle, .chart-marker-curve-end circle {{ fill: var(--cyan); }}
    .chart-marker-curve-start text, .chart-marker-curve-end text {{ fill: var(--cyan); }}
    .chart-marker-open circle {{ fill: var(--amber); }}
    .chart-marker-open text {{ fill: var(--amber); }}
    .chart-marker-close circle {{ fill: var(--red); }}
    .chart-marker-close text {{ fill: var(--red); }}
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
    .project-inputs {{ display: grid; gap: 8px; margin: 10px 0 14px; }}
    .project-inputs h4 {{ margin: 0; font-size: 12px; line-height: 18px; color: var(--muted); }}
    .report-card {{ display: grid; gap: 10px; margin: 10px 0 14px; border: 1px solid rgba(68,215,200,.2); border-radius: 8px; padding: 12px; background: rgba(68,215,200,.045); }}
    .report-card-head {{ display: flex; justify-content: space-between; gap: 10px; align-items: start; }}
    .report-card h4 {{ margin: 0; font-size: 13px; line-height: 19px; }}
    .report-link {{ color: var(--cyan); text-decoration: none; border: 1px solid rgba(68,215,200,.45); border-radius: 999px; padding: 4px 9px; font-size: 12px; white-space: nowrap; }}
    .report-link:hover {{ background: rgba(68,215,200,.1); }}
    .report-artifact {{ color: var(--faint); font-size: 11px; line-height: 16px; word-break: break-word; }}
    .report-stats {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }}
    .report-stat {{ border: 1px solid rgba(231,238,232,.08); border-radius: 8px; padding: 8px; background: rgba(255,255,255,.018); }}
    .report-stat span {{ display: block; color: var(--faint); font-size: 11px; line-height: 15px; }}
    .report-stat strong {{ display: block; margin-top: 3px; font-size: 15px; line-height: 20px; font-variant-numeric: tabular-nums; }}
    .framework-tags {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .framework-tag {{ border: 1px solid rgba(231,238,232,.14); border-radius: 999px; padding: 3px 8px; color: var(--muted); font-size: 11px; line-height: 16px; }}
    .framework-tag.covered {{ color: var(--cyan); border-color: rgba(68,215,200,.45); background: rgba(68,215,200,.08); }}
    .report-body {{ border: 1px solid rgba(231,238,232,.1); border-radius: 8px; background: rgba(0,0,0,.16); overflow: hidden; }}
    .report-body summary {{ cursor: pointer; padding: 9px 10px; color: var(--cyan); font-size: 12px; line-height: 18px; }}
    .report-body pre {{ margin: 0; max-height: 360px; overflow: auto; padding: 12px; white-space: pre-wrap; word-break: break-word; color: var(--muted); font: 12px/18px "IBM Plex Mono", "Geist Mono", monospace; border-top: 1px solid rgba(231,238,232,.08); }}
    @media (max-width: 900px) {{
      .shell {{ padding: 16px; }}
      .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .source-summary {{ grid-template-columns: 1fr; }}
      .grid {{ grid-template-columns: 1fr; }}
      .details {{ grid-template-columns: 1fr; }}
      .topbar {{ align-items: start; flex-direction: column; }}
      .top-actions {{ justify-content: start; }}
      table {{ min-width: 960px; }}
      .research-item {{ grid-template-columns: 1fr; gap: 6px; }}
      .research-item em {{ text-align: left; }}
      .detail-top {{ flex-direction: column; }}
      .report-card-head {{ flex-direction: column; }}
      .report-stats {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 520px) {{
      .shell {{ padding: 12px; }}
      h1 {{ font-size: 22px; line-height: 30px; }}
      .metrics {{ gap: 8px; }}
      .metric {{ padding: 12px; }}
      .metric strong {{ font-size: 22px; line-height: 28px; }}
      .source-card-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .panel-header {{ padding: 12px; }}
      .detail-card {{ padding: 12px; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="topbar">
      <div>
        <h1>投资信号看板</h1>
        <div class="stamp">最后生成：{escape(now)}</div>
      </div>
      <div class="top-actions">
        <a class="nav-link" href="/inbox" title="打开信息录入页">录入</a>
        <div class="stamp">{render_publish_stamp(last_publish)}</div>
      </div>
    </section>
    <section class="filter-bar" aria-label="看板筛选">
      <div class="filter-group source-filter" data-filter-group="source"><span class="filter-label">来源</span>{source_filter}</div>
      <div class="filter-group" data-filter-group="status"><span class="filter-label">状态</span>{status_filter}</div>
      <div class="filter-group" data-filter-group="direction"><span class="filter-label">方向</span>{direction_filter}</div>
    </section>
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
        <div class="table-wrap" tabindex="0" aria-label="跟踪项目表格">
          <table>
            <thead><tr><th>状态</th><th>信息源</th><th>项目</th><th>标的</th><th>方向</th><th>入场</th><th>逻辑分</th><th>收益</th><th>最新检查</th><th>动作</th><th>复核</th></tr></thead>
            <tbody>{project_rows}</tbody>
          </table>
        </div>
      </div>
      <aside class="card rail">
        <div class="panel-header"><h2>每日检查</h2></div>
        <ul>{check_items}</ul>
        <div class="rail-section">
          <div class="rail-section-head"><h2>最近输入</h2><span>{len(recent_inputs)}</span></div>
          <ul class="recent-inputs">{input_items}</ul>
        </div>
      </aside>
    </section>
    <section class="details">{detail_cards}</section>
  </main>
  <script>
    (() => {{
      const chips = Array.from(document.querySelectorAll('[data-filter-type]'));
      const rows = Array.from(document.querySelectorAll('tr[data-source]'));
      const cards = Array.from(document.querySelectorAll('article.detail-card[data-source]'));
      const state = {{ source: 'all', status: 'all', direction: 'all' }};
      const matches = (node) => {{
        return (state.source === 'all' || node.dataset.source === state.source)
          && (state.status === 'all' || node.dataset.status === state.status)
          && (state.direction === 'all' || node.dataset.direction === state.direction);
      }};
      const applyFilters = () => {{
        chips.forEach((chip) => chip.classList.toggle('active', state[chip.dataset.filterType] === chip.dataset.value));
        [...rows, ...cards].forEach((node) => {{
          node.hidden = !matches(node);
        }});
      }};
      chips.forEach((chip) => chip.addEventListener('click', () => {{
        state[chip.dataset.filterType] = chip.dataset.value || 'all';
        applyFilters();
      }}));
      applyFilters();
    }})();
  </script>
</body>
</html>"""


def render_source_filter(projects) -> str:
    sources = sorted({str(row["source_name"] or "manual") for row in projects})
    buttons = [
        "<button type='button' class='source-chip active' data-filter-type='source' data-value='all'>全部</button>"
    ]
    buttons.extend(
        (
            "<button type='button' class='source-chip' "
            f"data-filter-type='source' data-value='{escape(source)}'>{escape(source)}</button>"
        )
        for source in sources
    )
    return "".join(buttons)


def render_status_filter(projects) -> str:
    present_statuses = {str(row["status"]) for row in projects}
    options = [
        ("all", "全部"),
        ("active", "活跃"),
        ("needs_review", "待复核"),
        ("exit_signal", "平仓信号"),
        ("closed", "已平仓"),
    ]
    return "".join(
        (
            "<button type='button' class='source-chip"
            f"{' active' if value == 'all' else ''}' data-filter-type='status' "
            f"data-value='{escape(value)}' data-status='{escape(value)}'"
            f"{' disabled' if value != 'all' and value not in present_statuses else ''}>"
            f"{escape(label)}</button>"
        )
        for value, label in options
    )


def render_direction_filter(projects) -> str:
    present_directions = {str(row["direction"]) for row in projects}
    options = [
        ("all", "全部"),
        ("long", "做多"),
        ("short", "做空"),
        ("neutral", "观察"),
    ]
    return "".join(
        (
            "<button type='button' class='source-chip"
            f"{' active' if value == 'all' else ''}' data-filter-type='direction' "
            f"data-value='{escape(value)}'"
            f"{' disabled' if value != 'all' and value not in present_directions else ''}>"
            f"{escape(label)}</button>"
        )
        for value, label in options
    )


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
        if project_needs_review(row):
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


def render_project_row(row, performance, latest_check=None) -> str:
    status = str(row["status"])
    status_class = escape(status)
    review = "是" if project_needs_review(row) else "否"
    return_class = return_css(performance.return_pct)
    symbols = row["symbols"] or ""
    instrument_names = row["instrument_names"] or ""
    latest_return = format_return(performance.return_pct)
    return (
        f"<tr data-source='{escape(row['source_name'])}' "
        f"data-status='{escape(row['status'])}' data-direction='{escape(row['direction'])}'>"
        f"<td><span class='pill {status_class}' title='{escape(status_label(status))}'>{escape(status_label(status))}</span></td>"
        f"<td title='{escape(row['source_name'])}'>{escape(row['source_name'])}</td>"
        f"<td>{escape(row['title'])}</td>"
        f"<td title='{escape(symbols)} / {escape(instrument_names)}'><span class='symbol'>{escape(symbols)}</span><br><span class='muted'>{escape(instrument_names)}</span></td>"
        f"<td title='{escape(direction_label(row['direction']))}'>{escape(direction_label(row['direction']))}</td>"
        f"<td class='num' title='{escape(row['entry_date'] or '--')}'>{escape(row['entry_date'] or '--')}</td>"
        f"<td class='num'>{float(row['logic_score']):.1f}</td>"
        f"<td class='num {return_class}' title='{latest_return}'>{latest_return}</td>"
        f"<td class='check-cell'>{format_latest_check(latest_check)}</td>"
        f"<td class='action-cell' title='{escape(next_action_label(row))}'>{escape(next_action_label(row))}</td>"
        f"<td>{review}</td>"
        "</tr>"
    )


def render_check_item(row) -> str:
    rules = json.loads(row["triggered_rules"] or "[]")
    rule_html = "".join(f"<div class='rule-hit'>{escape(localize_text(rule))}</div>" for rule in rules)
    return (
        "<li>"
        f"<span>{escape(row['check_date'])}</span>"
        f"<strong>{escape(row['title'])}</strong>"
        f"<em>{escape(conclusion_label(row['conclusion']))}</em>"
        f"{rule_html}"
        "</li>"
    )


def render_recent_inputs(inputs: list[dict]) -> str:
    if not inputs:
        return "<li class='empty'>暂无输入记录</li>"
    return "\n".join(render_input_item(item) for item in inputs)


def render_input_item(item: dict) -> str:
    action = str(item.get("input_action") or "none")
    symbols = item.get("resolved_symbols") or []
    symbol_text = ", ".join(str(symbol) for symbol in symbols) or "--"
    project_count = len(item.get("project_ids") or [])
    return (
        f"<li data-input-action='{escape(action)}'>"
        "<div class='input-top'>"
        f"<strong>{escape(item.get('source_name'))}</strong>"
        f"<span class='input-action {escape(action)}'>{escape(input_action_label(action))}</span>"
        "</div>"
        f"<div class='input-preview'>{escape(item.get('content_preview'))}</div>"
        f"<div class='input-meta'>{escape(item.get('received_at'))} · {escape(symbol_text)} · 关联项目 {project_count}</div>"
        "</li>"
    )


def render_project_inputs(inputs: list[dict]) -> str:
    if not inputs:
        return "<div class='project-inputs'><h4>输入记录</h4><div class='check-log-item empty'>暂无关联输入</div></div>"
    items = "".join(render_input_item(item) for item in inputs)
    return f"<div class='project-inputs'><h4>输入记录</h4><ul class='recent-inputs'>{items}</ul></div>"


def render_project_detail(repo: Repository, row, performance) -> str:
    logic_blocks = repo.list_logic_blocks(int(row["id"]))
    logic_html = "\n".join(render_logic_block(block) for block in logic_blocks)
    check_log = render_project_check_log(repo.list_daily_checks(project_id=int(row["id"]), limit=5))
    report_snapshot = render_report_snapshot(repo, int(row["id"]))
    research_items = render_research_items(repo.list_research_items(project_id=int(row["id"]), limit=8))
    input_history = render_project_inputs(project_input_history(repo, int(row["id"]), limit=5))
    legs = "\n".join(
        f"<span class='leg'>{escape(leg.symbol)} · {leg.weight:.0%} · {format_return(leg.return_pct)}</span>"
        for leg in performance.legs
    )
    leg_curves = "\n".join(render_leg_curve(leg, row["entry_date"], row["closed_date"]) for leg in performance.legs)
    return (
        f"<article class='card detail-card' data-source='{escape(row['source_name'])}' "
        f"data-status='{escape(row['status'])}' data-direction='{escape(row['direction'])}'>"
        "<div class='detail-top'>"
        f"<div><h3>{escape(row['title'])}</h3><div class='muted'>{escape(row['source_name'])} · {escape(row['symbols'] or '')}</div></div>"
        f"<strong class='{return_css(performance.return_pct)}'>{format_return(performance.return_pct)}</strong>"
        "</div>"
        f"{render_performance_window(row, performance)}"
        f"{render_sparkline(performance.points, trade_markers=project_chart_markers(row['entry_date'], row['closed_date']))}"
        f"<div class='leg-list'>{legs}</div>"
        f"<div class='leg-curves'>{leg_curves}</div>"
        f"{report_snapshot}"
        f"{input_history}"
        f"{research_items}"
        f"{check_log}"
        f"<div class='logic-grid'>{logic_html}</div>"
        "</article>"
    )


def render_performance_window(row, performance) -> str:
    start = performance.window_start or "--"
    end = performance.window_end or "--"
    closed_date = row["closed_date"]
    label = "价格窗口"
    if row["status"] == "closed" and closed_date:
        label = f"价格窗口（含平仓后一个月，平仓日 {closed_date}）"
    return (
        "<div class='input-meta' "
        f"title='{escape(label)}: {escape(start)} to {escape(end)}'>"
        f"{escape(label)}：{escape(start)} 至 {escape(end)}"
        "</div>"
    )


def render_report_snapshot(repo: Repository, project_id: int) -> str:
    report = build_project_report(repo, project_id)
    if not report:
        return ""
    verification = report["data_verification"]
    markdown = localize_text(render_project_report_markdown(report))
    artifact = repo.get_latest_project_report(project_id, "markdown") or repo.get_latest_project_report(project_id)
    artifact_line = render_report_artifact_line(artifact)
    covered = {section["name"] for section in report["framework"] if section["items"]}
    tags = "".join(
        f"<span class='framework-tag{' covered' if name in covered else ''}'>{escape(name)}</span>"
        for name in ["3C", "5M", "3D", "3T"]
    )
    return (
        "<section class='report-card' aria-label='项目投研报告'>"
        "<div class='report-card-head'>"
        f"<div><h4>{escape(report['title'])}</h4><div class='muted'>内嵌投研报告，可下载报告文件</div></div>"
        f"<a class='report-link' href='{report_download_href(markdown)}' "
        "aria-label='下载项目投研报告文件' title='下载项目投研报告文件' "
        f"download='signal-track-project-{project_id}-report.md'>下载报告</a>"
        "</div>"
        f"{artifact_line}"
        "<div class='report-stats'>"
        f"<div class='report-stat'><span>已验证</span><strong>{verification['verified_count']}</strong></div>"
        f"<div class='report-stat'><span>待验证</span><strong>{verification['pending_count']}</strong></div>"
        f"<div class='report-stat'><span>已证伪</span><strong>{verification['contradicted_count']}</strong></div>"
        "</div>"
        f"<div class='framework-tags'>{tags}</div>"
        "<details class='report-body'>"
        "<summary>查看内嵌报告</summary>"
        f"<pre>{escape(markdown)}</pre>"
        "</details>"
        "</section>"
    )


def render_report_artifact_line(artifact) -> str:
    if not artifact:
        return "<div class='report-artifact'>尚未归档报告文件。导出一次后会显示报告文件。</div>"
    digest = str(artifact["content_hash"] or "")
    digest_label = digest[:12] if digest else "--"
    return (
        "<div class='report-artifact'>"
        f"已归档报告：{escape(str(artifact['path']))} · {escape(format_label(str(artifact['format'])))} · "
        f"sha256 {escape(digest_label)} - {escape(str(artifact['generated_at']))}"
        "</div>"
    )


def report_download_href(markdown: str) -> str:
    return "data:text/markdown;charset=utf-8," + quote(markdown)


def render_research_items(items) -> str:
    if not items:
        return "<div class='research-items'><h4>研究验证项</h4><div class='check-log-item empty'>暂无研究验证项</div></div>"
    rows = []
    for item in items:
        rows.append(
            "<div class='research-item'>"
            f"<strong>{escape(research_type_label(item['item_type']))}</strong>"
            f"<span>{escape(localize_text(item['content']))}</span>"
            f"<em>{escape(research_status_label(item['status']))}</em>"
            "</div>"
        )
    return "<div class='research-items'><h4>研究验证项</h4>" + "".join(rows) + "</div>"


def render_logic_block(block) -> str:
    evidence_html = render_logic_evidence(block["evidence"])
    return (
        f"<div class='logic-block {escape(block['logic_type'])}'>"
        f"<strong>{logic_label(block['logic_type'])}</strong><br>"
        f"{escape(localize_text(block['content']))}"
        f"{evidence_html}"
        "</div>"
    )


def render_logic_evidence(raw_evidence: str | None) -> str:
    evidence = parse_logic_evidence(raw_evidence)
    if not evidence:
        return ""
    items = "".join(f"<div class='logic-evidence-item'>{escape(localize_text(item))}</div>" for item in evidence)
    return f"<div class='logic-evidence'><span>证据 / 验证</span>{items}</div>"


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
        return [f"{format_label(str(key))}: {value}" for key, value in parsed.items()]
    return [str(parsed)]


def render_project_check_log(checks) -> str:
    if not checks:
        return "<div class='check-log'><h4>项目检查日志</h4><div class='check-log-item empty'>暂无检查记录</div></div>"
    items = []
    for row in checks:
        rules = json.loads(row["triggered_rules"] or "[]")
        rule_html = "".join(f"<div class='rule-hit'>{escape(localize_text(rule))}</div>" for rule in rules)
        items.append(
            "<div class='check-log-item'>"
            "<div class='check-log-top'>"
            f"<strong>{escape(row['check_date'])}</strong>"
            f"<span>{escape(conclusion_label(row['conclusion']))}</span>"
            "</div>"
            f"<div class='check-log-summary'>{escape(localize_text(row['summary']))}</div>"
            f"{rule_html}"
            "</div>"
        )
    return "<div class='check-log'><h4>项目检查日志</h4>" + "".join(items) + "</div>"


def render_leg_curve(leg, project_entry_date: str | None, project_closed_date: str | None) -> str:
    return (
        "<div class='leg-curve'>"
        "<div class='leg-curve-head'>"
        f"<strong>{escape(leg.symbol)} · {escape(leg.name)} · {leg.weight:.0%}</strong>"
        f"<span class='{return_css(leg.return_pct)}'>{format_price(leg.latest_price)} · {format_return(leg.return_pct)}</span>"
        "</div>"
        f"{render_sparkline(leg.price_points, css_class='mini-chart', label=f'{leg.symbol} 价格曲线', show_zero=False, trade_markers=project_chart_markers(project_entry_date, project_closed_date))}"
        "</div>"
    )


def render_sparkline(
    points: list[tuple[str, float]],
    css_class: str = "chart",
    label: str = "收益曲线",
    show_zero: bool = True,
    trade_markers: list[dict[str, str]] | None = None,
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
    markers = curve_boundary_markers(points) + (trade_markers or [])
    marker_html = render_chart_markers(points, coords, markers)
    return (
        f"<svg class='{escape(css_class)}' viewBox='0 0 640 120' role='img' aria-label='{escape(label)}'>"
        f"{zero_line}"
        f"<polyline points='{' '.join(coords)}' fill='none' stroke='#44D7C8' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round' />"
        f"{marker_html}"
        "</svg>"
    )


def project_chart_markers(entry_date: str | None, closed_date: str | None) -> list[dict[str, str]]:
    markers = []
    if entry_date:
        markers.append({"date": entry_date, "label": "开", "title_label": "开仓", "kind": "open"})
    if closed_date:
        markers.append({"date": closed_date, "label": "平", "title_label": "平仓", "kind": "close"})
    return markers


def curve_boundary_markers(points: list[tuple[str, float]]) -> list[dict[str, str]]:
    if not points:
        return []
    markers = [{"date": points[0][0], "label": "始", "title_label": "曲线开始", "kind": "curve-start"}]
    if points[-1][0] != points[0][0]:
        markers.append({"date": points[-1][0], "label": "末", "title_label": "曲线结束", "kind": "curve-end"})
    return markers


def render_chart_markers(points: list[tuple[str, float]], coords: list[str], markers: list[dict[str, str]]) -> str:
    if not points or not coords or not markers:
        return ""
    return "".join(chart_marker(points, coords, marker) for marker in markers)


def chart_marker(points: list[tuple[str, float]], coords: list[str], marker: dict[str, str]) -> str:
    target_date = marker["date"]
    index = chart_marker_index(points, target_date)
    if index is None:
        return ""
    point = points[index]
    coord = coords[index]
    date_label, value = point
    x_text, y_text = coord.split(",", 1)
    x = float(x_text)
    y = float(y_text)
    label = marker["label"]
    kind = marker["kind"]
    is_left_label = kind in {"open", "curve-start"}
    css_class = f"chart-marker-{kind}"
    text_anchor = "start" if is_left_label else "end"
    text_x = min(620, x + 10) if is_left_label else max(20, x - 10)
    text_y = marker_text_y(y, kind)
    plotted_suffix = marker_plotted_suffix(points, target_date, date_label)
    title_label = marker.get("title_label", label)
    title = f"{title_label}点：{target_date}{plotted_suffix} / {value:.4g}"
    visible_label = f"{label} {compact_date(target_date)}"
    return (
        f"<g class='chart-marker {css_class}'>"
        f"<title>{escape(title)}</title>"
        f"<circle cx='{x:.1f}' cy='{y:.1f}' r='5' />"
        f"<text x='{text_x:.1f}' y='{text_y:.1f}' text-anchor='{text_anchor}'>{escape(visible_label)}</text>"
        "</g>"
    )


def marker_text_y(y: float, kind: str) -> float:
    offset = 20 if kind in {"curve-start", "curve-end"} else -8
    return min(112, max(18, y + offset))


def marker_plotted_suffix(points: list[tuple[str, float]], target_date: str, plotted_date: str) -> str:
    if plotted_date == target_date:
        return ""
    first_date = points[0][0]
    last_date = points[-1][0]
    if target_date < first_date:
        return f"（早于曲线开始 {first_date}）"
    if target_date > last_date:
        return f"（晚于曲线结束 {last_date}）"
    return f"（图上定位 {plotted_date}）"


def compact_date(value: str) -> str:
    parts = value.split("-")
    if len(parts) == 3:
        return f"{parts[1]}-{parts[2]}"
    return value


def chart_marker_index(points: list[tuple[str, float]], target_date: str) -> int | None:
    if not points:
        return None
    for index, (point_date, _) in enumerate(points):
        if point_date >= target_date:
            return index
    return len(points) - 1


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


def format_latest_check(row) -> str:
    if not row:
        return "<span class='muted'>--</span>"
    conclusion = conclusion_label(row["conclusion"])
    title = f"{row['check_date']} / {conclusion}"
    return (
        f"<span title='{escape(title)}'>{escape(row['check_date'])}</span><br>"
        f"<span class='muted'>{escape(conclusion)}</span>"
    )


def status_label(value: object) -> str:
    labels = {
        "active": "跟踪中",
        "needs_review": "待复核",
        "exit_signal": "平仓信号",
        "closed": "已平仓",
        "watch_after_close": "平仓后观察",
        "archived": "已归档",
    }
    return labels.get(str(value), str(value))


def direction_label(value: object) -> str:
    labels = {
        "long": "做多",
        "short": "做空",
        "neutral": "观察",
        "unknown": "未知",
    }
    return labels.get(str(value), str(value))


def conclusion_label(value: object) -> str:
    labels = {
        "hold": "持有",
        "watch": "观察",
        "needs_review": "待复核",
        "exit_signal": "平仓信号",
    }
    return labels.get(str(value), str(value))


def input_action_label(value: object) -> str:
    labels = {
        "track": "新增跟踪",
        "update": "更新",
        "mixed": "更新/新增",
        "close": "平仓",
        "close_unmatched": "未匹配平仓",
        "none": "无操作",
    }
    return labels.get(str(value), str(value))


def research_type_label(value: object) -> str:
    labels = {
        "verification_note": "验证项",
        "exit_condition": "退出条件",
        "tracking_metric": "跟踪指标",
    }
    return labels.get(str(value), str(value))


def research_status_label(value: object) -> str:
    labels = {
        "pending": "待处理",
        "unverified": "未验证",
        "verified": "已验证",
        "contradicted": "已证伪",
        "ignored": "已忽略",
    }
    return labels.get(str(value), str(value))


def format_label(value: str) -> str:
    labels = {
        "markdown": "报告文件",
        "source": "来源",
        "local 3C-5M-3D-3T fallback": "本地 3C-5M-3D-3T 补充框架",
        "research_playbook": "研究手册",
        "cross_validation_rule": "交叉验证规则",
        "verification_note": "验证备注",
        "verification_status": "验证状态",
        "source_logic": "原始信号逻辑",
        "system_logic": "系统补充逻辑",
        "manual_note": "手动备注",
    }
    return labels.get(value, value.replace("_", " "))


def localize_text(value: object) -> str:
    text = "" if value is None else str(value)
    replacements = {
        "Project logic score 4.0 is below 6; keep the project in review until the thesis and tracking logic are verified.": "项目逻辑分 4.0 低于 6；在投资假设和跟踪逻辑完成复核前，保持待复核状态。",
        "Project logic score": "项目逻辑分",
        "is below 6; keep the project in review until the thesis and tracking logic are verified.": "低于 6；在投资假设和跟踪逻辑完成复核前，保持待复核状态。",
        "source: local 3C-5M-3D-3T fallback": "来源：本地 3C-5M-3D-3T 补充框架",
        "research_playbook: Step 1 financial/valuation, Step 2 industry/competition, Step 3 latest dynamics/management": "研究手册：第一步财务与估值，第二步行业与竞争，第三步最新动态与管理层。",
        "cross_validation_rule: core financial data requires at least two independent sources before being marked verified": "交叉验证规则：核心财务数据至少需要两个独立来源确认后才能标记为已验证。",
        "verification_status: unverified": "验证状态：未验证",
        "financial/valuation": "财务与估值",
        "industry/competition": "行业与竞争",
        "latest dynamics/management": "最新动态与管理层",
        "core financial data requires at least two independent sources before being marked verified": "核心财务数据至少需要两个独立来源确认后才能标记为已验证",
        "external financial, industry, and news verification": "外部财务、行业和新闻验证",
        "high-conviction use": "高置信度使用",
        "requires external financial, industry, and news verification before high-conviction use.": "需要完成外部财务、行业和新闻验证后，才能作为高置信度依据使用。",
        "collect latest revenue, net profit, OPM, ROE, PE/PB, free cash flow, and leverage; verify core financial numbers against at least two independent sources before using them.": "收集最新收入、净利润、经营利润率、ROE、PE/PB、自由现金流和杠杆数据；核心财务数据使用前至少用两个独立来源交叉验证。",
        "collect industry TAM/growth, market share trend, competitors, cycle position, and entry barriers; mark unverified figures explicitly until source quality is checked.": "收集行业空间/增速、份额趋势、竞争对手、周期位置和进入壁垒；来源质量确认前，所有数字都明确标记为未验证。",
        "review latest company news, strategy changes, management changes, M&A, analyst sentiment, and user-specific concerns from the original note.": "复核最新公司新闻、战略变化、管理层变化、并购、分析师情绪，以及原始笔记中的用户关注点。",
        "track whether the original thesis is improving or deteriorating through 3C signals (cycle position, key change, certainty) and the most relevant 5M operating metrics.": "通过 3C 信号（周期位置、关键变化、确定性）和最相关的 5M 经营指标，跟踪原始投资假设是在改善还是恶化。",
        "track price/return, moving-average breaks, valuation sentiment, and missing price data as daily 3D/3T risk signals.": "把价格/收益、均线跌破、估值情绪和缺失行情作为每日 3D/3T 风险信号跟踪。",
        "if verified data contradicts the original opening thesis or shows the key 3C change has reversed, mark this item contradicted and run a check.": "如果已验证数据与原始开仓假设冲突，或显示关键 3C 变化已经逆转，则将该项标记为已证伪并运行检查。",
        "if price action confirms thesis failure, for example a decisive moving-average break or configured drawdown/stop-loss threshold, trigger exit review.": "如果价格行为确认假设失败，例如有效跌破均线或触发已配置的回撤/止损阈值，则触发退出复核。",
        "local 3C-5M-3D-3T fallback": "本地 3C-5M-3D-3T 补充框架",
        "Step 1": "第一步",
        "Step 2": "第二步",
        "Step 3": "第三步",
        "Cycle": "周期",
        "Change": "变化",
        "Certainty": "确定性",
        "Market Space": "市场空间",
        "Market Share": "市场份额",
        "Business Model": "商业模式",
        "Management": "管理层",
        "External Change": "外部变化",
        "Sentiment/Valuation": "情绪/估值",
        "0-3 months": "0-3 个月",
        "3-15 months": "3-15 个月",
        "15+ months": "15 个月以上",
        "verified": "已验证",
        "unverified": "未验证",
        "pending": "待处理",
        "contradicted": "已证伪",
        "verification_note": "验证项",
        "exit_condition": "退出条件",
        "tracking_metric": "跟踪指标",
        "source_logic": "原始信号逻辑",
        "source_update": "后续信息更新",
        "system_logic": "系统补充逻辑",
        "close_logic": "平仓逻辑",
        "manual_note": "手动备注",
        "user_request": "用户请求",
        "supplement_confidence": "补充置信度",
        "exit_signal": "平仓信号",
        "needs_review": "待复核",
        "_supplement": "补充",
        "**long**": "**做多**",
        "**short**": "**做空**",
        "**neutral**": "**观察**",
        "：long": "：做多",
        "：short": "：做空",
        "：neutral": "：观察",
        " long，": " 做多，",
        " short，": " 做空，",
        " neutral，": " 观察，",
        " / etf": " / ETF",
        " / stock": " / 股票",
        " / index": " / 指数",
        " / fund": " / 基金",
        " / future": " / 期货",
        "provider/provider_symbol": "数据供应商/供应商代码",
        "price_bars": "本地行情表",
        "daily_checks": "每日检查表",
        "research_items": "研究验证项表",
        "user_request: start date changed to 2026-05-04": "用户请求：开始时间调整为 2026-05-04",
        "start date changed to 2026-05-04": "开始时间调整为 2026-05-04",
        "financial_data_and_valuation": "财务数据与估值",
        "industry_and_competition": "行业与竞争",
        "latest_dynamics_and_management": "最新动态与管理层",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(
        r"项目逻辑分\s+([0-9]+(?:\.[0-9]+)?)\s+低于 6；在投资假设和跟踪逻辑完成复核前，保持待复核状态。",
        r"项目逻辑分 \1 低于 6；在投资假设和跟踪逻辑完成复核前，保持待复核状态。",
        text,
    )
    text = re.sub(
        r"Project logic score\s+([0-9]+(?:\.[0-9]+)?)\s+低于 6；在投资假设和跟踪逻辑完成复核前，保持待复核状态。",
        r"项目逻辑分 \1 低于 6；在投资假设和跟踪逻辑完成复核前，保持待复核状态。",
        text,
    )
    text = re.sub(
        r"验证项: (.+?) 需要完成外部财务、行业和新闻验证后，才能作为高置信度依据使用。",
        r"验证项：\1 需要完成外部财务、行业和新闻验证后，才能作为高置信度依据使用。",
        text,
    )
    text = text.replace("验证项:", "验证项：")
    text = text.replace("退出条件:", "退出条件：")
    text = text.replace("跟踪指标:", "跟踪指标：")
    text = text.replace("来源:", "来源：")
    text = text.replace("研究手册:", "研究手册：")
    text = text.replace("交叉验证规则:", "交叉验证规则：")
    text = text.replace("验证状态:", "验证状态：")
    text = text.replace("用户请求:", "用户请求：")
    text = text.replace("手动备注:", "手动备注：")
    text = text.replace("[un已验证]", "[未验证]")
    text = text.replace("un已验证", "未验证")
    return text


def next_action_label(row) -> str:
    status = str(row["status"])
    if status == "exit_signal":
        return "复核平仓"
    if status == "closed":
        return "平仓后观察"
    if bool(row["weight_needs_review"]):
        return "确认权重"
    if status == "needs_review" or bool(row["needs_review"]):
        return "复核逻辑"
    return "继续跟踪"


def logic_label(value: str) -> str:
    if value == "source_logic":
        return "原始信号逻辑"
    if value == "system_logic":
        return "系统补充逻辑"
    if value == "source_update":
        return "后续信息更新"
    if value == "close_logic":
        return "平仓逻辑"
    if value == "weight_update":
        return "权重更新"
    if value == "manual_note":
        return "手动备注"
    return value


def render_publish_stamp(row) -> str:
    if not row:
        return "尚未发布"
    status = row["status_code"] or "--"
    url = row["url"] or ""
    if url:
        return f"最近发布：<a href='{escape(url)}'>{escape(url)}</a> · {escape(status)}"
    return f"最近发布状态：{escape(status)}"


def project_needs_review(row) -> bool:
    if str(row["status"]) == "closed":
        return False
    return bool(row["needs_review"]) or bool(row["weight_needs_review"])


def escape(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)
