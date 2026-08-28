> [!WARNING]
> **Superseded — do not follow this document for new work.**
>
> This is a pre-`paper` capture, scraped from a rendered login page. It predates
> the `--ds-*` token system, the `paper` theme (default since Wave 0, 2026-08) and
> the rail chrome. Its `--font-primary` / `--text-*` names are **not** the tokens
> the app uses today.
>
> The binding visual standard is
> [`.claude/skills/agnes-conventions/references/design-system.md`](../.claude/skills/agnes-conventions/references/design-system.md).
> See also `CLAUDE.md` → *Visual standard (binding for ALL UI work)*.
>
> Kept only as a record of the pre-unification look.

# Design System

Extracted from:
- your-instance.example.com/login

---

## Typography

### Font Family

```css
--font-primary: 'Inter', system-ui, sans-serif;
--font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
```

### Font Sizes

| Token | Size | Usage |
|-------|------|-------|
| `--text-xs` | 10px | Badges, labels |
| `--text-sm` | 12px | Secondary text, captions |
| `--text-base` | 14px | Body text, buttons, inputs |
| `--text-md` | 16px | Default body |
| `--text-lg` | 18px | Subheadings |
| `--text-xl` | 24px | Page titles (H1) |
| `--text-2xl` | 30px | Large metrics, KPI values |

### Font Weights

| Token | Weight | Usage |
|-------|--------|-------|
| `--font-normal` | 400 | Body text |
| `--font-medium` | 500 | Buttons, tabs, labels |
| `--font-semibold` | 600 | Headings, emphasis |
| `--font-bold` | 700 | Strong emphasis, metrics |

### Heading Styles

```css
/* H1 - Page Title */
.h1 {
  font-size: 24px;
  font-weight: 600;
  color: var(--text-primary);
}

/* H2 - Section Title */
.h2 {
  font-size: 18px;
  font-weight: 600;
  color: var(--text-primary);
}

/* H3 - Subsection */
.h3 {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
```

---

## Colors

### Primary Palette

| Token | Value | RGB | Usage |
|-------|-------|-----|-------|
| `--primary` | `#0073D1` | rgb(0, 115, 209) | Primary actions, links, active states |
| `--primary-light` | `rgba(0, 115, 209, 0.1)` | - | Primary hover backgrounds |

### Neutral Palette

| Token | Value | RGB | Usage |
|-------|-------|-----|-------|
| `--text-primary` | `#1A253C` | rgb(26, 37, 60) | Headings, primary text |
| `--text-secondary` | `#6B7280` | rgb(107, 114, 128) | Secondary text, placeholders |
| `--text-muted` | `rgba(107, 114, 128, 0.5)` | - | Disabled text |
| `--background` | `#F5F7FA` | rgb(245, 247, 250) | Page background |
| `--surface` | `#FFFFFF` | rgb(255, 255, 255) | Cards, modals |
| `--border` | `#E5E7EB` | rgb(229, 231, 235) | Borders, dividers |
| `--border-light` | `#F3F4F6` | rgb(243, 244, 246) | Subtle borders |

### Semantic Colors

| Token | Value | RGB | Usage |
|-------|-------|-----|-------|
| `--success` | `#10B77F` | rgb(16, 183, 127) | Positive values, growth |
| `--warning` | `#F59F0A` | rgb(245, 159, 10) | Warnings, attention |
| `--error` | `#EA580C` | rgb(234, 88, 12) | Errors, negative values |

### Color Usage Examples

```css
/* Positive trend */
.trend-positive {
  color: var(--success); /* #10B77F */
}

/* Negative trend */
.trend-negative {
  color: var(--error); /* #EA580C */
}

/* Warning alert */
.alert-warning {
  background: rgba(245, 159, 10, 0.1);
  border-left: 3px solid var(--warning);
}
```

---

## Spacing

### Base Scale

| Token | Value | Usage |
|-------|-------|-------|
| `--space-1` | 4px | Tight spacing |
| `--space-2` | 8px | Default gap |
| `--space-3` | 12px | Component padding |
| `--space-4` | 16px | Section spacing |
| `--space-5` | 20px | Card padding |
| `--space-6` | 24px | Large spacing |
| `--space-8` | 32px | Section gaps |

### Common Patterns

