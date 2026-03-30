/**
 * @id sre-ql/lifecycle-methods-check
 * @name Application lifecycle methods check
 * @description Detects classes with incomplete lifecycle method implementations.
 * @kind problem
 * @problem.severity error
 */
import python

class ApplicationSubclass extends Class {
  ApplicationSubclass() {
    // Direct inheritance from Application or Problem
    this.getABase().(Name).getId() in ["Application", "Problem"]
  }
}

predicate shouldIgnore(ApplicationSubclass c) {
  exists(Module m |
    m = c.getEnclosingModule() and
    exists(string filename |
      filename = m.getFile().getBaseName() and
      (
        filename = "blueprint_hotel_reservation.py" or
        filename = "hotel_reservation.py"
      )
    )
  )
}

predicate hasMethod(ApplicationSubclass c, string methodName) {
  exists(Function f |
    f.getScope() = c and
    f.getName() = methodName
  )
}

predicate callsCreateNamespace(ApplicationSubclass c) {
  exists(Call call, Function f |
    f.getScope() = c and
    call.getScope() = f and
    (
      call.getFunc().(Attribute).getName() = "create_namespace"
      or
      call.getFunc().(Attribute).getName() = "create_namespace_if_not_exist"
    )
  )
}

predicate callsDeleteNamespace(ApplicationSubclass c) {
  exists(Call call, Function f |
    f.getScope() = c and
    call.getScope() = f and
    call.getFunc().(Attribute).getName() = "delete_namespace"
  )
}

predicate callsStartPortForward(ApplicationSubclass c) {
  exists(Call call, Function f |
    f.getScope() = c and
    call.getScope() = f and
    call.getFunc().(Attribute).getName() = "start_port_forward"
  )
}

predicate callsStopPortForward(ApplicationSubclass c) {
  exists(Call call, Function f |
    f.getScope() = c and
    call.getScope() = f and
    call.getFunc().(Attribute).getName() = "stop_port_forward"
  )
}



predicate hasStopMechanism(ApplicationSubclass c) {
  // Check if stop_workload() method exists
  hasMethod(c, "stop_workload")
  or
  // Check if stop() method exists
  hasMethod(c, "stop")
  or
  // Check if there are actual calls to stop
  exists(Call call, Function f |
    f.getScope() = c and
    call.getScope() = f and
    (
      (call.getFunc().(Attribute).getName() = "stop" and
       call.getFunc().(Attribute).getObject().(Attribute).getName() in ["wrk", "workload_manager"])
      or
      call.getFunc().(Attribute).getName() = "stop_workload"
    )
  )
}

from ApplicationSubclass c, string msg
where
  (
    // Check 1: Has deploy() but no cleanup()
    (hasMethod(c, "deploy") and
     not hasMethod(c, "cleanup") and
     msg = "Class has deploy() method but missing cleanup() method")
    or
    // Check 2: Has deploy() but no delete()
    (hasMethod(c, "deploy") and
     not hasMethod(c, "delete") and
     msg = "Class has deploy() method but missing delete() method")
    or
    // Check 3: Has start_workload() but no stop mechanism
    (hasMethod(c, "start_workload") and
     not hasStopMechanism(c) and
     msg = "Class has start_workload() but no mechanism to stop workload (missing stop() or stop_workload())")
    or
    // Check 4: Calls create_namespace() but never delete_namespace()
    (callsCreateNamespace(c) and
     not callsDeleteNamespace(c) and
     msg = "Class calls create_namespace() but never calls delete_namespace() in cleanup")
    or
    // Check 5: Calls start_port_forward() but never stop_port_forward()
    (callsStartPortForward(c) and
     not callsStopPortForward(c) and
     msg = "Class calls start_port_forward() but never calls stop_port_forward() (resource leak)")
  ) and
  not shouldIgnore(c)
select c, msg
