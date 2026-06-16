"""Auto-generated from https://github.com/etcd-io/etcd/issues/19509

Title: [3.6] panic: Write called after Handler finished
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd19509(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/19509"
    root_cause_description = (
        "[3.6] panic: Write called after Handler finished. ### Bug report criteria - [x] This bug report is not security related, security issues should be disclosed privately via [etcd maintainers](mailto:etcd-maintainers@googlegroups.com). - [x] This is not a support request or question, support requests or questions should be raised in the etcd [discussion forums](https://github.com/etcd-io/etcd/discussions). - [x] You have read the etcd [bug reporting guidelines](https://github.com/etcd-io/etcd/blob/main/Documentation/contributor-guide/reporting_bugs.md). - [x] Existing open issues along with etcd [frequently asked questions](https://etcd.io/docs/latest/faq) have been checked and this is not a duplicate. ### What happened? etcd panics when watch requests are issued via the GRPC-JSON gateway and TLS is in use. It reproduces only with 3.6.0-r"
    )
    reproducer = '#!/usr/bin/env bash\nset -xeuo pipefail\n\ncat <<EOF > domains.ext\nauthorityKeyIdentifier=keyid,issuer\nbasicConstraints=CA:FALSE\nkeyUsage = digitalSignature, nonRepudiation, keyEncipherment, dataEncipherment\nsubjectAltName = @alt_names\n[alt_names]\nDNS.1 = localhost\nIP.1 = 127.0.0.1\nEOF\n\nopenssl req -x509 -nodes -new -sha256 -days 8192 -newkey rsa:2048 -keyout ca.key -out ca.pem -subj "/C=US/CN=Example-Root-CA"\nopenssl x509 -outform pem -in ca.pem -out ca.crt\n\nopenssl req -new -nodes -newkey rsa:2048 -keyout localhost.key -out localhost.csr -subj "/C=US/ST=YourState/L=YourCity/O=Example-Certificates/CN=localhost"\nopenssl x509 -req -sha256 -days 8192 -in localhost.csr -CA ca.pem -CAkey ca.key -CAcreateserial -extfile domains.ext -out localhost.crt'
    continuous_reproducer = True
