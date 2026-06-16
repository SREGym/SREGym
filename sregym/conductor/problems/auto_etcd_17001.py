"""Auto-generated from https://github.com/etcd-io/etcd/issues/17001

Title: Using a direct method call with v3client that doesn't go through gRPC to call the Endpoints() method results in a null pointer, causing a panic.
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd17001(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/17001"
    root_cause_description = (
        "Using a direct method call with v3client that doesn't go through gRPC to call the Endpoints() method results in a null pointer, causing a panic. . ### Bug report criteria - [X] This bug report is not security related, security issues should be disclosed privately via security@etcd.io. - [X] This is not a support request or question, support requests or questions should be raised in the etcd [discussion forums](https://github.com/etcd-io/etcd/discussions). - [X] You have read the etcd [bug reporting guidelines](https://github.com/etcd-io/etcd/blob/main/Documentation/contributor-guide/reporting_bugs.md). - [X] Existing open issues along with etcd [frequently asked questions](https://etcd.io/docs/latest/faq) have been checked and this is not a duplicate. ### What happened? Using a direct method call with v3client that doesn't go through gRPC to call the Endpoints() method results in a null pointer, causing a panic. The reaso"
    )
    reproducer = 'package main\n\nimport (\n\t"go.etcd.io/etcd/server/v3/etcdserver/api/v3client"\n\t"log"\n\t"time"\n\n\t"go.etcd.io/etcd/server/v3/embed"\n)\n\nfunc main() {\n\tcfg := embed.NewConfig()\n\tcfg.Dir = "default.etcd"\n\tcfg.MaxSnapFiles = 10\n\tcfg.MaxSnapFiles = 10\n\n\te, err := embed.StartEtcd(cfg)\n\tif err != nil {\n\t\tlog.Fatal(err)\n\t}\n\tselect {\n\tcase <-e.Server.ReadyNotify():\n\t\tlog.Println("Embedded etcd is ready!")\n\t\tcli := v3client.New(e.Server)\n\t\tendpoints := cli.Endpoints()\n\t\tlog.Printf("endpoint is %v", endpoints)\n\n\tcase <-time.After(60 * time.Second):\n\t\te.Server.Stop()\n\t\te.Close()\n\t\tlog.Fatal("Embedded etcd took too long to start!")\n\t}\n\n\t<-e.Server.StopNotify()\n\tlog.Println("Embedded etcd is stopped")\n\n}'
    continuous_reproducer = True
