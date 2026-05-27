import json
import textwrap

from sregym.conductor.oracles.edge_request_filter_mitigation import EdgeRequestFilterMitigationOracle
from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.problems.base import Problem
from sregym.service.apps.astronomy_shop import AstronomyShop
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

    def __init__(self):
        self.app = AstronomyShop()
        super().__init__(app=self.app, namespace=self.app.namespace)
        self.kubectl = KubeCtl()
        self.namespace = self.app.namespace
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

                my $raw_request = join("", @headers);
                $raw_request .= $body;
                $raw_request =~ s/\\r?\\nConnection:.*?\\r?\\n/\\r\\n/ig;
                $raw_request =~ s/\\r?\\nHost:.*?\\r?\\n/\\r\\n/ig;
                my $upstream_headers = "\\r\\nHost: "
                    . $upstream_host . ":" . $upstream_port
                    . "\\r\\nConnection: close\\r\\n\\r\\n";
                $raw_request =~ s/\\r?\\n\\r?\\n$/$upstream_headers/;

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
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} --timeout=120s"
        )

    def _start_crafted_traffic(self):
        driver = textwrap.dedent(
            f"""
            import time
            import urllib.request

            marker = "{self.process_marker}"
            url = "http://frontend-proxy:8080/?waf={self.crafted_payload}"
            while True:
                try:
                    urllib.request.urlopen(url, timeout=10).read()
                except Exception:
                    pass
                time.sleep({self.driver_interval_seconds})
            """
        ).strip()
        launcher = f"exec({driver!r})"
        shell = (
            f"rm -f {self.driver_log} {self.driver_pid}; "
            f"nohup /usr/local/bin/python3 -c {json.dumps(launcher)} "
            f">{self.driver_log} 2>&1 & echo \\$! > {self.driver_pid}"
        )
        self.kubectl.exec_command(
            f"kubectl exec deployment/{self.traffic_source} -n {self.namespace} -- /bin/sh -c {json.dumps(shell)}"
        )

    def _stop_crafted_traffic(self):
        command = (
            f"if [ -f {self.driver_pid} ]; then "
            f"kill \\$(cat {self.driver_pid}) 2>/dev/null || true; "
            f"rm -f {self.driver_pid}; "
            "fi"
        )
        self.kubectl.exec_command(
            f"kubectl exec deployment/{self.traffic_source} -n {self.namespace} -- /bin/sh -c {json.dumps(command)}"
        )

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        self._patch_edge_proxy()
        self._start_crafted_traffic()
        self.kubectl.wait_for_ready(self.namespace, max_wait=180)
        print(
            f"Fault: EdgeRequestFilterCPUSaturation | Service: {self.faulty_service} | "
            f"Namespace: {self.namespace} | Regex: {self.bad_regex}\n"
        )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        self._stop_crafted_traffic()
        self.kubectl.exec_command(f"kubectl rollout undo deployment/{self.faulty_service} -n {self.namespace}")
        self.kubectl.exec_command(
            f"kubectl rollout status deployment/{self.faulty_service} -n {self.namespace} --timeout=120s"
        )
        print(
            f"Recovered: stopped crafted requests and rolled back deployment/{self.faulty_service} "
            f"in namespace {self.namespace}\n"
        )
