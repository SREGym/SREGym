import copy
import shlex
import textwrap

from sregym.conductor.oracles.edge_request_filter_mitigation import EdgeRequestFilterMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.base import Application
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class EdgeRequestFilterCPUSaturation(Problem):
    """Inject a vulnerable WAF regex into the existing Astronomy Shop edge proxy.

    The fault does not create any new pods. It patches the existing
    frontend-proxy container command so the pod starts a small Perl edge
    request filter on the normal proxy port. Crafted requests from the existing
    load-generator deployment trigger catastrophic regex backtracking and pin
    the edge proxy CPU.
    """

    def __init__(self, *, app: Application | None = None):
        super().__init__(app=app if app is not None else AstronomyShop())
        self.kubectl = KubeCtl()
        self.faulty_service = "frontend-proxy"
        self.traffic_source = "load-generator"
        self.regex_env = "WAF_RULE_REGEX"
        self.bad_regex = "^([a-zA-Z]+)*$"
        self.safe_regex = "^[A-Za-z]+$"
        self.crafted_payload = "a" * 5000 + "!"
        self.cpu_limit = "100m"
        self.memory_limit = "65Mi"
        self.process_marker = "edge-traffic-replay"
        self.driver_interval_seconds = 0.05
        self.driver_log = "/tmp/edge-traffic-replay.log"
        self.driver_pid = "/tmp/edge-traffic-replay.pid"
        self._baseline_template = None
        self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                "A recent edge/WAF request-filter rule update on deployment `frontend-proxy` introduced the "
                "vulnerable "
                f"regex `{self.bad_regex}`. Requests carrying long near-matching `waf` query values trigger "
                "catastrophic backtracking in the edge proxy request filter, driving CPU saturation and causing "
                "timeouts for otherwise healthy HTTP paths. The fix is to roll back or disable the bad rule, or "
                f"replace it with a linear-time equivalent such as `{self.safe_regex}`."
            ),
        )
        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)
        self.mitigation_oracle = EdgeRequestFilterMitigationOracle(problem=self)

    def _capture_baseline_template(self):
        if self._baseline_template is not None:
            return

        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        self._baseline_template = copy.deepcopy(deployment.spec.template)

    def _restore_baseline_template(self):
        if self._baseline_template is None:
            raise RuntimeError("Cannot recover edge filter fault without a captured baseline template")

        deployment = self.kubectl.get_deployment(self.faulty_service, self.namespace)
        deployment.spec.template = copy.deepcopy(self._baseline_template)
        self.kubectl.apps_v1_api.replace_namespaced_deployment(
            name=self.faulty_service,
            namespace=self.namespace,
            body=deployment,
        )

    def _edge_filter_script(self) -> str:
        return textwrap.dedent(
            f"""
            use IO::Socket::INET;
            use IO::Select;

            my $rule = $ENV{{{self.regex_env}}} || q{{{self.bad_regex}}};
            my $port = $ENV{{ENVOY_PORT}} || 8080;
            my $upstream_host = $ENV{{FRONTEND_HOST}} || "frontend";
            my $upstream_port = $ENV{{FRONTEND_PORT}} || 8080;
            my $server = IO::Socket::INET->new(
                LocalAddr => "0.0.0.0",
                LocalPort => $port,
                Proto => "tcp",
                Listen => 128,
                ReuseAddr => 1
            ) or die "listen failed: $!";

            print "edge request filter listening on $port\\n";
            while (my $client = $server->accept()) {{
                my $request = <$client> || "";
                my @headers = ($request);
                while (my $line = <$client>) {{
                    push @headers, $line;
                    last if $line =~ /^\\r?\\n$/;
                }}
                my $content_length = 0;
                for my $header (@headers) {{
                    if ($header =~ /^Content-Length:\\s*(\\d+)\\s*$/i) {{
                        $content_length = $1;
                        last;
                    }}
                }}
                my $body = "";
                while (length($body) < $content_length) {{
                    my $chunk = "";
                    my $read_bytes = read($client, $chunk, $content_length - length($body));
                    last if !$read_bytes;
                    $body .= $chunk;
                }}

                my ($candidate) = $request =~ /[?&]waf=([^ &]+)/;
                $candidate ||= "";
                my $start = time();

                if (($ENV{{WAF_RULE_ENABLED}} || "true") ne "false") {{
                    $candidate =~ /$rule/;
                }}

                my $elapsed_s = time() - $start;
                print qq({{"event":"request_filter_eval","rule":"$rule",)
                    . qq("candidateLength":) . length($candidate)
                    . qq(,"elapsedSeconds":$elapsed_s}}\\n);

                if ($candidate ne "") {{
                    print $client "HTTP/1.1 200 OK\\r\\nContent-Type: text/plain\\r\\n"
                        . "Connection: close\\r\\nContent-Length: 3\\r\\n\\r\\nok\\n";
                    close $client;
                    next;
                }}

                my $upstream = IO::Socket::INET->new(
                    PeerHost => $upstream_host,
                    PeerPort => $upstream_port,
                    Proto => "tcp",
                    Timeout => 5
                );

                if (!$upstream) {{
                    print $client "HTTP/1.1 502 Bad Gateway\\r\\nContent-Type: text/plain\\r\\n"
                        . "Connection: close\\r\\nContent-Length: 12\\r\\n\\r\\nbad gateway\\n";
                    close $client;
                    next;
                }}

                my @forward_headers = grep {{ !/^(Host|Connection):/i }} @headers;
                my $raw_request = join("", @forward_headers);
                $raw_request =~ s/\\r?\\n\\r?\\n$//;
                my $upstream_headers = "\\r\\nHost: "
                    . $upstream_host . ":" . $upstream_port
                    . "\\r\\nConnection: close\\r\\n\\r\\n";
                $raw_request .= $upstream_headers . $body;

                print $upstream $raw_request;
                my $selector = IO::Select->new($upstream);
                my $buffer;
                while ($selector->can_read(1)) {{
                    my $read_bytes = read($upstream, $buffer, 8192);
                    last if !$read_bytes;
                    print $client $buffer;
                }}
                close $upstream;
                close $client;
            }}
            """
        ).strip()

    def _patch_edge_proxy(self):
        script = self._edge_filter_script()
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "edge.platform/change-ticket": "EDGE-1847",
                            "edge.platform/filter-profile": "managed-rules-v2",
                        }
                    },
                    "spec": {
                        "containers": [
                            {
                                "name": self.faulty_service,
                                "command": ["/usr/bin/perl", "-e"],
                                "args": [script],
                                "env": [
                                    {"name": self.regex_env, "value": self.bad_regex},
                                    {"name": "WAF_RULE_ENABLED", "value": "true"},
                                ],
                                "resources": {"limits": {"cpu": self.cpu_limit, "memory": self.memory_limit}},
                            }
                        ]
                    },
                }
            }
        }
        self.kubectl.apps_v1_api.patch_namespaced_deployment(
            name=self.faulty_service,
            namespace=self.namespace,
            body=body,
        )
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} --timeout=120s"
        )

    def _traffic_python(self, script: str) -> str:
        shell = 'PY=$(command -v python3 || command -v python); test -n "$PY"; exec "$PY" -c ' + shlex.quote(script)
        output = self.kubectl.exec_command_checked(
            shlex.join(
                [
                    "kubectl",
                    "exec",
                    f"deployment/{self.traffic_source}",
                    "-n",
                    self.namespace,
                    "-c",
                    self.traffic_source,
                    "--",
                    "/bin/sh",
                    "-c",
                    shell,
                ]
            ),
            timeout=60,
        )
        if output.strip():
            print(output.strip())
        return output

    def _start_crafted_traffic(self):
        service = getattr(self, "service_name", self.faulty_service)
        port = getattr(self, "listen_port", 8080)
        driver = textwrap.dedent(
            f"""
            import time
            import urllib.request

            marker = "{self.process_marker}"
            url = "http://{service}:{port}/?waf={self.crafted_payload}"
            while True:
                try:
                    urllib.request.urlopen(url, timeout=10).read()
                except Exception as exc:
                    print("Edge traffic request failed:", type(exc).__name__, str(exc), flush=True)
                time.sleep({self.driver_interval_seconds})
            """
        ).strip()
        self._traffic_python(
            textwrap.dedent(
                f"""
                import pathlib, subprocess, sys, time

                with open({self.driver_log!r}, "w") as log:
                    process = subprocess.Popen(
                        [sys.executable, "-u", "-c", {driver!r}],
                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                    )
                pathlib.Path({self.driver_pid!r}).write_text(str(process.pid))
                time.sleep(0.2)
                if process.poll() is not None:
                    raise RuntimeError("Edge traffic process exited: " + pathlib.Path({self.driver_log!r}).read_text())
                print("Edge traffic process started:", process.pid)
                """
            )
        )

    def _stop_crafted_traffic(self):
        marker_split = max(1, len(self.process_marker) // 2)
        marker_prefix = self.process_marker[:marker_split]
        marker_suffix = self.process_marker[marker_split:]
        cleanup = textwrap.dedent(
            f"""
            import os
            import signal

            marker = {marker_prefix!r} + {marker_suffix!r}
            marker_bytes = marker.encode()
            for entry in os.listdir("/proc"):
                if not entry.isdigit() or int(entry) == os.getpid():
                    continue
                try:
                    with open(f"/proc/{{entry}}/cmdline", "rb") as command_file:
                        command = command_file.read()
                    if marker_bytes in command:
                        os.kill(int(entry), signal.SIGTERM)
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    pass

            for path in ({self.driver_pid!r}, {self.driver_log!r}):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            """
        ).strip()
        self._traffic_python(cleanup)

    def _verify_fault_effect(self):
        service = getattr(self, "service_name", self.faulty_service)
        port = getattr(self, "listen_port", 8080)
        path = getattr(self, "health_path", "/")
        host = getattr(self, "probe_host_header", service)
        self._traffic_python(
            textwrap.dedent(
                f"""
                import os, pathlib, time, urllib.error, urllib.request

                os.kill(int(pathlib.Path({self.driver_pid!r}).read_text()), 0)
                request = urllib.request.Request("http://{service}:{port}{path}", headers={{"Host": {host!r}}})
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    os.kill(int(pathlib.Path({self.driver_pid!r}).read_text()), 0)
                    try:
                        with urllib.request.urlopen(request, timeout=3) as response:
                            response.read()
                    except TimeoutError:
                        print("Edge traffic is active and the normal request times out")
                        break
                    except urllib.error.URLError as exc:
                        if not isinstance(exc.reason, TimeoutError):
                            raise
                        print("Edge traffic is active and the normal request times out")
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError("Crafted traffic did not disrupt the edge: " + pathlib.Path({self.driver_log!r}).read_text()[-1000:])
                """
            )
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._capture_baseline_template()
        self._stop_crafted_traffic()
        self._patch_edge_proxy()
        self.kubectl.wait_for_ready(self.namespace, max_wait=180)
        self._start_crafted_traffic()
        self._verify_fault_effect()
        print(
            f"Fault: EdgeRequestFilterCPUSaturation | Service: {self.faulty_service} | "
            f"Namespace: {self.namespace} | Regex: {self.bad_regex}\n"
        )

    @mark_fault_injected(strict=True)
    def recover_fault(self):
        print("== Fault Recovery ==")
        if self._baseline_template is None:
            print("No edge template was changed by this problem; nothing to restore")
            return
        self._stop_crafted_traffic()
        self._restore_baseline_template()
        self.kubectl.exec_command_checked(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} --timeout=120s"
        )
        self._baseline_template = None
        print(
            f"Recovered: stopped crafted requests and restored deployment/{self.faulty_service} "
            f"in namespace {self.namespace}\n"
        )
