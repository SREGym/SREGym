# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import unittest
from aiopslab.service.shell import Shell


class TestExecShell(unittest.TestCase):
    def test_echo(self):
        command = "echo 'Hello, World!'"
        output = Shell.exec(command)
        self.assertEqual(output, "Hello, World!\n")

    def test_kubectl_pods(self):
        command = "kubectl get pods -n test-social-network"
        output = Shell.exec(command)
        self.assertTrue("compose-post-service" in output)

    def test_kubectl_services(self):
        command = "kubectl get services -n test-social-network"
        output = Shell.exec(command)
        self.assertTrue("compose-post-service" in output)

    def test_patch(self):
        command = 'kubectl patch svc user-service -n test-social-network --type=\'json\' -p=\'[{"op": "replace", "path": "/spec/ports/0/targetPort", "value": 9090}]\''
        output = Shell.exec(command)

        command = "kubectl get svc user-service -n test-social-network -o jsonpath='{.spec.ports[0].targetPort}'"
        output = Shell.exec(command)
        self.assertEqual(output, "9090")


if __name__ == "__main__":
    unittest.main()
