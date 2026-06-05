# Design System - Signal Track

## Product Context

- **What this is:** A private investment signal tracking dashboard that turns source notes into monitored positions, curves, daily checks, and exit signals.
- **Who it's for:** A discretionary investor who wants to drop raw source material into the system and quickly see what is active, what changed, and what needs attention.
- **Project type:** Data-heavy dashboard and project detail app.

## Aesthetic Direction

- **Direction:** Futuristic minimalism with glassmorphism.
- **Core pattern:** Card-based design with layered elements, but no cards inside cards.
- **Mood:** Calm, sharp, and analytical. It should feel like a trading desk instrument, not a marketing page.
- **Decoration level:** Intentional. Use translucent surfaces, fine borders, subtle grid texture, and layered elevation. Do not use decorative orbs, bokeh blobs, or generic gradients.

## Typography

- **Display:** `Satoshi` or `General Sans` for page titles and major section labels.
- **Body/UI:** `Geist` for readable Chinese/English mixed UI text.
- **Data/Tables:** `IBM Plex Mono` or `Geist Mono` with tabular numbers for prices, returns, dates, and ticker codes.
- **Fallback stack:** `system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`.
- **Scale:**
  - Page title: 28px / 36px, weight 650
  - Section title: 18px / 26px, weight 650
  - Card title: 15px / 22px, weight 650
  - Body: 14px / 22px, weight 450
  - Table/data: 13px / 20px, weight 500
  - Meta labels: 12px / 18px, weight 500

## Color

- **Approach:** Balanced. Neutral surfaces carry the product; color is reserved for direction, status, and attention.
- **Background:** `#0D0F0E` deep graphite.
- **Surface base:** `rgba(245, 247, 244, 0.055)`.
- **Surface raised:** `rgba(245, 247, 244, 0.085)`.
- **Surface active:** `rgba(245, 247, 244, 0.13)`.
- **Border:** `rgba(231, 238, 232, 0.14)`.
- **Border strong:** `rgba(231, 238, 232, 0.24)`.
- **Text primary:** `#F1F5EF`.
- **Text secondary:** `#AEB9B0`.
- **Text muted:** `#727D75`.
- **Primary accent:** `#44D7C8` signal cyan for active tracking, selected states, and chart highlights.
- **Secondary accent:** `#D8B35D` amber for watch/review states.
- **Long/success:** `#58D68D`.
- **Short/error:** `#FF6B6B`.
- **Info:** `#6AA9FF`.
- **Warning:** `#F2C94C`.

Use green/red only for financial direction or risk. Do not use them as decorative accents.

## Glassmorphism Rules

- Cards use translucent surfaces with `backdrop-filter: blur(18px)` when available.
- Card border radius is 8px.
- Use one shadow family only:
  - Resting: `0 1px 0 rgba(255,255,255,0.06) inset, 0 16px 48px rgba(0,0,0,0.24)`
  - Floating panel: `0 1px 0 rgba(255,255,255,0.08) inset, 0 24px 72px rgba(0,0,0,0.34)`
- Layering should come from sticky headers, side panels, chart overlays, and status strips, not from stacked decorative cards.
- Background texture may use a very subtle grid or noise at low opacity. It must not fight chart readability.

## Layout

- **Approach:** Grid-disciplined dashboard with selective layered panels.
- **Desktop shell:** Left rail navigation, top status bar, main content grid.
- **Mobile shell:** Single column with sticky compact status bar and collapsible filters.
- **Max content width:** 1440px for dashboard pages.
- **Grid:** 12 columns desktop, 6 columns tablet, 1 column mobile.
- **Spacing base:** 8px.
- **Density:** Compact, because the product is for repeated daily use.
- **Spacing scale:** 4, 8, 12, 16, 24, 32, 48, 64.

## Components

### Dashboard Cards

- Use cards for repeated project summaries, metric tiles, alerts, and detail panels.
- Each card has one job: summary, chart, logic, or event log.
- Avoid nested cards. If a card needs internal grouping, use dividers, soft bands, or a left status rail.
- Card header contains title, source, status pill, and timestamp.
- Card body prioritizes the main decision: hold, watch, exit signal, or needs review.

### Tables

- Tables are first-class, not secondary.
- Use sticky headers, compact rows, tabular numbers, and clear status colors.
- Columns for project list:
  - Status
  - Source
  - Instrument
  - Direction
  - Entry date
  - Return
  - Logic score
  - Last check
  - Next action

### Charts

- Use dark chart backgrounds integrated with the page, not white chart boxes.
- Main price line uses primary accent.
- Long/short entry markers use semantic direction colors.
- Exit signals use amber or red depending on severity.
- Portfolio projects show normalized return curves. Individual leg prices can be shown below or behind a segmented control.

### Status Pills

- Keep pills compact and data-like.
- `Active`: cyan outline.
- `Watch`: amber outline.
- `Exit Signal`: red fill at low opacity.
- `Closed`: neutral outline.
- `Needs Review`: amber fill at low opacity.

### Forms And Controls

- Icon buttons for refresh, publish, filter, expand, close, download, and settings.
- Segmented controls for views: Overview, By Source, Active, Signals, Closed.
- Toggles for showing raw logic vs system logic.
- Sliders or number inputs for portfolio weights.
- Menus for source, market, status, and direction filters.

## Page Templates

### Overview

- Top bar: last publish time, active count, exit signal count, today's checks.
- First row: four metric cards.
- Main area: project table on the left, alert rail on the right.
- Lower area: source performance, market exposure, recent updates.

### Project Detail

- Header: instrument, direction, source, status, return, dates.
- Main chart: price or normalized return from one month before entry to current or one month after close.
- Logic area:
  - Original source logic
  - System supplemented logic
  - Key tracking indicators
  - Exit triggers
- Timeline: daily checks and decisions.

### Portfolio Project

- Header shows portfolio name, aggregate direction, status, weighted return.
- Main chart shows weighted portfolio return.
- Leg table shows each instrument, weight, direction, return, and signal state.
- Each leg can expand into its own curve.

## Motion

- **Approach:** Minimal-functional.
- **Duration:** 120ms for hover/focus, 180ms for filter changes, 240ms for panel open/close.
- **Easing:** `cubic-bezier(0.2, 0.8, 0.2, 1)`.
- Motion should clarify state changes. No ornamental looping animation.

## Accessibility And Readability

- Charts and status colors must have text labels. Color alone is not enough.
- Table text must never truncate critical ticker, return, status, or date fields without tooltip/title fallback.
- Glass surfaces must maintain readable contrast in both dense and empty states.
- Numeric columns use right alignment and tabular numbers.

## Decisions Log

| Date | Decision | Rationale |
| --- | --- | --- |
| 2026-06-05 | Use futuristic minimalism with glassmorphism | Matches user's requested direction while keeping the dashboard calm and data-first. |
| 2026-06-05 | Use card-based layered UI without nested cards | Preserves the card-based aesthetic without hurting scan speed or layout clarity. |
| 2026-06-05 | Use cyan/amber/semantic accents over purple gradients | Keeps the system futuristic without falling into generic AI-dashboard styling. |

