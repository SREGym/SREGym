import csv
import json
import subprocess
import webbrowser
from datetime import datetime
from pathlib import Path

import click
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, Prompt
from rich.syntax import Syntax
from rich.table import Table

console = Console()

DATABASE = "/Users/lilygniedz/Documents/SREArena/SREArena/python-database"
QUERIES_DIR = Path(__file__).parent / "queries"
OUTPUT_DIR = Path("./results")

FIX_SUGGESTIONS = {
    "NO self.app defined": {
        "description": "Missing self.app assignment in Problem subclass",
        "fix_template": "self.app = {app_class}()",
        "explanation": "Problem subclasses must initialize self.app with an Application instance",
        "example": "self.app = AstronomyShop()",
    },
    "NO self.namespace defined": {
        "description": "Missing self.namespace assignment",
        "fix_template": "self.namespace = self.app.namespace",
        "explanation": "Set namespace from the application instance",
        "example": "self.namespace = self.app.namespace",
    },
    "NO self.faulty_service defined": {
        "description": "Missing self.faulty_service assignment",
        "fix_template": 'self.faulty_service = "{service_name}"',
        "explanation": "Specify which service has the fault",
        "example": 'self.faulty_service = "payment-service"',
    },
    "NO self.diagnosis_oracle defined": {
        "description": "Missing self.diagnosis_oracle assignment",
        "fix_template": "self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)",
        "explanation": "Attach a diagnosis oracle for evaluation",
        "example": "self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)",
    },
    "NO self.mitigation_oracle defined": {
        "description": "Missing self.mitigation_oracle assignment",
        "fix_template": "self.mitigation_oracle = MitigationOracle(problem=self)",
        "explanation": "Attach a mitigation oracle for evaluation",
        "example": "self.mitigation_oracle = MitigationOracle(problem=self)",
    },
    "missing @mark_fault_injected decorator": {
        "description": "Fault method missing required decorator",
        "fix_template": "@mark_fault_injected",
        "explanation": "Add @mark_fault_injected decorator above the method",
        "example": "@mark_fault_injected\ndef inject_fault(self):\n    ...",
    },
    "has inject_fault() but missing recover_fault()": {
        "description": "Missing recovery method",
        "fix_template": "@mark_fault_injected\ndef recover_fault(self):\n    self.injector.recover_fault()",
        "explanation": "Every inject_fault() needs a corresponding recover_fault()",
        "example": '@mark_fault_injected\ndef recover_fault(self):\n    self.injector.recover_fault("faultName")',
    },
    "has deploy() but missing cleanup()": {
        "description": "Missing cleanup method",
        "fix_template": "def cleanup(self):\n    self.kubectl.delete_namespace(self.namespace)",
        "explanation": "Every deploy() needs a corresponding cleanup() for resource management",
        "example": "def cleanup(self):\n    if self.trace_api:\n        self.trace_api.stop_port_forward()\n    self.kubectl.delete_namespace(self.namespace)",
    },
}


@click.group()
def cli():
    """SRE CodeQL Check Runner"""
    pass


