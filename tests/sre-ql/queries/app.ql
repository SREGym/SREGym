/**
 * @id sre-ql.null-check
 * @name Problem subclass app assignment check
 * @description Detects subclasses of Problem missing self.app assignments or assigning None.
 * @kind problem
 * @problem.severity warning
 */
import python

class ProblemSubclass extends Class {
  ProblemSubclass() {
    exists(ClassValue base |
      base.getName() = "Problem" and
      this.getABase().pointsTo(base)
    )
  }
}

class AppAssignment extends AssignStmt {
  AppAssignment() {
    exists(Attribute attr |
      attr = this.getATarget() and 
      attr.getObject().(Name).getId() = "self" and
      attr.getName() = "app"
    )
  }
}

predicate assignsApp(ProblemSubclass c, AppAssignment a) {
  a.getScope().(Function).getScope() = c
}

predicate assignsNone(AppAssignment a) {
  a.getValue() instanceof None
}

string getMessage(ProblemSubclass c) {
  not exists(AppAssignment a | assignsApp(c, a)) and
  result = "NO self.app defined"
  or
  exists(AppAssignment a |
    assignsApp(c, a) and
    assignsNone(a)
  ) and
  result = "self.app assigned to None"
}

from ProblemSubclass c, string msg
where msg = getMessage(c)
select c, msg