```css
/* Button padding */
.btn { padding: 0 12px; } /* 0px 12px */

/* Card padding */
.card { padding: 16px; }

/* Input padding */
.input { padding: 8px 16px; }

/* Sidebar width */
.sidebar { width: 224px; }
```

---

## Border Radius

| Token | Value | Usage |
|-------|-------|-------|
| `--radius-sm` | 4px | Small buttons, badges |
| `--radius-md` | 6px | Buttons, inputs, cards |
| `--radius-lg` | 8px | Large cards, modals |
| `--radius-xl` | 12px | Extra large containers |
| `--radius-full` | 9999px | Pills, avatars, circular |

---

## Shadows

```css
/* Subtle shadow - small elements */
--shadow-sm: rgba(0, 0, 0, 0.05) 0px 1px 2px 0px;

/* Medium shadow - dropdowns, popovers */
--shadow-md: rgba(0, 0, 0, 0.1) 0px 1px 3px 0px,
             rgba(0, 0, 0, 0.1) 0px 1px 2px -1px;

/* Card shadow - softer, elevated cards (from login page) */
--shadow-card: rgba(0, 0, 0, 0.08) 0px 4px 24px 0px,
               rgba(0, 0, 0, 0.04) 0px 1px 2px 0px;

/* Focus ring */
--shadow-focus: 0 0 0 2px var(--primary-light);
```

---

## Components

### Buttons

#### Primary Button

```css
.btn-primary {
  font-size: 14px;
  font-weight: 500;
  color: #FFFFFF;
  background-color: #0073D1;
  border: none;
  border-radius: 6px;
  padding: 0 12px;
  height: 36px;
}

.btn-primary:hover {
  background-color: #005BA3; /* darker */
}
```

#### Secondary Button

```css
.btn-secondary {
  font-size: 14px;
  font-weight: 500;
  color: #1A253C;
  background-color: #F5F7FA;
  border: 1px solid #E5E7EB;
  border-radius: 6px;
  padding: 0 12px;
  height: 36px;
}

.btn-secondary:hover {
  background-color: #E5E7EB;
}
```

#### Ghost Button

```css
.btn-ghost {
  font-size: 14px;
  font-weight: 500;
  color: #6B7280;
  background-color: transparent;
  border: none;
  border-radius: 8px;
  padding: 10px 12px;
}

.btn-ghost:hover {
  background-color: rgba(243, 244, 246, 0.5);
}
```

### Tabs

```css
.tab {
  font-size: 14px;
  font-weight: 500;
  color: #6B7280;
  background: transparent;
  padding: 8px 16px;
  border: none;
  border-bottom: 2px solid transparent;
}

.tab:hover {
  color: #1A253C;
}

.tab.active {
  color: #1A253C;
  background: #F5F7FA;
  border-bottom-color: #0073D1;
}
```

### Cards

```css
.card {
  background: #FFFFFF;
  border: 1px solid #E5E7EB;
  border-radius: 8px;
  box-shadow: var(--shadow-sm);
}

/* KPI Card */
.kpi-card {
  padding: 16px;
  min-width: 180px;
}

.kpi-card .label {
  font-size: 12px;
  font-weight: 500;
  color: #6B7280;
  margin-bottom: 4px;
}

.kpi-card .value {
  font-size: 30px;
  font-weight: 600;
  color: #1A253C;
}

.kpi-card .trend {
  font-size: 12px;
  font-weight: 500;
  margin-top: 8px;
}
```

### Form Inputs

```css
.input {
  font-size: 14px;
  color: #1A253C;
  background: #FFFFFF;
  border: 1px solid #E5E7EB;
  border-radius: 6px;
  padding: 8px 12px;
  height: 36px;
}

.input:focus {
  border-color: #0073D1;
  outline: none;
  box-shadow: 0 0 0 2px rgba(0, 115, 209, 0.1);
}

.input::placeholder {
  color: #6B7280;
}
```

### Select / Dropdown

```css
.select {
  font-size: 14px;
  font-weight: 500;
  color: #1A253C;
  background: rgba(243, 244, 246, 0.5);
  border: 1px solid #E5E7EB;
  border-radius: 6px;
  padding: 8px 12px;
  height: 36px;
}
```

### Tables

