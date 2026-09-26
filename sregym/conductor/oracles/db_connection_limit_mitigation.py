from sregym.conductor.oracles.base import Oracle


class DBConnectionLimitMitigationOracle(Oracle):
    """Mitigation passes when the target role's `rolconnlimit` is back to the
    correct value and no pods are stuck in a failing state."""

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        pg_pod = self.problem.pg_pod
        pg_superuser = self.problem.pg_superuser
        pg_db = self.problem.pg_db
        role = self.problem.role
        correct_limit = self.problem.correct_limit
        results = {}

        query = (
            f"SELECT rolconnlimit FROM pg_roles WHERE rolname='{role}';"
        )
        cmd = (
            f"kubectl exec -n {namespace} {pg_pod} -- "
            f"psql -U {pg_superuser} -d {pg_db} -At -c \"{query}\""
        )
        out = kubectl.exec_command(cmd).strip()
        role_ok = False
        try:
            live = int(out.splitlines()[0]) if out else None
            if live == correct_limit:
                print(f"✅ Role {role} CONNECTION LIMIT restored to {correct_limit}")
                role_ok = True
            else:
                print(
                    f"❌ Role {role} CONNECTION LIMIT is {live}, expected {correct_limit}"
                )
        except (ValueError, IndexError):
            print(f"❌ Unable to parse rolconnlimit from psql output: {out!r}")

        pods_ok = True
        if role_ok:
            pod_list = kubectl.list_pods(namespace)
            for pod in pod_list.items:
                if pod.status.phase != "Running":
                    print(f"❌ Pod {pod.metadata.name} is in phase: {pod.status.phase}")
                    pods_ok = False
                    break
                for cs in pod.status.container_statuses or []:
                    if cs.state.waiting and cs.state.waiting.reason:
                        print(
                            f"❌ Container {cs.name} is waiting: {cs.state.waiting.reason}"
                        )
                        pods_ok = False
                    elif (
                        cs.state.terminated
                        and cs.state.terminated.reason != "Completed"
                    ):
                        print(
                            f"❌ Container {cs.name} terminated: {cs.state.terminated.reason}"
                        )
                        pods_ok = False
                if not pods_ok:
                    break

        success = role_ok and pods_ok
        results["success"] = success
        print(f"Mitigation Result: {'✅ Pass' if success else '❌ Fail'}")
        return results
