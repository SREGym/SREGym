/**
 * @id sre-ql/mitigation-oracle-null-check
 * @name Problem subclass mitigation_oracle assignment check
 * @description Detects subclasses of Problem missing self.mitigation_oracle assignments or assigning None.
 * @kind problem
 * @problem.severity warning
 */
import python

class ProblemSubclass extends Class {
  ProblemSubclass() {
    // Direct inheritance from Problem
    this.getABase().(Name).getId() = "Problem"
  }
}

class MitigationOracleAssignment extends AssignStmt {
  MitigationOracleAssignment() {
    exists(Attribute attr |
      attr = this.getATarget() and
      attr.getObject().(Name).getId() = "self" and
      attr.getName() = "mitigation_oracle"
    )
  }
}

predicate shouldIgnore(ProblemSubclass c) {
  exists(Module m |
    m = c.getEnclosingModule() and
    exists(string filename |
      filename = m.getFile().getBaseName() and
      (
        filename = "ad_service_failure.py" or
        filename = "ad_service_high_cpu.py" or
        filename = "ad_service_manual_gc.py" or
        filename = "cart_service_failure.py" or
        filename = "gc_capacity_degradation.py" or
        filename = "image_slow_load.py" or
        filename = "kafka_queue_problems.py" or
        filename = "kubelet_crash.py" or
        filename = "latent_sector_error.py" or
        filename = "loadgenerator_flood_homepage.py" or
        filename = "payment_service_failure.py" or
        filename = "payment_service_unreachable.py" or
        filename = "product_catalog_failure.py" or
        filename = "read_error.py" or
        filename = "recommendation_service_cache_failure.py" or
        filename = "silent_data_corruption.py" or
        filename = "valkey_memory_disruption.py" or
        filename = "email_memory_leak.py" or
        filename = "khaos_faults.py" or
        filename = "llm_inaccurate_response.py" or
        filename = "llm_rate_limit_error.py"
      )
    )
  )
}

predicate assignsMitigationOracle(ProblemSubclass c, MitigationOracleAssignment a) {
  a.getScope().(Function).getScope() = c
}

predicate assignsNone(MitigationOracleAssignment a) {
  a.getValue() instanceof None
}

string getMessage(ProblemSubclass c) {
  not exists(MitigationOracleAssignment a | assignsMitigationOracle(c, a)) and
  result = "NO self.mitigation_oracle defined"
  or
  exists(MitigationOracleAssignment a |
    assignsMitigationOracle(c, a) and
    assignsNone(a)
  ) and
  result = "self.mitigation_oracle assigned to None"
}

from ProblemSubclass c, string msg
where
  msg = getMessage(c) and
  not shouldIgnore(c)
select c, msg
