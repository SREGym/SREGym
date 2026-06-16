"""Auto-generated from https://jira.mongodb.org/browse/SERVER-91784

Title: $project-$addFields on arrays can produce incorrect results
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoMongodb91784(GenericCustomBuildProblem):
    db_name   = "mongodb"
    issue_url = "https://jira.mongodb.org/browse/SERVER-91784"
    root_cause_description = (
        "$project-$addFields on arrays can produce incorrect results. Consider the following pipeline: {code} [ {$documents: [{a: [1, 2, 3]}]}, {$project: {\"a.b\": 1}}, {$addFields: {\"a.c\": \"why?\"}} ] {code} Inclusion $project on a sub-field of an array will remove all scalar values, making array “a” empty. $addFields on a sub-field of an array will add {c: “why?“} to every element of the array, but array is empty, so the expected answer would be {code} [{a: []}] {code} However, we get {code} { a: [ { c: 'why?' }, { c: 'why?' }, { c: 'why?' } ] } {code} because $project actually doesn't remove scalars, but replacing them with MISSING values. This is a bug. Especially given it behaves inconsistently with reading from disk, because this pipeline will return correct result. Consider a collection test with a single document {a: ["
    )
    reproducer = 'db.aggregate([\n  {$documents: [{a: [1, 2, 3]}]},\n  {$project: {"a.b": 1}},\n  {$addFields: {"a.c": "why?"}}\n])'
    continuous_reproducer = True
    expected_output = "{ a: [ { c: 'why?' }, { c: 'why?' }, { c: 'why?' } ] }"
