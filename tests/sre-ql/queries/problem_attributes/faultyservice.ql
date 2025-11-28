/**
 * @id sre-ql.faulty-service-null-check
 * @name Problem subclass faulty_service assignment check
 * @description Detects subclasses of Problem missing self.faulty_service assignments or assigning None.
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

class FaultyServiceAssignment extends AssignStmt {
  FaultyServiceAssignment() {
    exists(Attribute attr |
      attr = this.getATarget() and 
      attr.getObject().(Name).getId() = "self" and
      attr.getName() = "faulty_service"
    )
  }
}

predicate assignsFaultyService(ProblemSubclass c, FaultyServiceAssignment a) {
  a.getScope().(Function).getScope() = c
}

predicate assignsNone(FaultyServiceAssignment a) {
  a.getValue() instanceof None
}

string getMessage(ProblemSubclass c) {
  not exists(FaultyServiceAssignment a | assignsFaultyService(c, a)) and
  result = "NO self.faulty_service defined"
  or
  exists(FaultyServiceAssignment a |
    assignsFaultyService(c, a) and
    assignsNone(a)
  ) and
  result = "self.faulty_service assigned to None"
}

from ProblemSubclass c, string msg
where msg = getMessage(c)
select c, msg