@cli.command()
@click.option("--query", "-q", help="Run specific query file")
@click.option("--format", "-f", default="csv", type=click.Choice(["csv", "sarif", "json"]))
@click.option("--output", "-o", help="Output file path")
def run(query, format, output):
    """Run CodeQL checks"""
    OUTPUT_DIR.mkdir(exist_ok=True)

    if query:
        query_path = QUERIES_DIR / query
        if not query_path.exists():
            console.print(f"[red]❌ Query not found: {query}[/red]")
            return
    else:
        query_path = QUERIES_DIR

    output_file = output or OUTPUT_DIR / f"results.{format}"

    config_table = Table(show_header=False, box=box.SIMPLE)
    config_table.add_column("Setting", style="cyan")
    config_table.add_column("Value", style="yellow")
    config_table.add_row("Database", str(DATABASE))
    config_table.add_row("Queries", str(query_path))
    config_table.add_row("Output", str(output_file))
    config_table.add_row("Format", format)

    console.print(Panel(config_table, title="[bold blue]🔍 CodeQL Configuration[/bold blue]", border_style="blue"))

    cmd = [
        "codeql",
        "database",
        "analyze",
        DATABASE,
        str(query_path),
        f"--format={format}",
        f"--output={output_file}",
        "--rerun",
    ]

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
        task = progress.add_task("[cyan]Running analysis...", total=None)
        result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        console.print("\n[bold green]✅ Analysis complete![/bold green]\n")

        if format == "csv" and Path(output_file).exists():
            with open(output_file, "r", newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                rows = list(reader)
                issue_count = len(rows)

                if issue_count > 0:
                    console.print(f"[bold red]⚠️  Found {issue_count} issues[/bold red]\n")

                    table = Table(title="Top Issues", box=box.ROUNDED)
                    table.add_column("Name", style="cyan", no_wrap=False, max_width=30)
                    table.add_column("Message", style="yellow", no_wrap=False, max_width=40)
                    table.add_column("Path", style="white", no_wrap=False, max_width=35)
                    table.add_column("Line", style="green", justify="right", width=6)

                    for row in rows[:10]:
                        if len(row) >= 6:
                            name = row[0]
                            message = row[3]
                            path = row[4]
                            line = row[5]
                            table.add_row(name, message, path, line)

                    console.print(table)

                    if issue_count > 10:
                        console.print(f"\n[dim]... and {issue_count - 10} more issues[/dim]")
                else:
                    console.print(
                        Panel("[bold green]✨ No issues found! Your code is clean.[/bold green]", border_style="green")
                    )

        console.print(f"\n[dim]Full results saved to: {output_file}[/dim]")
    else:
        console.print(
            Panel(f"[red]{result.stderr}[/red]", title="[bold red]❌ Analysis Failed[/bold red]", border_style="red")
        )


@cli.command()
def interactive():
    """Interactive query selector"""

    console.print(
        Panel(
            "[bold]Select queries by number (comma-separated, e.g., 1,3,5 or 'all')[/bold]",
            title="[bold blue]🎮 Interactive Mode[/bold blue]",
            border_style="blue",
        )
    )

    queries = []
    table = Table(box=box.ROUNDED)
    table.add_column("#", style="cyan", justify="right", width=4)
    table.add_column("Query File", style="yellow", width=50)
    table.add_column("Description", style="white")

    for i, query_file in enumerate(sorted(QUERIES_DIR.rglob("*.ql")), 1):
        description = ""

        with open(query_file, "r") as f:
            for line in f:
                if "@name" in line:
                    description = line.split("@name")[1].strip()
                    break

        relative_path = str(query_file.relative_to(QUERIES_DIR))
        queries.append(relative_path)
        table.add_row(str(i), relative_path, description)

    if not queries:
        console.print("[yellow]No queries found in queries directory[/yellow]")
        return

    console.print(table)
    console.print()

    selection = Prompt.ask("[cyan]Enter query numbers to run (or 'all')[/cyan]", default="all")

    if selection.lower() == "all":
        selected_indices = list(range(len(queries)))
    else:
        try:
            selected_indices = [int(x.strip()) - 1 for x in selection.split(",")]
            selected_indices = [i for i in selected_indices if 0 <= i < len(queries)]
        except ValueError:
            console.print("[red]Invalid selection. Please enter numbers separated by commas.[/red]")
            return

    if not selected_indices:
        console.print("[yellow]No valid queries selected[/yellow]")
        return

    output_format = Prompt.ask("[cyan]Select output format[/cyan]", choices=["csv", "sarif", "json"], default="csv")

    selected_queries = [queries[i] for i in selected_indices]
    console.print(f"\n[cyan]Running {len(selected_queries)} selected queries...[/cyan]\n")

    for i, query in enumerate(selected_queries, 1):
        console.print(f"[bold cyan]━━━ ({i}/{len(selected_queries)}) {query} ━━━[/bold cyan]")
        ctx = click.Context(run)
        ctx.invoke(run, query=query, format=output_format, output=None)
        console.print()


@cli.command()
@click.option("--format", "-f", type=click.Choice(["markdown", "json", "slack", "github", "html"]), default="markdown")
@click.option("--output", "-o", help="Output file path")
def export(format, output):
    """Export results in different formats"""

    OUTPUT_DIR.mkdir(exist_ok=True)
    csv_files = list(OUTPUT_DIR.glob("*.csv"))

    if not csv_files:
        console.print("[yellow]No results found. Run checks first with: ./cli.py run[/yellow]")
        return

    # CodeQL CSV format (NO HEADERS): Name, Description, Severity, Message, Path, StartLine, StartColumn, EndLine, EndColumn
    all_issues = []
    for csv_file in csv_files:
        with open(csv_file, "r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) >= 5:
                    issue = {
                        "Name": row[0],
                        "Description": row[1],
                        "Severity": row[2],
                        "Message": row[3],
                        "Path": row[4],
                        "Line": row[5] if len(row) > 5 else "",
                        "source_file": csv_file.name,
                    }
                    all_issues.append(issue)

    if format == "markdown":
        output_file = output or OUTPUT_DIR / "report.md"
        _export_markdown(all_issues, output_file)

    elif format == "json":
        output_file = output or OUTPUT_DIR / "report.json"
        _export_json(all_issues, output_file)

    elif format == "slack":
        output_file = output or OUTPUT_DIR / "slack.json"
        _export_slack(all_issues, output_file)

    elif format == "github":
        output_file = output or OUTPUT_DIR / "github-issues.md"
        _export_github(all_issues, output_file)

    elif format == "html":
        output_file = output or OUTPUT_DIR / "report.html"
        _export_html(all_issues, output_file)

    console.print(f"\n[green]✅ Exported to {output_file}[/green]")


def _export_markdown(issues, output_file):
    """Export to Markdown format"""
    md = f"# 🔍 CodeQL Analysis Report\n\n"
    md += f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    md += f"**Total Issues:** {len(issues)}\n\n"

    md += "## Summary\n\n"

    from collections import Counter

    if issues:
        messages = [i.get("Message", "Unknown") for i in issues]
        common = Counter(messages).most_common(5)

        md += "### Most Common Issues\n\n"
        for msg, count in common:
            md += f"- **{msg}**: {count} occurrences\n"

        md += "\n## Detailed Issues\n\n"
        for i, issue in enumerate(issues, 1):
            md += f"### Issue #{i}\n\n"
            for key, value in issue.items():
                md += f"- **{key}**: {value}\n"
            md += "\n"

    with open(output_file, "w") as f:
        f.write(md)


def _export_json(issues, output_file):
    """Export to JSON format"""
    report = {"generated": datetime.now().isoformat(), "total_issues": len(issues), "issues": issues}

    with open(output_file, "w") as f:
        json.dump(report, f, indent=2)


def _export_slack(issues, output_file):
    """Export to Slack message format"""

    severity_emoji = {"error": "🔴", "warning": "⚠️", "note": "ℹ️"}

    message = {
        "text": f"🔍 CodeQL Analysis Results",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "🔍 CodeQL Analysis Results"}},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Total Issues:* {len(issues)}\n*Generated:* {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                },
            },
            {"type": "divider"},
        ],
    }

    for issue in issues[:10]:
        message["blocks"].append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{issue.get('Message', 'Unknown')}*\n`{issue.get('Name', 'Unknown location')}`",
                },
            }
        )

    if len(issues) > 10:
        message["blocks"].append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"_... and {len(issues) - 10} more issues_"}}
        )

    with open(output_file, "w") as f:
        json.dump(message, f, indent=2)

    console.print("\n[cyan]Slack message JSON created. Send to Slack with:[/cyan]")
    console.print(
        f"[dim]curl -X POST -H 'Content-type: application/json' --data @{output_file} YOUR_SLACK_WEBHOOK_URL[/dim]"
    )


