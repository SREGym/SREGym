/**
 * @name Hardcoded absolute path
 * @description Use Path(__file__).parent or relative paths instead of absolute paths
 * @kind problem
 * @problem.severity warning
 * @id python/hardcoded-absolute-path
 * @precision high
 */
import python

predicate shouldIgnore(StringLiteral path) {
  exists(Module m |
    m = path.getEnclosingModule() and
    exists(string filename |
      filename = m.getFile().getBaseName() and
      (
        filename = "automating_tests.py" or
        filename = "auto_submit.py"
      )
    )
  )
}

from StringLiteral path
where
  (
    path.getText().regexpMatch(".*/Users/.*") or
    path.getText().regexpMatch(".*/home/.*") or
    path.getText().regexpMatch(".*C:\\\\.*") or
    path.getText().regexpMatch(".*/root/.*")
  ) and
  not shouldIgnore(path)
select path, "Hardcoded absolute path detected - use relative paths instead"
