-- Read actual seeded repository state through Gitea's HTTP API.
request = function()
  local routes = {
    "/api/v1/repos/zoo-labs/zoo-utilities",
    "/api/v1/repos/zoo-labs/zoo-utilities/contents/README.md",
    "/api/v1/repos/zoo-labs/zoo-utilities/issues?state=open"
  }
  return wrk.format("GET", routes[math.random(#routes)])
end
