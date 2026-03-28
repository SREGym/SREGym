/**
 * @id sre-ql/namespace-null-check
 * @name Problem subclass namespace assignment check
 * @description Detects subclasses of Problem missing self.namespace assignments or assigning None.
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

class NamespaceAssignment extends AssignStmt {
  NamespaceAssignment() {
    exists(Attribute attr |
      attr = this.getATarget() and
      attr.getObject().(Name).getId() = "self" and
      attr.getName() = "namespace"
    )
  }
}

predicate assignsNamespace(ProblemSubclass c, NamespaceAssignment a) {
  a.getScope().(Function).getScope() = c
}

predicate assignsNone(NamespaceAssignment a) {
  a.getValue() instanceof None
}

predicate shouldIgnore(ProblemSubclass c) {
  exists(Module m |
    m = c.getEnclosingModule() and
    exists(string filename |
      filename = m.getFile().getBaseName() and
      (
        filename = "multiple_failures.py"
      )
    )
  )
}

string getMessage(ProblemSubclass c) {
  not exists(NamespaceAssignment a | assignsNamespace(c, a)) and
  result = "NO self.namespace defined"
  or
  exists(NamespaceAssignment a |
    assignsNamespace(c, a) and
    assignsNone(a)
  ) and
  result = "self.namespace assigned to None"
}

from ProblemSubclass c, string msg
where
  msg = getMessage(c) and
  not shouldIgnore(c)
select c, msg