```css
.table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}

.table th {
  font-weight: 500;
  color: #6B7280;
  text-align: left;
  padding: 12px 16px;
  border-bottom: 1px solid #E5E7EB;
  background: #F5F7FA;
}

.table td {
  color: #1A253C;
  padding: 12px 16px;
  border-bottom: 1px solid #F3F4F6;
}

.table tr:hover {
  background: rgba(243, 244, 246, 0.5);
}

/* Numeric cells */
.table td.numeric {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

/* Positive value */
.table .positive {
  color: #10B77F;
}

/* Negative value */
.table .negative {
  color: #EA580C;
}
```

### Sidebar Navigation

```css
.sidebar {
  width: 224px;
  background: #FFFFFF;
  border-right: 1px solid #E5E7EB;
  height: 100vh;
  display: flex;
  flex-direction: column;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 400;
  color: #6B7280;
  text-decoration: none;
}

.nav-item:hover {
  background: rgba(243, 244, 246, 0.5);
}

.nav-item.active {
  background: rgba(0, 115, 209, 0.1);
  color: #0073D1;
  font-weight: 500;
}
```

### Alerts / Banners

```css
.alert {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  border-radius: 8px;
}

.alert-warning {
  background: rgba(245, 159, 10, 0.1);
  border-left: 3px solid #F59F0A;
}

.alert-error {
  background: rgba(249, 115, 22, 0.1);
  border-left: 3px solid #EA580C;
}

.alert-title {
  font-size: 14px;
  font-weight: 600;
  color: #1A253C;
}

.alert-description {
  font-size: 12px;
  color: #6B7280;
}
```

### Badges / Pills

```css
.badge {
  font-size: 12px;
  font-weight: 500;
  padding: 2px 8px;
  border-radius: 9999px;
}

.badge-warning {
  background: rgba(245, 159, 10, 0.1);
  color: #F59F0A;
}

.badge-success {
  background: rgba(16, 183, 127, 0.1);
  color: #10B77F;
}

.badge-error {
  background: rgba(234, 88, 12, 0.1);
  color: #EA580C;
}
```

### Data Quality Score

```css
.dq-score {
  display: flex;
  align-items: center;
  gap: 8px;
}

.dq-score .value {
  font-size: 14px;
  font-weight: 600;
  color: #1A253C;
}

.dq-score .bar {
  width: 60px;
  height: 6px;
  background: #E5E7EB;
  border-radius: 9999px;
  overflow: hidden;
}

.dq-score .bar-fill {
  height: 100%;
  background: linear-gradient(to right, #EA580C, #F59F0A, #10B77F);
  border-radius: 9999px;
}
```

### Login Page Components

From the login page - split-screen layout with feature showcase.

#### Login Card (elevated)

```css
.login-card {
  background: #FFFFFF;
  border: 1px solid #E5E7EB;
  border-radius: 12px; /* --radius-xl */
  box-shadow: rgba(0, 0, 0, 0.08) 0px 4px 24px 0px,
              rgba(0, 0, 0, 0.04) 0px 1px 2px 0px;
  padding: 32px;
}
```

#### Feature Panel (glass effect on primary)

```css
/* Left panel with primary background */
.login-features {
  background: #0073D1; /* --primary */
  color: #FFFFFF;
  padding: 48px;
}

/* Glass-effect feature cards */
.feature-card {
  background: rgba(255, 255, 255, 0.1);
  border: 1px solid rgba(255, 255, 255, 0.15);
  border-radius: 8px;
  padding: 16px;
}

/* Feature icons */
.feature-icon {
  width: 40px;
  height: 40px;
  background: rgba(255, 255, 255, 0.15);
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.feature-card h3 {
  font-size: 14px;
  font-weight: 600;
  color: #FFFFFF;
}

.feature-card p {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.8);
}
```

---

## Icons

The application uses a Lucide-style icon set. Common icons:

| Icon | Usage |
|------|-------|
| `dashboard` | Dashboard nav |
| `file-text` | Reports nav |
| `calculator` | Plan & Budget nav |
| `list` | Chart of Accounts nav |
| `database` | Data Catalog nav |
| `book-open` | Glossary |
| `shield-check` | Data Quality |
| `sparkles` | AI Assistant |
| `search` | Search |
| `chevron-down` | Dropdown indicators |
| `trend-up` | Positive trends |
| `trend-down` | Negative trends |
| `building` | Business entity |
| `calendar` | Date picker |
| `download` | Export |
| `upload` | Import |
| `printer` | Print |
| `message-square` | Comments |
| `settings` | Settings |
| `user` | User profile |

