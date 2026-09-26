from sregym.conductor.oracles.base import Oracle


class SequenceHeadroomMitigationOracle(Oracle):
    """Mitigation passes when the target sequence has enough headroom below
    its data type's max, or the underlying column has been widened to BIGINT.
    """

    def evaluate(self) -> dict:
        print("== Mitigation Evaluation ==")

        kubectl = self.problem.kubectl
        namespace = self.problem.namespace
        pg_pod = self.problem.pg_pod
        pg_superuser = self.problem.pg_superuser
        pg_db = self.problem.pg_db
        sequence = self.problem.sequence
        column_schema = self.problem.column_schema
        column_table = self.problem.column_table
        column_name = self.problem.column_name
        min_headroom = self.problem.min_headroom
        results = {}

        # Check the sequence's current value.
        seq_cmd = (
            f"kubectl exec -n {namespace} {pg_pod} -- "
            f"psql -U {pg_superuser} -d {pg_db} -At "
            f"-c \"SELECT last_value FROM {sequence};\""
        )
        seq_out = kubectl.exec_command(seq_cmd).strip()
        try:
            last_value = int(seq_out.splitlines()[-1])
        except (ValueError, IndexError):
            print(f"❌ Could not read sequence {sequence}: {seq_out!r}")
            results["success"] = False
            return results

        # Check the column's data type.
        type_cmd = (
            f"kubectl exec -n {namespace} {pg_pod} -- "
            f"psql -U {pg_superuser} -d {pg_db} -At -c "
            f"\"SELECT data_type FROM information_schema.columns "
            f"WHERE table_schema='{column_schema}' "
            f"AND table_name='{column_table}' "
            f"AND column_name='{column_name}';\""
        )
        raw_type = kubectl.exec_command(type_cmd).strip()
        type_out = raw_type.splitlines()[-1] if raw_type else ""

        int4_max = 2147483647

        if type_out == "bigint":
            print(f"✅ Column widened to bigint (last_value={last_value}, plenty of headroom)")
            results["success"] = True
            return results

        if type_out == "integer":
            headroom = int4_max - last_value
            if headroom >= min_headroom:
                print(
                    f"✅ Column still integer but sequence reset — headroom={headroom}, "
                    f"threshold={min_headroom}"
                )
                results["success"] = True
                return results
            print(
                f"❌ Column still integer and headroom={headroom} < {min_headroom}"
            )
            results["success"] = False
            return results

        print(f"❌ Unexpected column data_type={type_out!r}")
        results["success"] = False
        return results
