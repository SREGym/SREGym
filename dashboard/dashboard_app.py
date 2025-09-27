import json
import os
import re
import sys
import time
from datetime import datetime

import dash
import pandas as pd
import plotly.express as px
import plotly.graph_objs as go
from dash import Input, Output, State, callback, dcc, html
from dash.dependencies import ClientsideFunction

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "srearena", "service"))
import threading
from collections import deque

import yaml
from flask import request
from kubectl import KubeCtl

DASHBOARD_URL = "http://127.0.0.1:11451"


class SREArenaDashboardServer:
    """SRE Arena Dashboard Server Class"""

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

        # Setup the application
        self._setup_layout()
        self._setup_callbacks()
        self._setup_api_routes()
        self.namespace = "default"

        self.kubectl = KubeCtl()
        # self.host_info = self.get_host_info()

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
                            "© 2025 SREArena Dashboard - xlab, UIUC",
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
                dcc.Interval(id="interval-component", interval=1000, n_intervals=0),  # Update every 3 seconds
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

            # Collect realtime cluster data outside of lock (can be slow)
            realtime_state = self._collect_cluster_data(self.namespace)

            # Try to enter critical section without blocking
            if not self._state_lock.acquire(blocking=False):
                return dash.no_update, None
            try:
                # Pop next log under the state lock to preserve strict ordering
                new_log = self._generate_log_data()
                # No new log: refresh only the live row if exists
                if new_log is None:
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
                    return list(self.history_rows), None

                # New log arrived: snapshot cluster state as the checkpoint for this log
                snapshot_state = realtime_state

                # If there was a previous latest log, push it to history using its checkpoint
                if self.latest_log is not None and self.latest_ckpt_state is not None:
                    # Special handling for SPLIT type logs in history
                    if self.latest_log.get("type") == "SPLIT":
                        history_child = self._render_split_line()
                    else:
                        history_child = html.Div(
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
                    self.history_rows.append(history_child)

                # Update latest pointers
                self.latest_log = new_log
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
        print(f"Starting SRE Arena Dashboard on {self.host}:{self.port}")
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
                # dev_tools_silence_routes_logging=True,
            )


if __name__ == "__main__":
    # Create and run the dashboard server
    dashboard = SREArenaDashboardServer(host="127.0.0.1", port=11451, debug=True)
    dashboard.run()
