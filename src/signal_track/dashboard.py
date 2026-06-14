from __future__ import annotations

import html
import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote

from .analytics import project_performance
from .db import Repository
from .input_summary import input_summaries, project_input_history
from .project_report import build_project_report, render_project_report_markdown


def render_dashboard(repo: Repository) -> str:
    projects = sorted(
        repo.list_project_rows(),
        key=lambda row: (row["entry_date"] or row["created_at"][:10], int(row["id"])),
        reverse=True,
    )
    recent_inputs = input_summaries(repo, limit=8)
    performances = {int(row["id"]): project_performance(repo, int(row["id"])) for row in projects}
    latest_checks = {
        int(row["id"]): next(iter(repo.list_daily_checks(project_id=int(row["id"]), limit=1)), None)
        for row in projects
    }
    active = sum(1 for row in projects if row["status"] in {"active", "needs_review"})
    exits = sum(1 for row in projects if row["status"] in {"exit_signal", "closed"})
    needs_review = sum(1 for row in projects if project_needs_review(row))
    returns = [perf.return_pct for perf in performances.values() if perf.return_pct is not None]
    avg_return = sum(returns) / len(returns) if returns else None
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    generated_stamp = f"按开仓时间从新到旧 · 最后生成：{now}"

    project_cards = "\n".join(
        render_project_card(repo, row, performances[int(row["id"])])
        for row in projects
    ) or "<div class='empty-state'>暂无跟踪项目</div>"
    source_filter = render_source_filter(projects)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Signal Track</title>
  <style>
    :root {{
      color-scheme: light;
      --midnight: #0D352B;
      --paper: #F7F1E3;
      --celeste: #D8E2D8;
      --herb: #526B45;
      --ink: #19342B;
      --muted: #746D5F;
      --line: rgba(13,53,43,.22);
      --panel: rgba(255,252,244,.76);
      --green: #2C7654;
      --red: #9A4D35;
      --amber: #A96B3C;
      --cyan: #237D78;
      --wash: #E8DDC9;
      --pencil: rgba(25,52,43,.34);
    }}
    * {{ box-sizing: border-box; }}
    html {{ overflow-x: hidden; background: var(--paper); }}
    body {{
      margin: 0;
      background:
        radial-gradient(rgba(25,52,43,.08) .7px, transparent .8px),
        linear-gradient(180deg, rgba(216,226,216,.58), rgba(247,241,227,.96) 340px),
        var(--paper);
      background-size: 8px 8px, auto, auto;
      color: var(--ink);
      font-family: "Noto Serif SC", "Source Han Serif SC", "Songti SC", Georgia, serif;
      overflow-x: hidden;
    }}
    .shell {{ max-width: 1280px; margin: 0 auto; padding: 28px 24px 34px; }}
    .topbar {{
      position: relative; display: flex; align-items: end; justify-content: space-between; gap: 22px; padding: 14px 0 26px; margin-bottom: 18px;
    }}
    .topbar::after {{ content: ""; position: absolute; left: 0; right: 120px; bottom: 7px; height: 11px; border-bottom: 2px solid rgba(154,77,53,.58); border-radius: 50%; transform: rotate(-.45deg); }}
    .brand-kicker {{ color: var(--herb); font: 700 12px/16px "Kaiti SC", "STKaiti", serif; letter-spacing: .08em; margin-bottom: 4px; }}
    h1 {{ margin: 0; font-size: 52px; line-height: 58px; letter-spacing: 0; color: var(--midnight); font-weight: 800; }}
    .stamp {{ color: var(--muted); font-size: 13px; line-height: 20px; }}
    .top-aside {{ display: flex; align-items: end; justify-content: end; text-align: right; min-width: min(440px, 100%); }}
    .top-aside .stamp {{ max-width: 440px; }}
    .summary-strip {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border: 1.4px solid rgba(13,53,43,.28); background: rgba(255,252,244,.58); margin-bottom: 18px; box-shadow: 5px 7px 0 rgba(82,107,69,.10); }}
    .metric {{ min-height: 70px; padding: 12px 16px; border-right: 1px solid rgba(13,53,43,.18); }}
    .metric:last-child {{ border-right: 0; }}
    .metric span {{ color: var(--muted); display: block; font: 700 12px/17px "Kaiti SC", "STKaiti", serif; letter-spacing: .04em; }}
    .metric strong {{ color: var(--midnight); display: block; font: 800 28px/34px "Noto Serif SC", "Source Han Serif SC", serif; font-variant-numeric: tabular-nums; }}
    .filter-bar {{ display: flex; align-items: center; flex-wrap: wrap; gap: 9px; margin: 0 0 16px; }}
    .filter-label {{ color: var(--muted); font-size: 13px; line-height: 20px; }}
    .source-chip {{ color: var(--midnight); background: rgba(255,252,244,.66); border: 1.3px solid rgba(13,53,43,.28); border-radius: 5px; min-height: 32px; padding: 0 13px; cursor: pointer; font: inherit; font-size: 14px; box-shadow: 2px 3px 0 rgba(82,107,69,.08); }}
    .source-chip:hover, .source-chip.active {{ background: var(--midnight); border-color: var(--midnight); color: var(--paper); }}
    .project-list {{ display: grid; gap: 14px; }}
    .project-card {{ position: relative; z-index: 0; border: 1.4px solid rgba(13,53,43,.30); background: rgba(255,252,244,.74); box-shadow: 6px 8px 0 rgba(82,107,69,.10), 0 18px 38px rgba(13,53,43,.08); border-radius: 6px; overflow: visible; }}
    .project-card:hover, .project-card:focus-within {{ z-index: 80; }}
    .project-card::before {{ content: ""; position: absolute; inset: 7px; border: 1px solid rgba(13,53,43,.12); border-radius: 4px; pointer-events: none; }}
    .project-main, .leg-row {{ display: grid; grid-template-columns: 82px minmax(190px, 1.15fr) minmax(340px, 1.75fr) 112px 92px 34px; gap: 12px; align-items: stretch; min-height: 92px; padding: 10px 16px; }}
    .project-id {{ color: var(--midnight); font: 800 17px/20px "IBM Plex Mono", "Geist Mono", monospace; }}
    .project-id-stack {{ align-self: center; position: relative; display: grid; gap: 7px; justify-items: start; z-index: 4; }}
    .project-id, .project-title, .project-return, .status-pill, .expand-button {{ align-self: center; }}
    .focus-info {{ position: relative; display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; border: 1.2px solid rgba(169,107,60,.55); border-radius: 999px; background: rgba(255,252,244,.82); color: var(--amber); cursor: help; font: 800 14px/1 "IBM Plex Mono", "Geist Mono", monospace; box-shadow: 2px 2px 0 rgba(82,107,69,.08); padding: 0; }}
    .focus-info:hover, .focus-info:focus-visible {{ background: var(--midnight); border-color: var(--midnight); color: var(--paper); outline: none; }}
    .focus-popover {{ position: absolute; left: 0; top: calc(100% + 8px); z-index: 120; width: max-content; min-width: 220px; max-width: min(360px, calc(100vw - 48px)); padding: 10px 12px; border: 1px solid rgba(247,241,227,.42); border-radius: 5px; background: rgba(13,53,43,.96); color: var(--paper); box-shadow: 0 16px 34px rgba(13,53,43,.22); opacity: 0; visibility: hidden; transform: translateY(-4px); transition: opacity .12s ease, transform .12s ease, visibility .12s ease; pointer-events: none; white-space: normal; text-align: left; font-family: "Noto Serif SC", "Source Han Serif SC", "Songti SC", Georgia, serif; }}
    .focus-popover::before {{ content: ""; position: absolute; left: 7px; top: -6px; width: 10px; height: 10px; background: rgba(13,53,43,.96); border-left: 1px solid rgba(247,241,227,.42); border-top: 1px solid rgba(247,241,227,.42); transform: rotate(45deg); }}
    .focus-info:hover .focus-popover, .focus-info:focus-visible .focus-popover {{ opacity: 1; visibility: visible; transform: translateY(0); }}
    .focus-popover-title {{ display: block; color: #F0C094; font: 700 12px/17px "Kaiti SC", "STKaiti", serif; margin-bottom: 4px; }}
    .focus-summary {{ display: block; color: rgba(247,241,227,.92); font-size: 13px; line-height: 19px; margin-bottom: 6px; }}
    .focus-signal {{ display: block; position: relative; color: rgba(247,241,227,.84); font-size: 13px; line-height: 19px; padding-left: 12px; }}
    .focus-signal::before {{ content: ""; position: absolute; left: 0; top: .72em; width: 4px; height: 4px; border-radius: 999px; background: #F0C094; }}
    .project-title {{ min-width: 0; }}
    .project-title strong {{ display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 18px; line-height: 24px; color: var(--midnight); }}
    .project-title .title-line {{ display: flex; align-items: baseline; overflow: hidden; text-overflow: clip; white-space: nowrap; }}
    .project-title .title-line span {{ display: inline; font-size: inherit; line-height: inherit; white-space: nowrap; }}
    .project-title .title-name {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; color: var(--midnight); }}
    .project-title .title-suffix {{ flex: 0 0 auto; margin-left: 1em; color: var(--amber); }}
    .project-title span {{ color: var(--muted); display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; line-height: 19px; }}
    .project-title .rule-line {{ margin-top: 8px; color: var(--muted); font: 13px/19px "Kaiti SC", "STKaiti", serif; white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
    .chart-wrap {{ min-width: 0; height: 100%; display: grid; grid-template-rows: minmax(0, 1fr) auto; align-items: stretch; }}
    .chart-meta {{ color: var(--muted); display: flex; justify-content: space-between; gap: 10px; font-size: 12px; line-height: 18px; font-family: "Kaiti SC", "STKaiti", serif; }}
    .project-return {{ text-align: right; font: 800 20px/24px "IBM Plex Mono", "Geist Mono", monospace; }}
    .status-pill {{ display: inline-flex; justify-self: end; align-items: center; justify-content: center; min-height: 26px; padding: 0 9px; border: 1.2px solid rgba(13,53,43,.25); border-radius: 5px; font-size: 13px; color: var(--midnight); background: rgba(216,226,216,.70); white-space: nowrap; box-shadow: 2px 2px 0 rgba(82,107,69,.08); }}
    .status-pill.closed {{ color: white; background: var(--herb); border-color: var(--herb); }}
    .status-pill.exit_signal {{ color: white; background: var(--amber); border-color: var(--amber); }}
    .expand-button {{ width: 30px; height: 30px; border: 1.3px solid rgba(13,53,43,.30); border-radius: 5px; color: var(--midnight); background: rgba(255,252,244,.70); cursor: pointer; font-size: 15px; line-height: 1; box-shadow: 2px 2px 0 rgba(82,107,69,.08); }}
    .expand-button[disabled] {{ opacity: .28; cursor: default; }}
    .project-card.expanded .expand-button {{ transform: rotate(180deg); }}
    .leg-panel {{ display: none; border-top: 1px solid rgba(13,53,43,.16); background: rgba(232,221,201,.35); padding: 0; }}
    .project-card.expanded .leg-panel {{ display: grid; gap: 0; }}
    .leg-row {{ border-top: 1px solid rgba(13,53,43,.10); }}
    .leg-row:first-child {{ border-top: 0; }}
    .leg-name {{ grid-column: 2; align-self: center; min-width: 0; }}
    .leg-title-line {{ display: flex; align-items: baseline; gap: 10px; min-width: 0; }}
    .leg-name strong {{ color: var(--midnight); display: block; font-size: 15px; line-height: 20px; }}
    .leg-title-line strong {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .leg-weight {{ color: var(--amber); flex: 0 0 auto; font: 800 18px/22px "IBM Plex Mono", "Geist Mono", monospace; }}
    .leg-entry {{ color: var(--muted); display: block; font-size: 12px; line-height: 17px; margin-top: 2px; }}
    .leg-row .mini-chart {{ grid-column: 3; align-self: stretch; }}
    .leg-return {{ grid-column: 4; align-self: center; text-align: right; font: 800 20px/24px "IBM Plex Mono", "Geist Mono", monospace; }}
    .leg-contribution {{ grid-column: 5; align-self: center; justify-self: end; text-align: right; font: 800 16px/20px "IBM Plex Mono", "Geist Mono", monospace; }}
    .symbol {{ color: var(--midnight); font-family: "IBM Plex Mono", "Geist Mono", monospace; }}
    .positive {{ color: var(--red); }}
    .negative {{ color: var(--green); }}
    .muted {{ color: var(--muted); }}
    .chart, .mini-chart {{ width: 100%; height: 100%; margin: 0; border: 1.2px solid rgba(13,53,43,.20); border-radius: 5px; background: rgba(255,252,244,.62); box-shadow: inset 0 0 0 1px rgba(255,252,244,.7), 2px 3px 0 rgba(82,107,69,.08); cursor: crosshair; }}
    .chart {{ min-height: 66px; }}
    .mini-chart {{ min-height: 74px; }}
    .entry-baseline {{ stroke: rgba(169,107,60,.34); stroke-width: 1; stroke-dasharray: 6 5; }}
    .chart-marker circle {{ fill: var(--amber); stroke: rgba(247,241,227,.95); stroke-width: 2; }}
    .chart-marker-line {{ stroke: var(--amber); stroke-width: 1.4; stroke-dasharray: 4 4; opacity: .9; }}
    .chart-marker text {{ fill: var(--amber); font: 600 13px/1 "Geist Mono", monospace; paint-order: stroke; stroke: rgba(252,243,227,.9); stroke-width: 3px; }}
    .chart-hover-hit {{ fill: transparent; pointer-events: all; }}
    .chart-hover-line {{ opacity: 0; stroke: var(--amber); stroke-width: 1.4; stroke-dasharray: 4 4; pointer-events: none; }}
    .chart-hover-dot {{ opacity: 0; fill: var(--amber); stroke: rgba(247,241,227,.96); stroke-width: 2.2; pointer-events: none; }}
    .chart-hover-label {{ opacity: 0; pointer-events: none; }}
    .chart-hover-label rect {{ fill: rgba(13,53,43,.92); stroke: rgba(247,241,227,.55); stroke-width: 1; }}
    .chart-hover-label text {{ fill: var(--paper); font: 700 12px/1 "IBM Plex Mono", "Geist Mono", monospace; }}
    .chart-hover-label .chart-hover-value {{ fill: #F0C094; }}
    .chart-hover-label .chart-hover-change {{ fill: rgba(247,241,227,.78); }}
    .chart-hover-active .chart-hover-line, .chart-hover-active .chart-hover-dot, .chart-hover-active .chart-hover-label {{ opacity: 1; }}
    .empty-state {{ border: 1px solid rgba(13,53,43,.18); background: rgba(255,252,244,.52); border-radius: 6px; padding: 28px; color: var(--muted); }}
    @media (max-width: 900px) {{
      .shell {{ padding: 16px; }}
      .summary-strip {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .topbar {{ align-items: start; flex-direction: column; }}
      .top-aside {{ justify-content: start; text-align: left; min-width: 0; }}
      .project-main {{ grid-template-columns: 74px minmax(0, 1fr) 74px 30px; }}
      .focus-popover {{ min-width: 210px; max-width: min(320px, calc(100vw - 36px)); }}
      .chart-wrap {{ grid-column: 1 / -1; }}
      .project-title .rule-line {{ -webkit-line-clamp: 3; }}
      .project-return {{ text-align: left; }}
      .leg-row {{ grid-template-columns: 1fr; min-height: 96px; }}
      .leg-name, .leg-row .mini-chart, .leg-return, .leg-contribution {{ grid-column: 1; }}
      .leg-return {{ text-align: left; }}
      .leg-contribution {{ justify-self: start; text-align: left; }}
    }}
    @media (max-width: 520px) {{
      .shell {{ padding: 12px; }}
      h1 {{ font-size: 22px; line-height: 30px; }}
      .summary-strip {{ grid-template-columns: 1fr 1fr; }}
      .project-main {{ padding: 8px; gap: 8px; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="topbar">
      <div class="brand-block">
        <div class="brand-kicker">EDITORIAL CURVE NOTE</div>
        <h1>Signal Track</h1>
      </div>
      <div class="top-aside">
        <div class="stamp">{escape(generated_stamp)}</div>
      </div>
    </section>
    <section class="summary-strip" aria-label="项目概览">
      <div class="metric"><span>项目</span><strong>{len(projects)}</strong></div>
      <div class="metric"><span>跟踪中</span><strong>{active}</strong></div>
      <div class="metric"><span>已触发平仓</span><strong>{exits}</strong></div>
      <div class="metric"><span>平均收益</span><strong>{format_return(avg_return)}</strong></div>
    </section>
    <section class="filter-bar" aria-label="按信息源筛选">
      <span class="filter-label">信息源</span>{source_filter}
    </section>
    <section class="project-list">{project_cards}</section>
  </main>
  <script>
    (() => {{
      const chips = Array.from(document.querySelectorAll('[data-filter-type]'));
      const cards = Array.from(document.querySelectorAll('.project-card[data-source]'));
      const state = {{ source: 'all' }};
      const matches = (node) => {{
        return state.source === 'all' || node.dataset.source === state.source;
      }};
      const applyFilters = () => {{
        chips.forEach((chip) => chip.classList.toggle('active', state[chip.dataset.filterType] === chip.dataset.value));
        cards.forEach((node) => {{
          node.hidden = !matches(node);
        }});
      }};
      chips.forEach((chip) => chip.addEventListener('click', () => {{
        state[chip.dataset.filterType] = chip.dataset.value || 'all';
        applyFilters();
      }}));
      document.querySelectorAll('[data-toggle-project]').forEach((button) => {{
        button.addEventListener('click', () => {{
          const card = button.closest('.project-card');
          if (!card) return;
          const expanded = card.classList.toggle('expanded');
          button.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        }});
      }});
      let activeChart = null;
      const clearChartHover = (chart) => {{
        if (!chart) return;
        chart.classList.remove('chart-hover-active');
      }};
      const chartHoverPoints = (chart) => {{
        if (!chart.__hoverPoints) {{
          try {{
            chart.__hoverPoints = JSON.parse(chart.dataset.chartHover || '[]');
          }} catch (error) {{
            chart.__hoverPoints = [];
          }}
        }}
        return chart.__hoverPoints;
      }};
      const nearestChartPoint = (points, x) => {{
        return points.reduce((nearest, point) => {{
          if (!nearest) return point;
          return Math.abs(point.x - x) < Math.abs(nearest.x - x) ? point : nearest;
        }}, null);
      }};
      const closestElement = (target, selector) => {{
        return target && target.closest ? target.closest(selector) : null;
      }};
      const updateChartHover = (event, chart) => {{
        const points = chartHoverPoints(chart);
        if (!points.length) return;
        if (activeChart && activeChart !== chart) clearChartHover(activeChart);
        activeChart = chart;
        const rect = chart.getBoundingClientRect();
        const x = ((event.clientX - rect.left) / Math.max(1, rect.width)) * 640;
        const point = nearestChartPoint(points, x);
        if (!point) return;
        chart.classList.add('chart-hover-active');
        const line = chart.querySelector('.chart-hover-line');
        const dot = chart.querySelector('.chart-hover-dot');
        const label = chart.querySelector('.chart-hover-label');
        const dateText = chart.querySelector('.chart-hover-date');
        const valueText = chart.querySelector('.chart-hover-value');
        const changeText = chart.querySelector('.chart-hover-change');
        if (line) {{
          line.setAttribute('x1', point.x.toFixed(1));
          line.setAttribute('x2', point.x.toFixed(1));
        }}
        if (dot) {{
          dot.setAttribute('cx', point.x.toFixed(1));
          dot.setAttribute('cy', point.y.toFixed(1));
        }}
        if (dateText) dateText.textContent = point.date;
        if (valueText) valueText.textContent = `${{point.label}} ${{point.value}}`;
        if (changeText) changeText.textContent = point.change || '';
        if (label) {{
          const labelRect = label.querySelector('rect');
          const textWidths = [dateText, valueText, changeText]
            .filter((node) => node && node.textContent)
            .map((node) => {{
              try {{
                return node.getBBox().width;
              }} catch (error) {{
                return (node.textContent || '').length * 7;
              }}
            }});
          const labelWidth = Math.ceil(Math.max(78, ...textWidths) + 18);
          const labelHeight = point.change ? 52 : 38;
          if (labelRect) {{
            labelRect.setAttribute('width', labelWidth.toFixed(0));
            labelRect.setAttribute('height', labelHeight.toFixed(0));
          }}
          let labelX = point.x + 10;
          if (labelX + labelWidth > 634) labelX = point.x - labelWidth - 10;
          labelX = Math.max(6, Math.min(640 - labelWidth - 6, labelX));
          let labelY = point.y + 10;
          if (labelY + labelHeight > 114) labelY = point.y - labelHeight - 10;
          labelY = Math.max(6, Math.min(120 - labelHeight - 6, labelY));
          label.setAttribute('transform', `translate(${{labelX.toFixed(1)}} ${{labelY.toFixed(1)}})`);
        }}
      }};
      document.addEventListener('mousemove', (event) => {{
        const chart = closestElement(event.target, 'svg[data-chart-hover]');
        if (chart) {{
          updateChartHover(event, chart);
          return;
        }}
        if (activeChart) {{
          clearChartHover(activeChart);
          activeChart = null;
        }}
      }});
      document.addEventListener('mouseleave', () => {{
        clearChartHover(activeChart);
        activeChart = null;
      }});
      applyFilters();
    }})();
  </script>
</body>
</html>"""


def render_project_card(repo: Repository, row, performance) -> str:
    legs = performance.legs
    is_portfolio = len(legs) > 1
    card_classes = "project-card is-portfolio" if is_portfolio else "project-card"
    source = row["source_name"] or "manual"
    symbols = row["symbols"] or "--"
    title = row["title"] or symbols
    status = str(row["status"])
    window = f"{performance.window_start or '--'} 至 {performance.window_end or '--'}"
    expand = (
        "<button class='expand-button' type='button' data-toggle-project aria-expanded='false' title='展开组合标的'>⌄</button>"
        if is_portfolio
        else "<button class='expand-button' type='button' disabled aria-hidden='true'>·</button>"
    )
    leg_panel = render_leg_rows(legs, row["entry_date"], row["closed_date"], performance.window_start, performance.window_end)
    hover_price_points = single_leg_price_points(performance)
    hover_reference_value = single_leg_entry_price(performance)
    return (
        f"<article class='{card_classes}' data-source='{escape(source)}' data-status='{escape(status)}'>"
        "<div class='project-main'>"
        f"{render_project_id_stack(repo, row)}"
        "<div class='project-title'>"
        f"{render_project_title(title)}"
        f"<span>{escape(source)} · {escape(symbols)}</span>"
        f"<span class='rule-line'>{escape(project_rule_line(row))}</span>"
        "</div>"
        "<div class='chart-wrap'>"
        f"{render_sparkline(performance.points, trade_markers=project_chart_markers(row['entry_date'], row['closed_date']), window_start=performance.window_start, window_end=performance.window_end, hover_points=hover_price_points, hover_value_mode='price' if hover_price_points else None, hover_reference_value=hover_reference_value)}"
        f"<div class='chart-meta'><span>{escape(window)}</span><span>{escape(default_rule_label(row))}</span></div>"
        "</div>"
        f"<div class='project-return {return_css(performance.return_pct)}'>{format_return(performance.return_pct)}</div>"
        f"<span class='status-pill {escape(status)}'>{escape(compact_status_label(status))}</span>"
        f"{expand}"
        "</div>"
        f"{leg_panel}"
        "</article>"
    )


def render_project_id_stack(repo: Repository, row) -> str:
    summary, signals = project_focus_brief(repo, row)
    return (
        "<div class='project-id-stack'>"
        f"<div class='project-id'>{escape(project_id_label(row))}</div>"
        f"{render_focus_info(summary, signals)}"
        "</div>"
    )


def render_focus_info(summary: str | None, signals: list[str]) -> str:
    clean_summary = (summary or "").strip()
    clean_signals = [signal.strip() for signal in signals if signal and signal.strip()]
    if not clean_summary and not clean_signals:
        return ""
    summary_html = f"<span class='focus-summary'>{escape(clean_summary)}</span>" if clean_summary else ""
    signal_html = "".join(
        f"<span class='focus-signal'>{escape(signal)}</span>"
        for signal in clean_signals[:4]
    )
    return (
        "<button class='focus-info' type='button' aria-label='重点跟踪信号' data-focus-info>"
        "i"
        "<span class='focus-popover'>"
        "<span class='focus-popover-title'>重点观察</span>"
        f"{summary_html}"
        f"{signal_html}"
        "</span>"
        "</button>"
    )


def project_focus_brief(repo: Repository, row) -> tuple[str | None, list[str]]:
    del repo
    metadata = project_metadata(row)
    summary = normalize_focus_text(metadata.get("tracking_focus_summary"))
    signals = normalize_focus_signals(metadata.get("tracking_focus_signals"))
    return summary, signals


def normalize_focus_signals(raw: object) -> list[str]:
    if isinstance(raw, list):
        candidates = [normalize_focus_text(item) for item in raw]
    elif isinstance(raw, str):
        candidates = [normalize_focus_text(item) for item in re.split(r"[\n；;]+", raw)]
    else:
        candidates = []
    return [item for item in candidates if item][:4]


def normalize_focus_text(raw: object) -> str | None:
    if raw is None:
        return None
    text = localize_text(raw).strip()
    text = re.sub(r"\s+", " ", text)
    return text or None


def render_project_title(title: str) -> str:
    name, suffix = split_tracking_title(title)
    if not suffix:
        return f"<strong>{escape(title)}</strong>"
    return (
        "<strong class='title-line'>"
        f"<span class='title-name'>{escape(name)}</span>"
        f"<span class='title-suffix'>{escape(suffix)}</span>"
        "</strong>"
    )


def single_leg_price_points(performance) -> list[tuple[str, float]] | None:
    if len(performance.legs) != 1:
        return None
    return performance.legs[0].price_points or None


def single_leg_entry_price(performance) -> float | None:
    if len(performance.legs) != 1:
        return None
    return performance.legs[0].entry_price


def split_tracking_title(title: str) -> tuple[str, str | None]:
    for suffix in ("做多跟踪", "做空跟踪", "观察跟踪", "中性跟踪"):
        marker = f" {suffix}"
        if title.endswith(marker):
            return title[: -len(marker)].rstrip(), suffix
    return title, None


def render_leg_rows(
    legs,
    project_entry_date: str | None,
    project_closed_date: str | None,
    window_start: str | None,
    window_end: str | None,
) -> str:
    if len(legs) <= 1:
        return ""
    rows = []
    for leg in legs:
        contribution = leg_contribution_pct(leg)
        rows.append(
            "<div class='leg-row'>"
            "<div class='leg-name'>"
            "<div class='leg-title-line'>"
            f"<strong>{escape(display_leg_name(leg))}</strong>"
            f"<span class='leg-weight'>权重 {leg.weight:.0%}</span>"
            "</div>"
            f"<span class='leg-entry'><span class='symbol'>{escape(leg.symbol)}</span> · 入场 {escape(leg.entry_date or project_entry_date or '--')}</span>"
            "</div>"
            f"{render_sparkline(leg.price_points, css_class='mini-chart', label=f'{leg.symbol} 价格曲线', show_zero=False, trade_markers=project_chart_markers(project_entry_date, project_closed_date), window_start=window_start, window_end=window_end, hover_reference_value=leg.entry_price)}"
            f"<div class='leg-return {return_css(leg.return_pct)}' title='标的自身涨跌'>{format_return(leg.return_pct)}</div>"
            f"<div class='leg-contribution {return_css(contribution)}' title='对组合收益的贡献'>{format_return(contribution)}</div>"
            "</div>"
        )
    return "<div class='leg-panel'>" + "".join(rows) + "</div>"


def leg_contribution_pct(leg) -> float | None:
    if leg.return_pct is None:
        return None
    return leg.return_pct * leg.weight


def project_id_label(row) -> str:
    return f"#{int(row['id']):03d}"


def default_rule_label(row) -> str:
    if row["closed_date"]:
        return f"平仓 {row['closed_date']}"
    metadata = project_metadata(row)
    if metadata.get("hold_until_label"):
        return str(metadata["hold_until_label"])
    if metadata.get("hold_until"):
        return f"持有到 {metadata['hold_until']}"
    return "默认规则"


def project_rule_line(row) -> str:
    metadata = project_metadata(row)
    if metadata.get("rule_line"):
        return str(metadata["rule_line"])
    if metadata.get("default_close_rule") == "disabled_until" and metadata.get("hold_until"):
        return f"策略：持有到 {metadata['hold_until']}；到期前不触发默认跌 20% 平仓/回撤止盈。"
    if metadata.get("default_close_rule") == "disabled":
        return "策略：不使用默认跌 20% 平仓/回撤止盈规则。"
    return "默认：跌 20% 平仓；从最高点回撤 20% 止盈。触发后曲线继续跟踪 1 个月。"


def project_metadata(row) -> dict:
    try:
        metadata = json.loads(row["metadata"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


def compact_status_label(status: str) -> str:
    labels = {
        "active": "跟踪中",
        "needs_review": "跟踪中",
        "exit_signal": "已触发",
        "closed": "已平仓",
        "watch_after_close": "平仓后",
        "archived": "归档",
    }
    return labels.get(status, status)


def display_leg_name(leg) -> str:
    name = str(leg.name or "").strip()
    symbol = str(leg.symbol or "").strip()
    if not name:
        return symbol
    for suffix in (f" · {symbol}", f" {symbol}", f"({symbol})", f"（{symbol}）"):
        if symbol and name.endswith(suffix):
            return name[: -len(suffix)].rstrip()
    return name


def safe_json(raw: str | None) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
    check_log = render_project_check_log(
        repo.list_daily_checks(project_id=int(row["id"]), limit=5),
        suppress_resolved_low_logic=not project_needs_review(row),
    )
    report_snapshot = render_report_snapshot(repo, int(row["id"]))
    research_items = render_research_items(repo.list_research_items(project_id=int(row["id"]), limit=8))
    input_history = render_project_inputs(project_input_history(repo, int(row["id"]), limit=5))
    hover_price_points = single_leg_price_points(performance)
    hover_reference_value = single_leg_entry_price(performance)
    legs = "\n".join(
        f"<span class='leg'>{escape(leg.symbol)} · {leg.weight:.0%} · {format_return(leg.return_pct)}</span>"
        for leg in performance.legs
    )
    leg_curves = "\n".join(
        render_leg_curve(
            leg,
            row["entry_date"],
            row["closed_date"],
            performance.window_start,
            performance.window_end,
        )
        for leg in performance.legs
    )
    return (
        f"<article class='card detail-card' data-source='{escape(row['source_name'])}' "
        f"data-status='{escape(row['status'])}' data-direction='{escape(row['direction'])}'>"
        "<div class='detail-top'>"
        f"<div><h3>{escape(row['title'])}</h3><div class='muted'>{escape(row['source_name'])} · {escape(row['symbols'] or '')}</div></div>"
        f"<strong class='{return_css(performance.return_pct)}'>{format_return(performance.return_pct)}</strong>"
        "</div>"
        f"{render_performance_window(row, performance)}"
        f"{render_sparkline(performance.points, trade_markers=project_chart_markers(row['entry_date'], row['closed_date']), window_start=performance.window_start, window_end=performance.window_end, hover_points=hover_price_points, hover_value_mode='price' if hover_price_points else None, hover_reference_value=hover_reference_value)}"
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


def render_project_check_log(checks, suppress_resolved_low_logic: bool = False) -> str:
    if suppress_resolved_low_logic:
        checks = [row for row in checks if not is_low_logic_review_check(row)]
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


def is_low_logic_review_check(row) -> bool:
    try:
        rules = json.loads(row["triggered_rules"] or "[]")
    except json.JSONDecodeError:
        return False
    return row["conclusion"] == "needs_review" and any("Project logic score" in str(rule) for rule in rules)


def is_resolved_low_logic_check(check_row, project_row) -> bool:
    return bool(project_row) and not project_needs_review(project_row) and is_low_logic_review_check(check_row)


def render_leg_curve(
    leg,
    project_entry_date: str | None,
    project_closed_date: str | None,
    window_start: str | None,
    window_end: str | None,
) -> str:
    return (
        "<div class='leg-curve'>"
        "<div class='leg-curve-head'>"
        f"<strong>{escape(display_leg_name(leg))} · {leg.weight:.0%}</strong>"
        f"<span class='{return_css(leg.return_pct)}'>{format_price(leg.latest_price)} · {format_return(leg.return_pct)}</span>"
        "</div>"
        f"<div class='muted'><span class='symbol'>{escape(leg.symbol)}</span> · 入场 {escape(leg.entry_date or project_entry_date or '--')}</div>"
        f"{render_sparkline(leg.price_points, css_class='mini-chart', label=f'{leg.symbol} 价格曲线', show_zero=False, trade_markers=project_chart_markers(project_entry_date, project_closed_date), window_start=window_start, window_end=window_end, hover_reference_value=leg.entry_price)}"
        "</div>"
    )


def render_sparkline(
    points: list[tuple[str, float]],
    css_class: str = "chart",
    label: str = "收益曲线",
    show_zero: bool = True,
    trade_markers: list[dict[str, str]] | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    hover_points: list[tuple[str, float]] | None = None,
    hover_value_mode: str | None = None,
    hover_reference_value: float | None = None,
) -> str:
    if len(points) < 2:
        return f"<div class='{escape(css_class)} empty'>暂无价格曲线。运行 check --provider 或 fetch-bars 后显示。</div>"
    width = 640
    height = 120
    domain_start, domain_end = chart_domain(points, window_start, window_end)
    plot_points = extend_points_to_domain(points, domain_start, domain_end)
    values = [value for _, value in plot_points]
    minimum = min(values)
    maximum = max(values)
    span = maximum - minimum or 1
    coords = []
    coord_pairs = []
    for point_date, value in plot_points:
        x = chart_x(point_date, domain_start, domain_end, width)
        y = height - ((value - minimum) / span * (height - 18)) - 9
        coord_pairs.append((x, y))
        coords.append(f"{x:.1f},{y:.1f}")
    zero_line = ""
    if show_zero:
        zero_y = height - ((0 - minimum) / span * (height - 18)) - 9
        zero_y = max(8, min(height - 8, zero_y))
        zero_line = f"<line x1='0' y1='{zero_y:.1f}' x2='640' y2='{zero_y:.1f}' stroke='rgba(169,107,60,.18)' />"
    markers = curve_boundary_markers(domain_start.isoformat(), domain_end.isoformat()) + (trade_markers or [])
    baseline_html = render_entry_baseline(plot_points, coords, markers, width)
    value_mode = "return" if show_zero else "price"
    marker_html = render_chart_markers(
        plot_points,
        coords,
        markers,
        domain_start,
        domain_end,
        width,
        value_mode=value_mode,
    )
    hover_payload = chart_hover_payload(
        plot_points,
        coord_pairs,
        value_mode,
        hover_points=hover_points,
        hover_value_mode=hover_value_mode,
        hover_reference_value=hover_reference_value,
    )
    hover_html = render_chart_hover_layer(width, height)
    return (
        f"<svg class='{escape(css_class)}' viewBox='0 0 640 120' preserveAspectRatio='none' role='img' aria-label='{escape(label)}' data-chart-hover='{hover_payload}'>"
        f"{zero_line}"
        f"{baseline_html}"
        f"<polyline points='{' '.join(coords)}' fill='none' stroke='#237D78' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round' />"
        f"{marker_html}"
        f"{hover_html}"
        "</svg>"
    )


def chart_hover_payload(
    points: list[tuple[str, float]],
    coord_pairs: list[tuple[float, float]],
    value_mode: str,
    hover_points: list[tuple[str, float]] | None = None,
    hover_value_mode: str | None = None,
    hover_reference_value: float | None = None,
) -> str:
    display_mode = hover_value_mode or value_mode
    value_label = "收益" if display_mode == "return" else "价格"
    payload = [
        {
            "x": round(x, 1),
            "y": round(y, 1),
            "date": point_date,
            "label": value_label,
            "value": marker_value_label(display_value, display_mode),
            "change": hover_change_label(display_value, display_mode, hover_reference_value),
        }
        for (point_date, value), (x, y) in zip(points, coord_pairs)
        for display_value in [hover_value_for_date(hover_points, point_date, value)]
    ]
    return escape(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def hover_change_label(value: float, display_mode: str, reference: float | None) -> str:
    if display_mode == "return":
        return f"较开仓 {format_chart_return(value)}"
    if reference is None or reference == 0:
        return ""
    return f"较开仓 {format_chart_return((value / reference) - 1)}"


def hover_value_for_date(
    hover_points: list[tuple[str, float]] | None,
    point_date: str,
    fallback: float,
) -> float:
    if not hover_points:
        return fallback
    index = chart_marker_index(hover_points, point_date)
    if index is None:
        return fallback
    hover_date, hover_value = hover_points[index]
    if hover_date > point_date and index > 0:
        return hover_points[index - 1][1]
    return hover_value


def render_chart_hover_layer(width: int, height: int) -> str:
    return (
        "<g class='chart-hover' aria-hidden='true'>"
        f"<line class='chart-hover-line' x1='0' y1='8' x2='0' y2='{height - 16}' />"
        "<circle class='chart-hover-dot' cx='0' cy='0' r='4.8' />"
        "<g class='chart-hover-label' transform='translate(8 8)'>"
        "<rect width='96' height='52' rx='4' />"
        "<text class='chart-hover-date' x='9' y='15'></text>"
        "<text class='chart-hover-value' x='9' y='30'></text>"
        "<text class='chart-hover-change' x='9' y='45'></text>"
        "</g>"
        "</g>"
        f"<rect class='chart-hover-hit' x='0' y='0' width='{width}' height='{height}' />"
    )


def project_chart_markers(entry_date: str | None, closed_date: str | None) -> list[dict[str, str]]:
    markers = []
    if entry_date:
        markers.append({"date": entry_date, "label": "开", "title_label": "开仓", "kind": "open"})
    if closed_date:
        markers.append({"date": closed_date, "label": "平", "title_label": "平仓", "kind": "close"})
    return markers


def chart_domain(
    points: list[tuple[str, float]],
    window_start: str | None,
    window_end: str | None,
) -> tuple[date, date]:
    first_date = date.fromisoformat(points[0][0])
    last_date = date.fromisoformat(points[-1][0])
    domain_start = date.fromisoformat(window_start) if window_start else first_date
    domain_end = date.fromisoformat(window_end) if window_end else last_date
    if domain_end <= domain_start:
        domain_end = last_date if last_date > domain_start else domain_start
    return domain_start, domain_end


def extend_points_to_domain(
    points: list[tuple[str, float]],
    domain_start: date,
    domain_end: date,
) -> list[tuple[str, float]]:
    start_label = domain_start.isoformat()
    end_label = domain_end.isoformat()
    extended = [(point_date, value) for point_date, value in points if start_label <= point_date <= end_label]
    if not extended:
        extended = list(points)
    if extended[0][0] > start_label:
        extended.insert(0, (start_label, extended[0][1]))
    if extended[-1][0] < end_label:
        extended.append((end_label, extended[-1][1]))
    return extended


def chart_x(point_date: str, domain_start: date, domain_end: date, width: int) -> float:
    point = date.fromisoformat(point_date)
    total_days = max(1, (domain_end - domain_start).days)
    offset_days = (point - domain_start).days
    return max(0.0, min(float(width), (offset_days / total_days) * width))


def curve_boundary_markers(start_date: str, end_date: str) -> list[dict[str, str]]:
    markers = [
        {
            "date": start_date,
            "label": "始",
            "title_label": "曲线开始",
            "kind": "curve-start",
            "show_date_label": "false",
        }
    ]
    if end_date != start_date:
        markers.append(
            {
                "date": end_date,
                "label": "末",
                "title_label": "曲线结束",
                "kind": "curve-end",
                "show_date_label": "false",
            }
        )
    return markers


def render_chart_markers(
    points: list[tuple[str, float]],
    coords: list[str],
    markers: list[dict[str, str]],
    domain_start: date,
    domain_end: date,
    width: int,
    value_mode: str,
) -> str:
    if not points or not coords or not markers:
        return ""
    return "".join(chart_marker(points, coords, marker, domain_start, domain_end, width, value_mode) for marker in markers)


def render_entry_baseline(
    points: list[tuple[str, float]],
    coords: list[str],
    markers: list[dict[str, str]],
    width: int,
) -> str:
    open_marker = next((marker for marker in markers if marker.get("kind") == "open"), None)
    if not open_marker:
        return ""
    index = chart_marker_index(points, open_marker["date"])
    if index is None:
        return ""
    _, y_text = coords[index].split(",", 1)
    y = float(y_text)
    title = f"盈亏线：{open_marker['date']}"
    return (
        "<g class='entry-baseline-group'>"
        f"<title>{escape(title)}</title>"
        f"<line class='entry-baseline' x1='0' y1='{y:.1f}' x2='{width}' y2='{y:.1f}' />"
        "</g>"
    )


def chart_marker(
    points: list[tuple[str, float]],
    coords: list[str],
    marker: dict[str, str],
    domain_start: date,
    domain_end: date,
    width: int,
    value_mode: str,
) -> str:
    target_date = marker["date"]
    index = chart_marker_index(points, target_date)
    if index is None:
        return ""
    point = points[index]
    coord = coords[index]
    date_label, value = point
    _, y_text = coord.split(",", 1)
    x = chart_x(target_date, domain_start, domain_end, width)
    y = float(y_text)
    label = marker["label"]
    kind = marker["kind"]
    css_class = f"chart-marker-{kind}"
    text_anchor = marker_text_anchor(x, width)
    text_x = marker_text_x(x, width)
    text_y = marker_text_y(kind)
    plotted_suffix = marker_plotted_suffix(points, target_date, date_label)
    title_label = marker.get("title_label", label)
    title = f"{title_label}点：{target_date}{plotted_suffix} / {value:.4g}"
    value_label = marker_value_label(value, value_mode)
    date_label_text = f"{label} {compact_date(target_date)}"
    vertical_line = ""
    circle = f"<circle cx='{x:.1f}' cy='{y:.1f}' r='5' />"
    if kind in {"open", "close"}:
        vertical_line = f"<line class='chart-marker-line' x1='{x:.1f}' y1='8' x2='{x:.1f}' y2='104' />"
        circle = ""
    date_label_html = ""
    if marker.get("show_date_label") != "false":
        date_label_html = (
            f"<text x='{text_x:.1f}' y='{text_y:.1f}' text-anchor='{text_anchor}'>{escape(date_label_text)}</text>"
        )
    return (
        f"<g class='chart-marker {css_class}'>"
        f"<title>{escape(title)}</title>"
        f"{vertical_line}"
        f"{circle}"
        f"<text x='{text_x:.1f}' y='18' text-anchor='{text_anchor}'>{escape(value_label)}</text>"
        f"{date_label_html}"
        "</g>"
    )


def marker_value_label(value: float, value_mode: str) -> str:
    if value_mode == "return":
        return format_chart_return(value)
    return format_price(value)


def marker_text_anchor(x: float, width: int) -> str:
    if x <= 24:
        return "start"
    if x >= width - 24:
        return "end"
    return "middle"


def marker_text_x(x: float, width: int) -> float:
    if x <= 24:
        return 8
    if x >= width - 24:
        return width - 8
    return x


def marker_text_y(kind: str) -> float:
    return 112


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


def format_chart_return(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:+.1%}"


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


def project_needs_review(row) -> bool:
    if str(row["status"]) == "closed":
        return False
    return bool(row["needs_review"]) or bool(row["weight_needs_review"])


def escape(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)
