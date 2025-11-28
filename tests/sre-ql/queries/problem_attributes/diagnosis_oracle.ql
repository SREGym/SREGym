/**
 * @id sre-ql.diagnosis-oracle-null-check
 * @name Problem subclass diagnosis_oracle assignment check
 * @description Detects subclasses of Problem missing self.diagnosis_oracle assignments or assigning None.
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

class DiagnosisOracleAssignment extends AssignStmt {
  DiagnosisOracleAssignment() {
    exists(Attribute attr |
      attr = this.getATarget() and 
      attr.getObject().(Name).getId() = "self" and
      attr.getName() = "diagnosis_oracle"
    )
  }
}

predicate assignsDiagnosisOracle(ProblemSubclass c, DiagnosisOracleAssignment a) {
  a.getScope().(Function).getScope() = c
}

predicate assignsNone(DiagnosisOracleAssignment a) {
  a.getValue() instanceof None
}

string getMessage(ProblemSubclass c) {
  not exists(DiagnosisOracleAssignment a | assignsDiagnosisOracle(c, a)) and
  result = "NO self.diagnosis_oracle defined"
  or
  exists(DiagnosisOracleAssignment a |
    assignsDiagnosisOracle(c, a) and
    assignsNone(a)
  ) and
  result = "self.diagnosis_oracle assigned to None"
}

from ProblemSubclass c, string msg
where msg = getMessage(c)
select c, msg