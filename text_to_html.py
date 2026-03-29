import sys
import html
import re


def colorize_line(line):
    """
    Adds syntax highlighting for common log keywords.
    """
    escaped = html.escape(line)

    patterns = {
        r'\bERROR\b': 'error',
        r'\bFAIL\b': 'error',
        r'\bWARNING\b': 'warning',
        r'\bINFO\b': 'info',
        r'\bSUCCESS\b': 'success',
        r'\bDEBUG\b': 'debug'
    }

    for pattern, css_class in patterns.items():
        escaped = re.sub(
            pattern,
            lambda m: f'<span class="{css_class}">{m.group(0)}</span>',
            escaped,
            flags=re.IGNORECASE
        )

    return escaped


def convert_to_html(input_file, output_file):
    with open(input_file, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    html_lines = []
    for i, line in enumerate(lines, start=1):
        colored = colorize_line(line.rstrip())
        html_lines.append(
            f"""
            <div class="log-line" data-line="{i}">
                <span class="lineno">{i:5}</span>
                <span class="content">{colored}</span>
            </div>
            """
        )

    html_content = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Log Viewer</title>

<style>
body {{
    margin: 0;
    background: #0f172a;
    color: #e2e8f0;
    font-family: Consolas, monospace;
}}

.header {{
    position: sticky;
    top: 0;
    background: #1e293b;
    padding: 15px;
    display: flex;
    gap: 15px;
    align-items: center;
    border-bottom: 1px solid #334155;
    z-index: 1000;
}}

.header input {{
    padding: 6px 10px;
    background: #0f172a;
    border: 1px solid #334155;
    color: white;
    border-radius: 6px;
}}

button {{
    background: #2563eb;
    border: none;
    padding: 6px 10px;
    color: white;
    border-radius: 6px;
    cursor: pointer;
}}

button:hover {{
    background: #1d4ed8;
}}

.log-container {{
    padding: 10px;
    height: calc(100vh - 70px);
    overflow-y: scroll;
}}

.log-line {{
    display: flex;
    padding: 2px 5px;
    border-radius: 4px;
}}

.log-line:hover {{
    background: rgba(255,255,255,0.05);
}}

.lineno {{
    width: 60px;
    color: #64748b;
    user-select: none;
}}

.content {{
    white-space: pre-wrap;
    flex: 1;
}}

.error {{ color: #ef4444; font-weight: bold; }}
.warning {{ color: #f59e0b; }}
.info {{ color: #3b82f6; }}
.success {{ color: #22c55e; font-weight: bold; }}
.debug {{ color: #a78bfa; }}

.highlight {{
    background-color: yellow;
    color: black;
}}

.hidden {{
    display: none;
}}
</style>

<script>
function searchLogs() {{
    let term = document.getElementById("searchBox").value.toLowerCase();
    let lines = document.querySelectorAll(".log-line");

    lines.forEach(line => {{
        let content = line.querySelector(".content");
        let text = content.textContent.toLowerCase();

        content.innerHTML = content.textContent;

        if (term === "") {{
            line.classList.remove("hidden");
        }} else if (text.includes(term)) {{
            line.classList.remove("hidden");

            let regex = new RegExp("(" + term + ")", "gi");
            content.innerHTML = content.innerHTML.replace(regex, "<span class='highlight'>$1</span>");
        }} else {{
            line.classList.add("hidden");
        }}
    }});
}}

function filterLevel(level) {{
    let lines = document.querySelectorAll(".log-line");

    lines.forEach(line => {{
        if (level === "all") {{
            line.classList.remove("hidden");
        }} else {{
            let content = line.innerText.toLowerCase();
            if (content.includes(level)) {{
                line.classList.remove("hidden");
            }} else {{
                line.classList.add("hidden");
            }}
        }}
    }});
}}

function copyLogs() {{
    let text = "";
    document.querySelectorAll(".log-line").forEach(line => {{
        if (!line.classList.contains("hidden")) {{
            text += line.innerText + "\\n";
        }}
    }});

    navigator.clipboard.writeText(text);
    alert("Visible logs copied!");
}}
</script>

</head>

<body>

<div class="header">
    <input type="text" id="searchBox" placeholder="Search logs..." onkeyup="searchLogs()">
    <button onclick="filterLevel('error')">Errors</button>
    <button onclick="filterLevel('warning')">Warnings</button>
    <button onclick="filterLevel('info')">Info</button>
    <button onclick="filterLevel('all')">Show All</button>
    <button onclick="copyLogs()">Copy Visible</button>
</div>

<div class="log-container">
    {"".join(html_lines)}
</div>

</body>
</html>
"""

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"Converted to {output_file}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python text_to_html.py input.txt output.html")
    else:
        convert_to_html(sys.argv[1], sys.argv[2])