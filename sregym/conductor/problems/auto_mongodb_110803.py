"""Auto-generated from https://jira.mongodb.org/browse/SERVER-110803

Title: $top and $bottom ignore sortBy clause when preceeding $sort stage makes it eligible for DISTINCT_SCAN optimization
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoMongodb110803(GenericCustomBuildProblem):
    db_name   = "mongodb"
    issue_url = "https://jira.mongodb.org/browse/SERVER-110803"
    root_cause_description = (
        "$top and $bottom ignore sortBy clause when preceeding $sort stage makes it eligible for DISTINCT_SCAN optimization. {panel:title=Issue Status as of March 17, 2026|borderColor=#cccccc|titleBGColor=#6cb33f|bgColor=#eeeeee} *SUMMARY* On MongoDB Server v8.0+ it is possible to get incorrect results for $sort + $group with $bottom or $top accumulators queries in some cases when a distinct optimization is incorrectly applied. The error is deterministic. *ISSUE DESCRIPTION AND IMPACT* The issue affects $sort + $group aggregation queries with $bottom/$top accumulators eligible for distinct scan optimization in the presence of an index satisfying the sorting pattern of the $sort stage. For example: {code} Index: { a: 1, b: 1 } Aggregation pipeline: [ { $sort: { a: 1, b: 1 } }, { $group: { _id: \"$a\", max_b: { $top: { output: \"$b\", sortBy: { b:"
    )
    reproducer = 'db.events.insertMany([{ device: "M57906", date: new Date(\'2025-07-06T00:00:01.305Z\') }, { device: "M57906", date: new Date(\'2025-08-28T09:46:33.017Z\') }]);\ndb.events.createIndex({ device: 1, date: 1 });\ndb.events.aggregate([{ $sort: { device: 1, date: 1 } }, { $group: { _id: "$device", obj: { $bottom: { output: "$date", sortBy: { date: -1 } } } } }])'
    continuous_reproducer = True
    expected_output = '{ "_id": "M57906", "obj": ISODate("2025-08-28T09:46:33.017Z") }'
