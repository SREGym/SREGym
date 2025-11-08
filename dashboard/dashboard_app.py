import atexit
import json
import os
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import dash
import pandas as pd
import plotly.express as px
import plotly.graph_objs as go
from dash import Input, Output, State, callback, dcc, html
from dash.dependencies import ClientsideFunction

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "sregym", "service"))
import threading
from collections import deque

import yaml
from flask import request

from sregym.service.kubectl import KubeCtl

DASHBOARD_URL = "http://127.0.0.1:11451"


class SREGymDashboardServer:
    """SREGym Dashboard Server Class"""

    # CONCISE Toggle - when True, hide pods for all-green deployments
    CONCISE_TOGGLE = True

    def __init__(self, host="127.0.0.1", port=11451, debug=True):
        """
        Initialize the dashboard server

        Args:
            host (str): Server host address
            port (int): Server port
            debug (bool): Enable debug mode
        """
        self.host = host
        self.port = port
        self.debug = debug

        # Initialize Dash application
        self.app = dash.Dash(__name__)

        # Log history storage
        self.log_history = []
        # External log queue for logs received via API (thread-safe with lock)
        self.external_log_queue = deque()
        self._log_lock = threading.Lock()
        self._state_lock = threading.Lock()
        # Structured rows: history of {log, ckpt_state}
        self.history_rows: list[dict] = []
        self.latest_log: dict | None = None
        self.latest_ckpt_state: dict | None = None
        # No extra export data structure; export will render from existing state on exit

        # Setup the application
        self._setup_layout()
        self._setup_callbacks()
        self._setup_api_routes()
        self.namespace = "default"

        self.kubectl = KubeCtl()
        # self.host_info = self.get_host_info()

        # Register graceful shutdown export hooks
        atexit.register(self._export_on_exit)
        # Flag to track if we've already handled a shutdown signal
        self._signal_handled = False
        # Register signal handlers for graceful shutdown
        try:
            signal.signal(signal.SIGINT, self._handle_signal)  # Ctrl-C
            signal.signal(signal.SIGTERM, self._handle_signal)  # terminate() sends SIGTERM
        except Exception:
            pass

    # a bit not elegant here to be honest
    def get_host_info(self):
        # read the script/ansible/inventory.yml, and return the host info
        worker_info = {}
        with open(os.path.join(os.path.dirname(__file__), "..", "scripts", "ansible", "inventory.yml"), "r") as f:
            inventory = yaml.safe_load(f)

            # Extract variables from all.vars
            variables = {}
            if "all" in inventory and "vars" in inventory["all"]:
                variables = inventory["all"]["vars"]

            # get all the workers
            if "all" in inventory and "children" in inventory["all"] and "worker_nodes" in inventory["all"]["children"]:
                workers = inventory["all"]["children"]["worker_nodes"]["hosts"]
                for worker in workers:
                    ansible_host = workers[worker]["ansible_host"]
                    ansible_user = workers[worker]["ansible_user"]

                    # Replace variables in ansible_user
                    ansible_user = self._replace_variables(ansible_user, variables)

                    # Skip if variables couldn't be resolved
                    if "{{" in ansible_user:
                        print(f"Warning: Unresolved variables in {worker} user: {ansible_user}")
                        continue

                    worker_info[ansible_host] = ansible_user
                return worker_info

        print(f"No worker nodes found in the inventory file, your cluster is not applicable for this fault injector")
        return None

    def _replace_variables(self, value, variables):
        """Replace variables in a string with their values"""
        for key, val in variables.items():
            value = value.replace(f"{{{{{key}}}}}", str(val))
        return value

    def _setup_api_routes(self):
        """Setup API routes for external log ingestion"""

        @self.app.server.route("/api/logs", methods=["POST"])
        def receive_log():
            try:
                log_data = request.get_json()
                if log_data:
                    # Convert external log format to internal format
                    content = log_data.get("content", "")
                    sort = log_data.get("sort", "DUMMY")
                    log_entry = {
                        "type": sort,
                        "content": content,
                        "timestamp": log_data.get("timestamp", "Fault Extract Timestamp"),
                        "location": log_data.get("location", ""),
                    }
                    print(">>>", log_entry)
                    with self._log_lock:
                        self.external_log_queue.append(log_entry)
                    if sort == "STAGE" and content.startswith("Start"):
                        # try to find "<abc-ed>"
                        match = re.search(r"<(.*?)>", content)
                        if match:
                            self.namespace = match.group(1)
                return {"status": "success"}, 200
            except Exception as e:
                return {"status": "error", "message": str(e)}, 400

    def _generate_log_data(self):
        """Generate log data from external queue"""
        # Check for external logs first
        log_entry = None
        with self._log_lock:
            if self.external_log_queue:
                log_entry = self.external_log_queue.popleft()

        if log_entry is not None:
            # Add new log to history
            self.log_history.append(log_entry)
            # Return the latest log for display
            return log_entry
        # No external logs available, return None to skip this update
        return None

    def _drain_log_queue(self) -> list[dict]:
        """Drain and return all pending logs from the external queue in FIFO order."""
        drained: list[dict] = []
        with self._log_lock:
            while self.external_log_queue:
                drained.append(self.external_log_queue.popleft())
        if drained:
            self.log_history.extend(drained)
        return drained

    def _get_export_dir(self) -> Path:
        export_dir = os.getenv("SREGYM_DASH_EXPORT_DIR", ".")
        p = Path(export_dir).expanduser().resolve()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            p = Path(".").resolve()
        return p

    def _build_log_html(self, log_entry: dict) -> str:
        type_colors = {
            "STAGE": "#ffc107",
            "ENV": "#007bff",
            "LLM": "#6c757d",
            "PROMPT": "#28a745",
            "ERROR": "#dc3545",
            "EVAL": "#6f42c1",
            "SPLIT": "#dee2e6",
            "WARNING": "#fd7e14",
        }
        color = type_colors.get(log_entry.get("type", ""), "#6c757d")
        ts = log_entry.get("timestamp", "")
        typ = log_entry.get("type", "")
        loc = log_entry.get("location", "")
        content = (log_entry.get("content", "") or "").split("\n")
        content_html = "".join([("<br>" if i > 0 else "") + self._escape_html(line) for i, line in enumerate(content)])
        return (
            f'<div style="background-color:{color}20;border:1px solid {color};border-radius:8px;padding:8px;font-family:Consolas;">'
            f'<span style="color:#6c757d">[{self._escape_html(ts)}] </span>'
            f'<span style="color:{color};font-weight:bold;background-color:{color}20;padding:2px 6px;border-radius:4px;margin-right:8px;font-family:Consolas;font-size:12px;">{self._escape_html(typ)}</span>'
            f'<span style="color:#6c757d;font-family:Consolas;font-size:11px;margin-right:8px;">{self._escape_html(loc)}</span>'
            f'<span style="color:#2c3e50;font-family:Consolas;font-size:12px;">{content_html}</span>'
            f"</div>"
        )

    def _escape_html(self, s: str) -> str:
        return (
            (s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
        )

    def _build_status_html(self, cluster_data: dict | None) -> str:
        if not cluster_data:
            return "<div></div>"
        nodes = cluster_data.get("nodes", {}) if cluster_data else {}
        deployments = cluster_data.get("deployments", {}) if cluster_data else {}

        # Nodes
        node_spans: list[str] = []
        for node_name, node_info in nodes.items():
            ready = node_info.get("ready", "Unknown")
            issues = node_info.get("issues", [])
            if ready == "False":
                node_emoji = "🔴"
            elif ready == "Unknown":
                node_emoji = "❓"
            elif ready == "True":
                node_emoji = "🟡" if issues else "🟢"
            else:
                node_emoji = "❓"
            issues_text = ""
            if issues:
                issues_letters = [str(issue)[0].upper() for issue in issues]
                issues_text = " " + "".join(issues_letters)
            node_spans.append(
                f'<span style="margin-right:10px"><span style="margin-right:3px;font-size:12px;font-family:Consolas">{self._escape_html(node_name)}</span>'
                f'<span style="margin-right:3px;font-size:10px;font-family:Consolas">{node_emoji}</span>'
                f'<strong style="color:#ffc107;font-size:10px;font-family:Consolas">{self._escape_html(issues_text)}</strong></span>'
            )

        # Deployments split into 3 columns
        deployment_list = list(deployments.items())
        per_col = len(deployment_list) // 3 + (1 if len(deployment_list) % 3 != 0 else 0)
        col1 = deployment_list[:per_col]
        col2 = deployment_list[per_col : per_col * 2]
        col3 = deployment_list[per_col * 2 :]

        def build_col(items: list[tuple[str, dict]]) -> str:
            parts: list[str] = []
            for dep_name, dep_data in items:
                dep_meta = dep_data.get("deployment_meta", ("0/0/0", "gray"))
                pods = dep_data.get("pods", [])
                status_text, status_color = dep_meta
                dep_emoji = (
                    "🟢"
                    if status_color == "green"
                    else ("🟡" if status_color == "yellow" else ("🔴" if status_color == "red" else "⚪"))
                )

                pod_lines: list[str] = []
                all_pods_green = bool(pods) and all((p.get("status") == "Running" for p in pods))
                show_pods = not (self.CONCISE_TOGGLE and status_color == "green" and all_pods_green)
                if show_pods:
                    for pod in pods:
                        node_number = pod.get("node_number", -1)
                        node_emoji = f"{node_number}️⃣" if 1 <= node_number <= 9 else "❓"
                        status_letter = (pod.get("status") or "U")[0].upper()
                        color = pod.get("status_color", "#6c757d")
                        pod_lines.append(
                            f'<div style="margin-left:15px;margin-bottom:1px">'
                            f'<span style="margin-right:3px;font-size:10px;font-family:Consolas">{node_emoji}</span>'
                            f'<span style="margin-right:3px;font-size:10px;font-family:Consolas">{self._escape_html(pod.get("name", "Unknown"))}</span>'
                            f'<strong style="color:{color};font-weight:bold;font-size:10px;font-family:Consolas">{self._escape_html(status_letter)}</strong>'
                            f"</div>"
                        )

                parts.append(
                    f'<div style="margin-bottom:5px"><div style="margin-bottom:2px">'
                    f'<span style="margin-right:3px;font-size:10px;font-family:Consolas">{dep_emoji}</span>'
                    f'<strong style="margin-right:3px;font-size:11px;font-family:Consolas">{self._escape_html(dep_name)}</strong>'
                    f'<span style="color:#6c757d;font-size:10px;font-family:Consolas">{self._escape_html(status_text)}</span>'
                    f"</div><div>{''.join(pod_lines)}</div></div>"
                )
            return "".join(parts)

        col1_html = build_col(col1)
        col2_html = build_col(col2)
        col3_html = build_col(col3)

        return (
            f'<div style="border:1px solid #e9ecef;border-radius:8px;padding:8px;background-color:#ffffff">'
            f'<div style="margin-bottom:8px">{''.join(node_spans)}</div>'
            f'<div style="width:100%;overflow:hidden">'
            f'<div style="width:33.33%;float:left;padding-right:5px;box-sizing:border-box">{col1_html}</div>'
            f'<div style="width:33.33%;float:left;padding-right:5px;box-sizing:border-box">{col2_html}</div>'
            f'<div style="width:33.33%;float:left;box-sizing:border-box">{col3_html}</div>'
            f"</div></div>"
        )

    def _build_row_html(self, log_entry: dict, ckpt_state: dict | None, use_realtime_right: bool = False) -> str:
        left = self._build_log_html(log_entry)
        right = self._build_status_html(ckpt_state)
        return (
            f'<div style="width:100%;overflow:hidden;margin-bottom:6px">'
            f'<div style="width:50%;float:left;padding:4px;box-sizing:border-box">{left}</div>'
            f'<div style="width:50%;float:left;padding:4px;box-sizing:border-box">{right}</div>'
            f"</div>"
        )

    def _export_on_exit(self):
        try:
            # Freeze a consistent snapshot
            if self._state_lock.acquire(blocking=True):
                try:
                    # Drain any remaining logs and append to log_history; build export from log_history
                    remaining = self._drain_log_queue()
                    final_snapshot = self._collect_cluster_data(self.namespace)

                    export_dir = self._get_export_dir()
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    out_file = export_dir / f"sregym_dashboard_{ts}.html"

                    rows_html: list[str] = []
                    # Build from log_history in order; use final snapshot for right side
                    for log_entry in self.log_history:
                        if log_entry.get("type") == "SPLIT":
                            rows_html.append(
                                '<div><hr style="border:none;border-top:2px solid #dee2e6;margin:10px 0;width:100%"></div>'
                            )
                        else:
                            rows_html.append(self._build_row_html(log_entry, final_snapshot))

                    # Latest row with its saved checkpoint if exists
                    if self.latest_log is not None:
                        rows_html.append(self._build_row_html(self.latest_log or {}, final_snapshot))

                    html_doc = (
                        '<!DOCTYPE html><html><head><meta charset="utf-8">'
                        '<meta name="viewport" content="width=device-width, initial-scale=1">'
                        "<title>SREGym Dashboard Export</title>"
                        "<style>body{font-family:Arial,sans-serif;margin:0;padding:20px;background-color:#ffffff;}"
                        "</style></head><body>"
                        f"<div>{''.join(rows_html)}</div>"
                        '<footer style="background-color:#f8f9fa;border-top:1px solid #e9ecef;margin-top:20px;min-height:40px;display:flex;align-items:center;justify-content:center;">'
                        '<p style="text-align:center;color:#6c757d;margin:0;padding:10px">© 2025 SREGym Dashboard - xlab, UIUC</p>'
                        "</footer>"
                        "</body></html>"
                    )
                    with open(out_file, "w", encoding="utf-8") as f:
                        f.write(html_doc)
                    print(f"[SREGym Dashboard] Exported HTML to: {out_file}")
                finally:
                    try:
                        self._state_lock.release()
                    except Exception:
                        pass
        except Exception as e:
            print(f"[SREGym Dashboard] Export on exit failed: {e}")

    def _handle_signal(self, signum, frame):
        """
        Handle shutdown signals (SIGINT, SIGTERM) gracefully.
        Exports trace data before exiting.
        """
        if self._signal_handled:
            # Already handled a shutdown signal, ignore subsequent ones
            signal_name = (
                "SIGINT" if signum == signal.SIGINT else "SIGTERM" if signum == signal.SIGTERM else f"signal {signum}"
            )
            print(f"[SREGym Dashboard] Ignoring subsequent {signal_name} signal...")
            return

        # Mark that we've handled the signal
        self._signal_handled = True

        signal_name = (
            "SIGINT" if signum == signal.SIGINT else "SIGTERM" if signum == signal.SIGTERM else f"signal {signum}"
        )
        print(f"[SREGym Dashboard] Caught {signal_name}, exporting current view...")

        try:
            # Export trace data before exiting
            self._export_on_exit()
            print(f"[SREGym Dashboard] Export completed, shutting down...")
        except Exception as e:
            print(f"[SREGym Dashboard] Error during export: {e}", file=sys.stderr)
        finally:
            # Use os._exit() to ensure immediate termination after export
            # This is important for daemon processes
            os._exit(0)

    def _parse_concise_deployment_info(self, info_str):
        """Parse the concise deployment info from kubectl get deployment -o wide output"""
        if not info_str or not info_str.strip():
            return {}

        lines = info_str.strip().split("\n")
        if len(lines) < 2:
            return {}

        # Skip the header line
        data_lines = lines[1:]
        deployments = {}

        for line in data_lines:
            if not line.strip():
                continue

            # Split by whitespace
            parts = line.split()
            if len(parts) < 4:  # Minimum required fields: NAME, READY, UP-TO-DATE, AVAILABLE
                continue

            name = parts[0]
            ready = parts[1]  # Format: X/Y
            available = parts[3]  # AVAILABLE column

            # Create the format: {name: A/X/Y}
            real, expected = ready.split("/")
            if available == 0:  # red
                color = "red"
            elif available == expected:  # green
                color = "green"
            else:  # yellow
                color = "yellow"
            deployments[name] = (f"{available}/{ready}", color)

        return deployments

    def _parse_concise_pod_info(self, info_str):
        """Parse the concise pod info from kubectl get pod -o wide output"""
        # print(info_str)
        # print("\n\n\n\n")
        if not info_str or not info_str.strip():
            return []

        lines = info_str.strip().split("\n")
        if len(lines) < 2:
            return []

        # Skip the header line
        data_lines = lines[1:]
        pods_info = []

        for line in data_lines:
            if not line.strip():
                continue

            # Split by whitespace, but handle the case where some fields might be empty
            parts = line.split()
            if len(parts) < 8:  # Minimum required fields
                continue

            pod_info = {
                "name": parts[0],
                "ready": parts[1],
                "status": parts[2],
                "node": parts[6],
            }

            # only take the last 5 hash from the name
            # catch the part after the last two -
            parts = pod_info["name"].split("-")[:-2]
            # print(parts)
            pod_info["deployment_name"] = ""
            for part in parts:
                pod_info["deployment_name"] += part
                if part != parts[-1]:
                    pod_info["deployment_name"] += "-"
            pod_info["name"] = pod_info["name"][-5:]

            # Determine status color/emoji
            status = pod_info["status"]
            pod_info["color"] = "000000"
            if status == "Running":  # green
                pod_info["status_color"] = "#28a745"
            elif status in ["Pending", "ContainerCreating", "Terminating"]:  # yellow
                pod_info["status_color"] = "#ffc107"
            elif status in ["Failed", "Error", "CrashLoopBackOff"]:  # red
                pod_info["status_color"] = "#dc3545"
            else:  # gray
                pod_info["status_color"] = "#6c757d"

            # extract node number
            try:
                pod_info["node_number"] = int(pod_info["node"].split(".")[0][-1])
            except:
                pod_info["node_number"] = -1

            pods_info.append(pod_info)

            # extract deployment name

        return pods_info

    def _collect_cluster_data(self, app_namespace):
        """collect the cluster data"""
        try:
            deployments = self.kubectl.get_concise_deployments_info(app_namespace)
            pods_raw = self.kubectl.get_concise_pods_info(app_namespace)
            nodes = self.kubectl.list_nodes()

            # Process nodes information
            nodes_info = {}
            if nodes and hasattr(nodes, "items"):
                for node in nodes.items:
                    node_dict = node.to_dict() if hasattr(node, "to_dict") else node
                    status = node_dict.get("status", {})
                    conditions = status.get("conditions", [])

                    node_info = {
                        "ready": "Unknown",
                        "issues": [],
                    }

                    for condition in conditions:
                        if condition.get("type") == "Ready":
                            node_info["ready"] = condition.get("status", "Unknown")
                        else:
                            if condition.get("status") == "True":
                                node_info["issues"].append(condition.get("type", "Unknown"))

                    nodes_info[node_dict.get("metadata", {}).get("name", "Unknown").split(".")[0]] = node_info

            pods_info = self._parse_concise_pod_info(pods_raw)
            # print(pods_info)
            # print("\n\n\n\n")
            deployments_info = self._parse_concise_deployment_info(deployments) if deployments else {}
            deployments_info_with_pods = {}

            for deployment in deployments_info.keys():
                deployments_info_with_pods[deployment] = {
                    "pods": [pod for pod in pods_info if pod["deployment_name"] == deployment],
                    "deployment_meta": deployments_info[deployment],
                }

            return {"nodes": nodes_info, "deployments": deployments_info_with_pods, "namespace": app_namespace}

        except Exception as e:
            print(f"Error collecting cluster data: {str(e)}")
            return {"pods": [], "nodes": {}, "deployments": {}, "namespace": app_namespace, "error": str(e)}

    def _render_log_block(self, log_entry: dict) -> html.Div:
        """Render a single log entry as a colored block based on its type."""
        type_colors = {
            "STAGE": "#ffc107",  # yellow
            "ENV": "#007bff",  # blue
            "LLM": "#6c757d",  # gray
            "PROMPT": "#28a745",  # green
            "ERROR": "#dc3545",  # red
            "EVAL": "#6f42c1",  # purple
            "SPLIT": "#dee2e6",  # light gray
            "WARNING": "#fd7e14",  # orange-red
        }
        color = type_colors.get(log_entry.get("type", ""), "#6c757d")
        container_style = {
            "background-color": color + "20",
            "border": f"1px solid {color}",
            "border-radius": "8px",
            "padding": "8px",
            "font-family": "Consolas",
        }

        # Split content by newlines and create proper line breaks
        content_lines = log_entry.get("content", "").split("\n")

        # Build full content elements
        full_elements = []
        for i, line in enumerate(content_lines):
            if i > 0:
                full_elements.append(html.Br())
            full_elements.append(line)

        # If more than 6 lines, render a collapsible block with a preview (first 6 lines)
        if len(content_lines) > 6:
            preview_elements = []
            for i, line in enumerate(content_lines[:6]):
                if i > 0:
                    preview_elements.append(html.Br())
                preview_elements.append(line)
            # Add an ellipsis hint in the preview
            preview_elements.append(html.Span(" …", style={"color": "#6c757d"}))

            content_component = html.Details(
                [
                    html.Summary(
                        html.Span(
                            preview_elements, style={"color": "#2c3e50", "font-family": "Consolas", "font-size": "12px"}
                        )
                    ),
                    html.Div(
                        full_elements,
                        style={"color": "#2c3e50", "font-family": "Consolas", "font-size": "12px", "marginTop": "6px"},
                    ),
                ]
            )
        else:
            content_component = html.Span(
                full_elements, style={"color": "#2c3e50", "font-family": "Consolas", "font-size": "12px"}
            )

        return html.Div(
            [
                html.Span(f"[{log_entry.get('timestamp', '')}] ", style={"color": "#6c757d"}),
                html.Span(
                    log_entry.get("type", ""),
                    style={
                        "color": color,
                        "font-weight": "bold",
                        "background-color": color + "20",
                        "padding": "2px 6px",
                        "border-radius": "4px",
                        "margin-right": "8px",
                        "font-family": "Consolas",
                        "font-size": "12px",
                    },
                ),
                html.Span(
                    f"{log_entry.get('location', '')}",
                    style={"color": "#6c757d", "font-family": "Consolas", "font-size": "11px", "margin-right": "8px"},
                ),
                content_component,
            ],
            style=container_style,
        )

    def _render_split_line(self) -> html.Div:
        """Render a simple split line for SPLIT type logs in history."""
        return html.Div(
            [html.Hr(style={"border": "none", "border-top": "2px solid #dee2e6", "margin": "10px 0", "width": "100%"})],
            style={"width": "100%", "text-align": "center"},
        )

    def _render_status_block(self, cluster_data: dict) -> html.Div:
        """Render the status block (nodes + deployments) given cluster_data."""
        nodes = cluster_data.get("nodes", {}) if cluster_data else {}
        deployments = cluster_data.get("deployments", {}) if cluster_data else {}

        # Nodes row
        node_items = []
        for node_name, node_info in nodes.items():
            ready = node_info.get("ready", "Unknown")
            issues = node_info.get("issues", [])
            if ready == "False":
                node_emoji = "🔴"
            elif ready == "Unknown":
                node_emoji = "❓"
            elif ready == "True":
                node_emoji = "🟡" if issues else "🟢"
            else:
                node_emoji = "❓"
            issues_text = ""
            if issues:
                issues_letters = [issue[0].upper() for issue in issues]
                issues_text = f" {''.join(issues_letters)}"
            node_items.append(
                html.Span(
                    [
                        html.Span(
                            node_name, style={"margin-right": "3px", "font-size": "12px", "font-family": "Consolas"}
                        ),
                        html.Span(
                            node_emoji, style={"margin-right": "3px", "font-size": "10px", "font-family": "Consolas"}
                        ),
                        html.Strong(
                            issues_text, style={"color": "#ffc107", "font-size": "10px", "font-family": "Consolas"}
                        ),
                    ],
                    style={"margin-right": "10px"},
                )
            )

        # Deployments 3 columns
        deployment_list = list(deployments.items())
        per_col = len(deployment_list) // 3 + (1 if len(deployment_list) % 3 != 0 else 0)
        col1 = deployment_list[:per_col]
        col2 = deployment_list[per_col : per_col * 2]
        col3 = deployment_list[per_col * 2 :]

        def build_col(items: list[tuple[str, dict]]) -> list:
            column_items: list = []
            for dep_name, dep_data in items:
                dep_meta = dep_data.get("deployment_meta", ("0/0/0", "gray"))
                pods = dep_data.get("pods", [])
                status_text, status_color = dep_meta
                dep_emoji = (
                    "🟢"
                    if status_color == "green"
                    else ("🟡" if status_color == "yellow" else ("🔴" if status_color == "red" else "⚪"))
                )
                pod_items = []
                # Concise: hide pods if deployment is green and all pods are Running
                all_pods_green = bool(pods) and all(p.get("status") == "Running" for p in pods)
                show_pods = not (self.CONCISE_TOGGLE and status_color == "green" and all_pods_green)
                if show_pods:
                    for pod in pods:
                        node_number = pod.get("node_number", -1)
                        node_emoji = f"{node_number}️⃣" if 1 <= node_number <= 9 else "❓"
                        status_letter = (pod.get("status") or "U")[0].upper()
                        pod_items.append(
                            html.Div(
                                [
                                    html.Span(
                                        node_emoji,
                                        style={"margin-right": "3px", "font-size": "10px", "font-family": "Consolas"},
                                    ),
                                    html.Span(
                                        pod.get("name", "Unknown"),
                                        style={"margin-right": "3px", "font-size": "10px", "font-family": "Consolas"},
                                    ),
                                    html.Strong(
                                        status_letter,
                                        style={
                                            "color": pod.get("status_color", "#6c757d"),
                                            "font-weight": "bold",
                                            "font-size": "10px",
                                            "font-family": "Consolas",
                                        },
                                    ),
                                ],
                                style={"margin-left": "15px", "margin-bottom": "1px"},
                            )
                        )
                column_items.append(
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Span(
                                        dep_emoji,
                                        style={"margin-right": "3px", "font-size": "10px", "font-family": "Consolas"},
                                    ),
                                    html.Strong(
                                        dep_name,
                                        style={"margin-right": "3px", "font-size": "11px", "font-family": "Consolas"},
                                    ),
                                    html.Span(
                                        status_text,
                                        style={"color": "#6c757d", "font-size": "10px", "font-family": "Consolas"},
                                    ),
                                ],
                                style={"margin-bottom": "2px"},
                            ),
                            html.Div(pod_items),
                        ],
                        style={"margin-bottom": "5px"},
                    )
                )
            return column_items

        col1_items = build_col(col1)
        col2_items = build_col(col2)
        col3_items = build_col(col3)

        return html.Div(
            [
                html.Div(node_items, style={"margin-bottom": "8px"}),
                html.Div(
                    [
                        html.Div(
                            col1_items,
                            style={
                                "width": "33.33%",
                                "float": "left",
                                "padding-right": "5px",
                                "box-sizing": "border-box",
                            },
                        ),
                        html.Div(
                            col2_items,
                            style={
                                "width": "33.33%",
                                "float": "left",
                                "padding-right": "5px",
                                "box-sizing": "border-box",
                            },
                        ),
                        html.Div(col3_items, style={"width": "33.33%", "float": "left", "box-sizing": "border-box"}),
                    ],
                    style={"width": "100%", "overflow": "hidden"},
                ),
            ],
            style={
                "border": "1px solid #e9ecef",
                "border-radius": "8px",
                "padding": "8px",
                "background-color": "#ffffff",
            },
        )

    def _setup_layout(self):
        """Setup the application layout"""
        self.app.layout = html.Div(
            [
                # Main container: multiple rows (log + status)
                html.Div(
                    [
                        html.Div(
                            id="rows-display",
                            style={
                                "width": "100%",
                                "box-sizing": "border-box",
                                "max-height": "70vh",
                                "overflowY": "auto",
                                "scrollBehavior": "smooth",
                            },
                        ),
                        dcc.Store(id="scroll-anchor"),
                        dcc.Store(id="new-log-trigger", data=None),
                    ],
                    style={"width": "100%", "overflow": "hidden", "margin-bottom": "20px"},
                ),
                # Footer area
                html.Div(
                    [
                        html.P(
                            "© 2025 SREGym Dashboard - xlab, UIUC",
                            style={"text-align": "center", "color": "#6c757d", "margin": "0", "padding": "10px"},
                        )
                    ],
                    style={
                        "background-color": "#f8f9fa",
                        "border-top": "1px solid #e9ecef",
                        "margin-top": "20px",
                        "min-height": "40px",
                        "display": "flex",
                        "align-items": "center",
                        "justify-content": "center",
                    },
                ),
                # Timer component
                dcc.Interval(id="interval-component", interval=3000, n_intervals=0),  # Update every 3 seconds
            ],
            style={
                "font-family": "Arial, sans-serif",
                "margin": "0",
                "padding": "20px",
                "background-color": "#ffffff",
                "min-height": "100vh",
            },
        )

    def _setup_callbacks(self):
        """Setup the application callbacks"""

        # Auto-scroll rows container to bottom only when new log arrives
        self.app.clientside_callback(
            ClientsideFunction(namespace="utils", function_name="scrollToBottom"),
            Output("scroll-anchor", "data"),
            Input("new-log-trigger", "data"),
        )

        @self.app.callback(
            [Output("rows-display", "children"), Output("new-log-trigger", "data")],
            Input("interval-component", "n_intervals"),
            State("rows-display", "children"),
        )
        def update_rows(n, current_children):
            """Concurrency-safe render: build from server state only, ignore current_children."""
            # Always fetch realtime cluster state once per tick
            # If another invocation is updating, skip this tick to avoid piling up
            if self._state_lock.locked():
                return dash.no_update, None
            print(f"<<<<<<<<<< Try update rows: {n}, children: {len(current_children) if current_children else 0}")

            # Collect realtime cluster data outside of lock (can be slow)

            # Try to enter critical section without blocking
            if not self._state_lock.acquire(blocking=False):
                return dash.no_update, None
            try:

                realtime_state = self._collect_cluster_data(self.namespace)
                # Drain all pending logs under the state lock to preserve ordering
                new_logs = self._drain_log_queue()
                print(
                    f"<<<<<<<<<< Entered critical section, Fetched New logs: {len(new_logs)}, children: {len(current_children) if current_children else 0}"
                )
                # No new logs: refresh only the live row if exists
                if not new_logs:
                    if self.latest_log is not None:
                        latest_row = html.Div(
                            [
                                html.Div(
                                    self._render_log_block(self.latest_log),
                                    style={
                                        "width": "50%",
                                        "float": "left",
                                        "padding": "4px",
                                        "box-sizing": "border-box",
                                    },
                                ),
                                html.Div(
                                    self._render_status_block(realtime_state),
                                    style={
                                        "width": "50%",
                                        "float": "left",
                                        "padding": "4px",
                                        "box-sizing": "border-box",
                                    },
                                ),
                            ],
                            style={"width": "100%", "overflow": "hidden"},
                        )
                        children = list(self.history_rows)
                        children.append(latest_row)
                        return children, None
                    # No latest log yet, just return history
                    print(f"<<<<<<<<<< No latest log yet, just return history")
                    return list(self.history_rows), None

                # New logs arrived: snapshot cluster state as the checkpoint for this batch
                snapshot_state = realtime_state

                # If there was a previous latest log, push it to history using its checkpoint
                if self.latest_log is not None and self.latest_ckpt_state is not None:
                    if self.latest_log.get("type") == "SPLIT":
                        self.history_rows.append(self._render_split_line())
                    else:
                        self.history_rows.append(
                            html.Div(
                                [
                                    html.Div(
                                        self._render_log_block(self.latest_log),
                                        style={
                                            "width": "50%",
                                            "float": "left",
                                            "padding": "4px",
                                            "box-sizing": "border-box",
                                        },
                                    ),
                                    html.Div(
                                        self._render_status_block(self.latest_ckpt_state),
                                        style={
                                            "width": "50%",
                                            "float": "left",
                                            "padding": "4px",
                                            "box-sizing": "border-box",
                                        },
                                    ),
                                ],
                                style={"width": "100%", "overflow": "hidden", "margin-bottom": "6px"},
                            )
                        )

                # Push all but the newest drained logs into history using the batch snapshot
                if len(new_logs) > 1:
                    for queued_log in new_logs[:-1]:
                        if queued_log.get("type") == "SPLIT":
                            self.history_rows.append(self._render_split_line())
                        else:
                            self.history_rows.append(
                                html.Div(
                                    [
                                        html.Div(
                                            self._render_log_block(queued_log),
                                            style={
                                                "width": "50%",
                                                "float": "left",
                                                "padding": "4px",
                                                "box-sizing": "border-box",
                                            },
                                        ),
                                        html.Div(
                                            self._render_status_block(snapshot_state),
                                            style={
                                                "width": "50%",
                                                "float": "left",
                                                "padding": "4px",
                                                "box-sizing": "border-box",
                                            },
                                        ),
                                    ],
                                    style={"width": "100%", "overflow": "hidden", "margin-bottom": "6px"},
                                )
                            )

                # Update latest pointers with the newest log in this batch
                self.latest_log = new_logs[-1]
                self.latest_ckpt_state = snapshot_state

                # Build the new latest row with real-time state on the right
                latest_row = html.Div(
                    [
                        html.Div(
                            self._render_log_block(self.latest_log),
                            style={"width": "50%", "float": "left", "padding": "4px", "box-sizing": "border-box"},
                        ),
                        html.Div(
                            self._render_status_block(realtime_state),
                            style={"width": "50%", "float": "left", "padding": "4px", "box-sizing": "border-box"},
                        ),
                    ],
                    style={"width": "100%", "overflow": "hidden"},
                )

                children = list(self.history_rows)
                children.append(latest_row)
                print(f"<<<<<<<<<< now children list has: {len(children)}")
                return children, True
            finally:
                self._state_lock.release()

    def get_log_history(self):
        """Get the complete log history"""
        return self.log_history

    def clear_log_history(self):
        """Clear the log history"""
        self.log_history = []

    def run(self, threaded=False):
        """Start the dashboard server"""
        print(f"Starting SREGym Dashboard on {self.host}:{self.port}")
        in_main_thread = threading.current_thread() is threading.main_thread()
        if threaded or not in_main_thread:
            # When running in a thread, disable debug reloader and signals
            self.app.run(
                debug=False,
                host=self.host,
                port=self.port,
                use_reloader=False,
                # dev_tools_silence_routes_logging=True,
                threaded=True,
            )
        else:
            # Main thread: allow normal debug behavior
            # Disable reloader so Ctrl-C cleanly stops the server
            self.app.run(
                debug=self.debug,
                host=self.host,
                port=self.port,
                use_reloader=False,
                threaded=True,
                # dev_tools_silence_routes_logging=True,
            )


if __name__ == "__main__":
    # Create and run the dashboard server
    dashboard = SREGymDashboardServer(host="127.0.0.1", port=11451, debug=True)
    dashboard.run()
