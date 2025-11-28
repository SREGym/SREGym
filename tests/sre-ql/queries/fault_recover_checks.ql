/**
 * @id sre-ql.fault-injection-recovery-check
 * @name Fault injection and recovery consistency check
 * @description Detects Problem subclasses with missing or inconsistent inject_fault/recover_fault methods.
 * @kind problem
 * @problem.severity error
 */
import python

class ProblemSubclass extends Class {
  ProblemSubclass() {
    // Direct inheritance from Problem
    this.getABase().(Name).getId() = "Problem"
  }
}

class FaultMethod extends Function {
  FaultMethod() {
    this.getName() in ["inject_fault", "recover_fault"]
  }

  string getMethodName() {
    result = this.getName()
  }

  predicate hasMarkFaultInjectedDecorator() {
    exists(Expr decorator |
      decorator = this.getADecorator() and
      (
        // Direct decorator: @mark_fault_injected
        decorator.(Name).getId() = "mark_fault_injected"
        or
        // Call decorator: @mark_fault_injected()
        decorator.(Call).getFunc().(Name).getId() = "mark_fault_injected"
        or
        // Attribute access: @module.mark_fault_injected
        decorator.(Attribute).getName() = "mark_fault_injected"
        or
        // Call on attribute: @module.mark_fault_injected()
        decorator.(Call).getFunc().(Attribute).getName() = "mark_fault_injected"
      )
    )
  }

  string getFaultIdentifier() {
    exists(Call call, StringLiteral arg |
      call.getScope() = this and
      call.getFunc().(Attribute).getName() in ["inject_fault", "recover_fault"] and
      arg = call.getArg(0) and
      result = arg.getText()
    )
  }
}

predicate hasMethod(ProblemSubclass c, string methodName) {
  exists(FaultMethod m |
    m.getScope() = c and
    m.getMethodName() = methodName
  )
}

predicate hasDecoratorOnMethod(ProblemSubclass c, string methodName) {
  exists(FaultMethod m |
    m.getScope() = c and
    m.getMethodName() = methodName and
    m.hasMarkFaultInjectedDecorator()
  )
}

string getFaultIdentifiers(ProblemSubclass c, string methodName) {
  exists(FaultMethod m |
    m.getScope() = c and
    m.getMethodName() = methodName and
    result = m.getFaultIdentifier()
  )
}

predicate hasMismatchedFaultIdentifiers(ProblemSubclass c) {
  exists(string injectId, string recoverId |
    injectId = getFaultIdentifiers(c, "inject_fault") and
    recoverId = getFaultIdentifiers(c, "recover_fault") and
    injectId != recoverId
  )
}

from ProblemSubclass c, string msg
where
  // Check 1: Has inject_fault but no recover_fault
  (hasMethod(c, "inject_fault") and
   not hasMethod(c, "recover_fault") and
   msg = "Problem subclass has inject_fault() but missing recover_fault()")
  or
  // Check 2: Has recover_fault but no inject_fault
  (hasMethod(c, "recover_fault") and
   not hasMethod(c, "inject_fault") and
   msg = "Problem subclass has recover_fault() but missing inject_fault()")
  or
  // Check 3: inject_fault exists but missing @mark_fault_injected decorator
  (hasMethod(c, "inject_fault") and
   not hasDecoratorOnMethod(c, "inject_fault") and
   msg = "inject_fault() method missing @mark_fault_injected decorator")
  or
  // Check 4: recover_fault exists but missing @mark_fault_injected decorator
  (hasMethod(c, "recover_fault") and
   not hasDecoratorOnMethod(c, "recover_fault") and
   msg = "recover_fault() method missing @mark_fault_injected decorator")
  or
  // Check 5: Mismatched fault identifiers between inject and recover
  (hasMismatchedFaultIdentifiers(c) and
   msg = "inject_fault() and recover_fault() use different fault identifiers: '" + 
         getFaultIdentifiers(c, "inject_fault") + "' vs '" + 
         getFaultIdentifiers(c, "recover_fault") + "'")
select c, msg