---

## Layout

### Page Structure

```
┌─────────────────────────────────────────────────────────────────┐
│ Sidebar (224px)  │  Main Content                                │
│                  │  ┌─────────────────────────────────────────┐ │
│ [Logo]           │  │ Header: Title + Actions (right aligned) │ │
│ [Search]         │  └─────────────────────────────────────────┘ │
│                  │  ┌─────────────────────────────────────────┐ │
│ [Nav Items]      │  │ Filters Bar (entity, date, view toggle) │ │
│ - Dashboard      │  └─────────────────────────────────────────┘ │
│ - Reports        │  ┌─────────────────────────────────────────┐ │
│ - Plan & Budget  │  │ Content Area                            │ │
│ - COA            │  │                                         │ │
│ - Data Catalog   │  │ (Cards, Tables, Charts)                 │ │
│   - Glossary     │  │                                         │ │
│   - Data Quality │  │                                         │ │
│                  │  │                                         │ │
│ [Tour]           │  │                                         │ │
│ [AI Assistant]   │  │                                         │ │
│ [User Profile]   │  └─────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

### Responsive Breakpoints

```css
/* Desktop (default) */
@media (min-width: 1280px) {
  .sidebar { width: 224px; }
  .main { margin-left: 224px; }
}

/* Tablet */
@media (max-width: 1279px) {
  .sidebar { width: 64px; } /* Icons only */
  .main { margin-left: 64px; }
}

/* Mobile */
@media (max-width: 768px) {
  .sidebar { display: none; } /* Hamburger menu */
  .main { margin-left: 0; }
}
```

---

## CSS Custom Properties Summary

```css
:root {
  /* Colors */
  --primary: #0073D1;
  --primary-light: rgba(0, 115, 209, 0.1);
  --text-primary: #1A253C;
  --text-secondary: #6B7280;
  --background: #F5F7FA;
  --surface: #FFFFFF;
  --border: #E5E7EB;
  --success: #10B77F;
  --warning: #F59F0A;
  --error: #EA580C;

  /* Typography */
  --font-primary: 'Inter', system-ui, sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;

  /* Font sizes */
  --text-xs: 10px;
  --text-sm: 12px;
  --text-base: 14px;
  --text-md: 16px;
  --text-lg: 18px;
  --text-xl: 24px;
  --text-2xl: 30px;

  /* Font weights */
  --font-normal: 400;
  --font-medium: 500;
  --font-semibold: 600;
  --font-bold: 700;

  /* Spacing */
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
  --space-8: 32px;

  /* Border radius */
  --radius-sm: 4px;
  --radius-md: 6px;
  --radius-lg: 8px;
  --radius-xl: 12px;
  --radius-full: 9999px;

  /* Shadows */
  --shadow-sm: rgba(0, 0, 0, 0.05) 0px 1px 2px 0px;
  --shadow-md: rgba(0, 0, 0, 0.1) 0px 1px 3px 0px, rgba(0, 0, 0, 0.1) 0px 1px 2px -1px;
  --shadow-card: rgba(0, 0, 0, 0.08) 0px 4px 24px 0px, rgba(0, 0, 0, 0.04) 0px 1px 2px 0px;

  /* Layout */
  --sidebar-width: 224px;
  --header-height: 64px;
}
```

---

## Tailwind CSS Mapping

If using Tailwind CSS, here's the equivalent configuration:

```javascript
// tailwind.config.js
module.exports = {
  theme: {
    extend: {
      colors: {
        primary: {
          DEFAULT: '#0073D1',
          light: 'rgba(0, 115, 209, 0.1)',
        },
        text: {
          primary: '#1A253C',
          secondary: '#6B7280',
        },
        background: '#F5F7FA',
        surface: '#FFFFFF',
        border: '#E5E7EB',
        success: '#10B77F',
        warning: '#F59F0A',
        error: '#EA580C',
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Monaco', 'Consolas', 'monospace'],
      },
      fontSize: {
        'xs': '10px',
        'sm': '12px',
        'base': '14px',
        'md': '16px',
        'lg': '18px',
        'xl': '24px',
        '2xl': '30px',
      },
      borderRadius: {
        'sm': '4px',
        'md': '6px',
        'lg': '8px',
        'xl': '12px',
      },
      spacing: {
        'sidebar': '224px',
      },
    },
  },
}
```