def _export_github(issues, output_file):
    """Export to GitHub issues format"""
    md = "# CodeQL Issues\n\n"
    md += f"Found {len(issues)} issues that need attention.\n\n"

    for i, issue in enumerate(issues, 1):
        md += f"## Issue #{i}: {issue.get('Message', 'Unknown')}\n\n"
        md += f"**Location:** `{issue.get('Name', 'Unknown')}`\n\n"

        if "Description" in issue and issue["Description"]:
            md += f"**Description:** {issue['Description']}\n\n"

        md += "**Labels:** `codeql`, `bug`, `automated`\n\n"
        md += "---\n\n"

    with open(output_file, "w") as f:
        f.write(md)

    console.print("\n[cyan]GitHub issues template created. Create issues with:[/cyan]")
    console.print('[dim]gh issue create --title "CodeQL Issue" --body-file ' + str(output_file) + "[/dim]")


def _export_html(issues, output_file):
    """Export to HTML format"""

    html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>CodeQL Analysis Report</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 20px;
            min-height: 100vh;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px;
            text-align: center;
        }}
        .header h1 {{
            font-size: 2.5em;
            margin-bottom: 10px;
        }}
        .stats {{
            display: flex;
            justify-content: space-around;
            padding: 30px;
            background: #f8f9fa;
            border-bottom: 2px solid #e9ecef;
        }}
        .stat {{
            text-align: center;
        }}
        .stat-number {{
            font-size: 3em;
            font-weight: bold;
            color: #667eea;
        }}
        .stat-label {{
            color: #6c757d;
            font-size: 0.9em;
            text-transform: uppercase;
            letter-spacing: 1px;
        }}
        .issues {{
            padding: 40px;
        }}
        .issue {{
            background: #fff;
            border: 2px solid #e9ecef;
            border-radius: 12px;
            padding: 25px;
            margin-bottom: 20px;
            transition: all 0.3s ease;
        }}
        .issue:hover {{
            transform: translateY(-5px);
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
            border-color: #667eea;
        }}
        .issue-header {{
            display: flex;
            align-items: center;
            margin-bottom: 15px;
        }}
        .issue-number {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            width: 40px;
            height: 40px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
            margin-right: 15px;
        }}
        .issue-title {{
            font-size: 1.2em;
            font-weight: 600;
            color: #2d3748;
            flex: 1;
        }}
        .issue-severity {{
            padding: 5px 15px;
            border-radius: 20px;
            font-size: 0.85em;
            font-weight: 600;
            text-transform: uppercase;
        }}
        .severity-error {{
            background: #fee;
            color: #c53030;
        }}
        .severity-warning {{
            background: #fef3cd;
            color: #d97706;
        }}
        .issue-location {{
            color: #667eea;
            margin-top: 10px;
            font-size: 0.9em;
            word-wrap: break-word;
        }}
        .issue-file {{
            color: #6c757d;
            font-family: 'Monaco', monospace;
            font-size: 0.85em;
            margin-top: 5px;
        }}
        .issue-description {{
            color: #718096;
            font-size: 0.9em;
            margin-top: 10px;
            font-style: italic;
        }}
        .footer {{
            text-align: center;
            padding: 30px;
            background: #f8f9fa;
            color: #6c757d;
            font-size: 0.9em;
        }}
        .no-issues {{
            text-align: center;
            padding: 60px;
            color: #6c757d;
        }}
        .no-issues-icon {{
            font-size: 4em;
            margin-bottom: 20px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🔍 CodeQL Analysis Report</h1>
            <p>Generated on {datetime.now().strftime('%B %d, %Y at %H:%M:%S')}</p>
        </div>
        
        <div class="stats">
            <div class="stat">
                <div class="stat-number">{len(issues)}</div>
                <div class="stat-label">Total Issues</div>
            </div>
            <div class="stat">
                <div class="stat-number">{len([i for i in issues if i.get('Severity', '').lower() == 'error'])}</div>
                <div class="stat-label">Errors</div>
            </div>
            <div class="stat">
                <div class="stat-number">{len([i for i in issues if i.get('Severity', '').lower() == 'warning'])}</div>
                <div class="stat-label">Warnings</div>
            </div>
        </div>
        
        <div class="issues">
"""

    if not issues:
        html += """
            <div class="no-issues">
                <div class="no-issues-icon">✨</div>
                <h2>No Issues Found!</h2>
                <p>Your code is clean and follows all CodeQL rules.</p>
            </div>
"""
    else:
        for i, issue in enumerate(issues, 1):
            name = issue.get("Name", "Unknown")
            description = issue.get("Description", "")
            message = issue.get("Message", "No message")
            path = issue.get("Path", "Unknown location")
            line = issue.get("Line", "")
            severity = issue.get("Severity", "warning")

            severity_class = "error" if severity.lower() == "error" else "warning"

            html += f"""
            <div class="issue">
                <div class="issue-header">
                    <div class="issue-number">{i}</div>
                    <div class="issue-title">{message}</div>
                    <span class="issue-severity severity-{severity_class}">{severity.upper()}</span>
                </div>
                <div class="issue-description">{description}</div>
                <div class="issue-location">📋 {name}</div>
                <div class="issue-file">📍 {path}{':' + line if line else ''}</div>
            </div>
"""

    html += f"""
        </div>
        
        <div class="footer">
            <p>Generated by SRE CodeQL Check Runner</p>
            <p>For more information, run: ./cli.py --help</p>
        </div>
    </div>
</body>
</html>
"""

    with open(output_file, "w") as f:
        f.write(html)


@cli.command()
@click.option("--auto-open/--no-auto-open", default=True, help="Automatically open report in browser")
def report(auto_open):
    """📊 Generate HTML report"""

    OUTPUT_DIR.mkdir(exist_ok=True)
    csv_files = list(OUTPUT_DIR.glob("*.csv"))

    if not csv_files:
        console.print("[yellow]No results found. Run checks first with: ./cli.py run[/yellow]")
        return

    all_issues = []
    for csv_file in csv_files:
        with open(csv_file, "r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) >= 5:
                    issue = {
                        "Name": row[0],
                        "Description": row[1],
                        "Severity": row[2],
                        "Message": row[3],
                        "Path": row[4],
                        "Line": row[5] if len(row) > 5 else "",
                        "source_file": csv_file.name,
                    }
                    all_issues.append(issue)

    report_file = OUTPUT_DIR / "report.html"
    _export_html(all_issues, report_file)

    console.print(f"\n[green]✅ Report generated: {report_file}[/green]")

    if auto_open:
        console.print(f"[cyan]Opening report in browser...[/cyan]")
        webbrowser.open(f"file://{report_file.absolute()}")


@cli.command()
def fixes():
    """Show available fix suggestions"""

    OUTPUT_DIR.mkdir(exist_ok=True)

    csv_files = list(OUTPUT_DIR.glob("*.csv"))

    if not csv_files:
        console.print("[yellow]No results found. Run checks first with: ./cli.py run[/yellow]")
        return

    console.print(
        Panel(
            "[bold]Available fixes for common issues[/bold]",
            title="[bold blue]🔧 Fix Suggestions[/bold blue]",
            border_style="blue",
        )
    )

    issues_with_fixes = []

    for csv_file in csv_files:
        with open(csv_file, "r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) >= 5:
                    issue = {
                        "Name": row[0],
                        "Description": row[1],
                        "Severity": row[2],
                        "Message": row[3],
                        "Path": row[4],
                        "Line": row[5] if len(row) > 5 else "",
                    }

                    message = issue.get("Message", "")
                    for pattern, fix_info in FIX_SUGGESTIONS.items():
                        if pattern.lower() in message.lower():
                            issues_with_fixes.append({"issue": issue, "fix": fix_info})
                            break

    if not issues_with_fixes:
        console.print("\n[yellow]No fixable issues found or no fix suggestions available.[/yellow]")
        return

    console.print(f"\n[green]Found {len(issues_with_fixes)} issues with fix suggestions![/green]\n")

    for i, item in enumerate(issues_with_fixes, 1):
        issue = item["issue"]
        fix = item["fix"]

        console.print(f"[bold cyan]Issue #{i}:[/bold cyan] {issue.get('Message', 'Unknown')}")
        console.print(f"[dim]Location: {issue.get('Name', 'Unknown')}[/dim]\n")

        console.print(
            Panel(
                f"[yellow]{fix['explanation']}[/yellow]\n\n"
                f"[bold]Suggested fix:[/bold]\n"
                f"[green]{fix['fix_template']}[/green]\n\n"
                f"[bold]Example:[/bold]\n"
                f"[cyan]{fix['example']}[/cyan]",
                title=f"[bold]🔧 Fix #{i}[/bold]",
                border_style="green",
            )
        )
        console.print()


@cli.command()
@click.argument("category", type=click.Choice(["problem", "application", "lifecycle", "fault"]))
def check(category):
    """Run checks by category"""
    query_map = {
        "problem": [
            "null.ql",
            "namespace-null.ql",
            "faulty-service-null.ql",
            "diagnosis-oracle-null.ql",
            "mitigation-oracle-null.ql",
        ],
        "application": ["application-required-attrs.ql"],
        "lifecycle": ["lifecycle-methods.ql"],
        "fault": ["fault-injection-recovery.ql"],
    }

    queries = query_map.get(category, [])

    console.print(
        Panel(
            f"[bold]Running {len(queries)} {category} checks[/bold]",
            title=f"[bold blue]🔍 {category.capitalize()} Analysis[/bold blue]",
            border_style="blue",
        )
    )

    all_issues = []

    for i, query in enumerate(queries, 1):
        console.print(f"\n[cyan]({i}/{len(queries)}) Running {query}...[/cyan]")

        query_path = QUERIES_DIR / query
        if not query_path.exists():
            console.print(f"[yellow]  ⚠️  Query not found, skipping[/yellow]")
            continue

        output_file = OUTPUT_DIR / f"{category}-{query}.csv"

        cmd = [
            "codeql",
            "database",
            "analyze",
            DATABASE,
            str(query_path),
            "--format=csv",
            f"--output={output_file}",
            "--rerun",
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0 and output_file.exists():
            with open(output_file, "r", newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                rows = list(reader)
                issue_count = len(rows)

                if issue_count > 0:
                    console.print(f"  [red]❌ {issue_count} issues found[/red]")
                    all_issues.extend(rows)
                else:
                    console.print(f"  [green]✅ No issues[/green]")

    console.print("\n" + "=" * 60 + "\n")
    if all_issues:
        console.print(Panel(f"[bold red]Total Issues: {len(all_issues)}[/bold red]", border_style="red"))
    else:
        console.print(Panel("[bold green]✨ All checks passed![/bold green]", border_style="green"))


@cli.command()
def summary():
    """Show summary of all results"""
    csv_files = list(OUTPUT_DIR.glob("*.csv"))

    if not csv_files:
        console.print(
            Panel(
                "[yellow]No results found. Run checks first with:[/yellow]\n\n" "[cyan]./cli.py run[/cyan]",
                title="[bold]📊 Results Summary[/bold]",
                border_style="yellow",
            )
        )
        return

    console.print("\n[bold blue]📊 Results Summary[/bold blue]\n")

    table = Table(box=box.ROUNDED)
    table.add_column("Result File", style="cyan")
    table.add_column("Issues", justify="right", style="yellow")
    table.add_column("Status", justify="center")

    total_issues = 0

    for csv_file in sorted(csv_files):
        with open(csv_file, "r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = list(reader)
            issue_count = len(rows)
            total_issues += issue_count

            status = "✅" if issue_count == 0 else "❌"
            style = "green" if issue_count == 0 else "red"

            table.add_row(csv_file.name, f"[{style}]{issue_count}[/{style}]", status)

    console.print(table)
    console.print()

    if total_issues == 0:
        console.print(Panel("[bold green]✨ Perfect! No issues found.[/bold green]", border_style="green"))
    else:
        console.print(Panel(f"[bold red]Total Issues: {total_issues}[/bold red]", border_style="red"))


if __name__ == "__main__":
    cli()
