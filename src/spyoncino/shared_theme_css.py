"""
Shared dashboard theme (CSS variables + base components) for web UIs.

Used by the main dashboard and the standalone camera discovery app.
"""

SHARED_DASHBOARD_THEME_CSS = """
        :root {
            --bg-0: #0a0f0c;
            --bg-1: #101813;
            --bg-2: #162119;
            --border: #233226;
            --text-0: #f2f5f3;
            --text-1: #c9d2cc;
            --text-muted: #8b9890;
            --accent-green: #3ddc84;
            --accent-green-strong: #2ecf73;
            --danger: #ff6b6b;
            --warning: #f7c948;
            --radius-sm: 8px;
            --radius-md: 12px;
            --space-1: 4px;
            --space-2: 8px;
            --space-3: 12px;
            --space-4: 16px;
            --space-5: 20px;
            --space-6: 28px;
            --shadow-soft: 0 12px 24px rgba(0, 0, 0, 0.22);
        }
        * { box-sizing: border-box; }
        html, body { margin: 0; padding: 0; }
        body {
            font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif;
            background: radial-gradient(circle at top right, #142218, var(--bg-0) 45%);
            color: var(--text-0);
        }
        .btn {
            border: 1px solid transparent;
            border-radius: var(--radius-sm);
            height: 40px;
            padding: 0 var(--space-4);
            cursor: pointer;
            font-weight: 600;
            transition: background-color 120ms ease, border-color 120ms ease, transform 120ms ease;
        }
        .btn:focus-visible,
        .input:focus-visible,
        textarea.input:focus-visible {
            outline: 2px solid rgba(61, 220, 132, 0.4);
            outline-offset: 2px;
        }
        .btn-primary {
            background: var(--accent-green);
            color: #041308;
        }
        .btn-primary:hover { background: var(--accent-green-strong); }
        .btn-primary:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        .btn-secondary {
            background: transparent;
            color: var(--text-1);
            border-color: var(--border);
        }
        .btn-secondary:hover { border-color: var(--accent-green); color: var(--text-0); }
        .btn-danger {
            background: transparent;
            color: #ff9a9a;
            border-color: rgba(255, 107, 107, 0.55);
        }
        .btn-danger:hover {
            background: rgba(255, 107, 107, 0.12);
            border-color: rgba(255, 107, 107, 0.85);
            color: #ffd4d4;
        }
        .input {
            width: 100%;
            height: 40px;
            border-radius: var(--radius-sm);
            border: 1px solid var(--border);
            background: var(--bg-2);
            color: var(--text-0);
            padding: 0 var(--space-3);
        }
        textarea.input {
            height: auto;
            min-height: 88px;
            padding: var(--space-3);
            resize: vertical;
            font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
            font-size: 0.86rem;
        }
        .card {
            background: linear-gradient(180deg, #111a14 0%, var(--bg-1) 100%);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            box-shadow: var(--shadow-soft);
        }
        .muted { color: var(--text-muted); }
        """
