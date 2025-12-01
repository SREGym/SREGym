/**
 * @name Hardcoded absolute path
 * @description Use Path(__file__).parent or relative paths instead of absolute paths
 * @kind problem
 * @problem.severity warning
 * @id python/hardcoded-absolute-path
 * @precision high
 */
import python

from StringLiteral path
where
  path.getText().regexpMatch(".*/Users/.*") or
  path.getText().regexpMatch(".*/home/.*") or
  path.getText().regexpMatch(".*C:\\\\.*") or
  path.getText().regexpMatch(".*/root/.*")
select path, "Hardcoded absolute path detected - use relative paths instead"