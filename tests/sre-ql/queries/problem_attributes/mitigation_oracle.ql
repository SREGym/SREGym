/**
 * @id sre-ql.mitigation-oracle-null-check
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
where msg = getMessage(c)
select c, msg