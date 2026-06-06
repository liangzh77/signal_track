from __future__ import annotations

import html
import json
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
        <h1>Signal Track 投资信号看板</h1>
        <div class="stamp">最后生成：{escape(now)}</div>
      </div>
      <div class="top-actions">
        <a class="nav-link" href="/inbox" title="打开信息录入页">Inbox</a>
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
    status = escape(row["status"])
    review = "是" if project_needs_review(row) else "否"
    return_class = return_css(performance.return_pct)
    symbols = row["symbols"] or ""
    instrument_names = row["instrument_names"] or ""
    latest_return = format_return(performance.return_pct)
    return (
        f"<tr data-source='{escape(row['source_name'])}' "
        f"data-status='{escape(row['status'])}' data-direction='{escape(row['direction'])}'>"
        f"<td><span class='pill {status}' title='{status}'>{status}</span></td>"
        f"<td title='{escape(row['source_name'])}'>{escape(row['source_name'])}</td>"
        f"<td>{escape(row['title'])}</td>"
        f"<td title='{escape(symbols)} / {escape(instrument_names)}'><span class='symbol'>{escape(symbols)}</span><br><span class='muted'>{escape(instrument_names)}</span></td>"
        f"<td title='{escape(row['direction'])}'>{escape(row['direction'])}</td>"
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
    rule_html = "".join(f"<div class='rule-hit'>{escape(rule)}</div>" for rule in rules)
    return (
        "<li>"
        f"<span>{escape(row['check_date'])}</span>"
        f"<strong>{escape(row['title'])}</strong>"
        f"<em>{escape(row['conclusion'])}</em>"
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
        f"<span class='input-action {escape(action)}'>{escape(action)}</span>"
        "</div>"
        f"<div class='input-preview'>{escape(item.get('content_preview'))}</div>"
        f"<div class='input-meta'>{escape(item.get('received_at'))} · {escape(symbol_text)} · projects {project_count}</div>"
        "</li>"
    )


def render_project_inputs(inputs: list[dict]) -> str:
    if not inputs:
        return "<div class='project-inputs'><h4>Input history</h4><div class='check-log-item empty'>No linked inputs</div></div>"
    items = "".join(render_input_item(item) for item in inputs)
    return f"<div class='project-inputs'><h4>Input history</h4><ul class='recent-inputs'>{items}</ul></div>"


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
    leg_curves = "\n".join(render_leg_curve(leg) for leg in performance.legs)
    return (
        f"<article class='card detail-card' data-source='{escape(row['source_name'])}' "
        f"data-status='{escape(row['status'])}' data-direction='{escape(row['direction'])}'>"
        "<div class='detail-top'>"
        f"<div><h3>{escape(row['title'])}</h3><div class='muted'>{escape(row['source_name'])} · {escape(row['symbols'] or '')}</div></div>"
        f"<strong class='{return_css(performance.return_pct)}'>{format_return(performance.return_pct)}</strong>"
        "</div>"
        f"{render_performance_window(row, performance)}"
        f"{render_sparkline(performance.points)}"
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
    markdown = render_project_report_markdown(report)
    covered = {section["name"] for section in report["framework"] if section["items"]}
    tags = "".join(
        f"<span class='framework-tag{' covered' if name in covered else ''}'>{escape(name)}</span>"
        for name in ["3C", "5M", "3D", "3T"]
    )
    return (
        "<section class='report-card' aria-label='project research report'>"
        "<div class='report-card-head'>"
        f"<div><h4>{escape(report['title'])}</h4><div class='muted'>Embedded report with Markdown download</div></div>"
        f"<a class='report-link' href='{report_download_href(markdown)}' "
        "aria-label='下载项目投研报告 Markdown' title='下载项目投研报告 Markdown' "
        f"download='signal-track-project-{project_id}-report.md'>Markdown</a>"
        "</div>"
        "<div class='report-stats'>"
        f"<div class='report-stat'><span>verified</span><strong>{verification['verified_count']}</strong></div>"
        f"<div class='report-stat'><span>pending</span><strong>{verification['pending_count']}</strong></div>"
        f"<div class='report-stat'><span>contradicted</span><strong>{verification['contradicted_count']}</strong></div>"
        "</div>"
        f"<div class='framework-tags'>{tags}</div>"
        "<details class='report-body'>"
        "<summary>View embedded report</summary>"
        f"<pre>{escape(markdown)}</pre>"
        "</details>"
        "</section>"
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


def format_latest_check(row) -> str:
    if not row:
        return "<span class='muted'>--</span>"
    title = f"{row['check_date']} / {row['conclusion']}"
    return (
        f"<span title='{escape(title)}'>{escape(row['check_date'])}</span><br>"
        f"<span class='muted'>{escape(row['conclusion'])}</span>"
    )


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
