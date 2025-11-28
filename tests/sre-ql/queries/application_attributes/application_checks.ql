/**
 * @id sre-ql.application-load-json-check
 * @name Application subclass load_app_json call check
 * @description Detects Application subclasses that don't call self.load_app_json() or super().load_app_json().
 * @kind problem
 * @problem.severity error
 */
import python

class ApplicationSubclass extends Class {
  ApplicationSubclass() {
    // Direct inheritance from Application
    this.getABase().(Name).getId() = "Application"
  }
}

predicate callsLoadAppJson(ApplicationSubclass c) {
  exists(Call call, Function init |
    init.getName() = "__init__" and
    init.getScope() = c and
    call.getScope() = init and
    call.getFunc().(Attribute).getName() = "load_app_json" and
    call.getFunc().(Attribute).getObject().(Name).getId() = "self"
  )
}

predicate overridesLoadAppJson(ApplicationSubclass c) {
  exists(Function f |
    f.getName() = "load_app_json" and
    f.getScope() = c
  )
}

predicate callsSuperLoadAppJson(ApplicationSubclass c) {
  exists(Call call, Function f |
    f.getName() = "load_app_json" and
    f.getScope() = c and
    call.getScope() = f and
    call.getFunc().(Attribute).getName() = "load_app_json" and
    call.getFunc().(Attribute).getObject().(Call).getFunc().(Name).getId() = "super"
  )
}

from ApplicationSubclass c, string msg
where
  (not callsLoadAppJson(c) and
   msg = "Application subclass __init__ does not call self.load_app_json()")
  or
  (overridesLoadAppJson(c) and
   not callsSuperLoadAppJson(c) and
   msg = "Application subclass overrides load_app_json() but doesn't call super().load_app_json()")
select c, msg