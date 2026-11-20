# Company tools

The `companyctl` command provides the normal company operations gateway. Download it from the company operations API:
`curl -fsS http://host.docker.internal:28311/companyctl -o companyctl && chmod +x companyctl`

| Command | Operation |
|---|---|
| `companyctl inventory` | List company servers |
| `companyctl exec SERVER 'COMMAND'` | Execute a command on a company server |
| `companyctl console SERVER 'COMMAND'` | Execute through the provider serial console |
| `companyctl stream SERVER 'COMMAND' > FILE` | Stream complete command output to a local file |
| `companyctl put SERVER LOCAL_FILE /ABSOLUTE/GUEST/PATH` | Upload a local file |
| `companyctl start SERVER` / `stop SERVER` | Start or stop a server |
| `companyctl rescue SERVER` / `unrescue SERVER` | Enter or leave provider rescue mode |
| `companyctl unrescue-stopped SERVER` | Leave rescue mode with the server stopped |
| `companyctl rebuild SERVER` | Rebuild a server |
| `companyctl template [JSON_FILE]` | Read or update the default instance template |
| `companyctl create NAME` / `delete NAME` | Create or delete a company instance |

| Endpoint | Service |
|---|---|
| `http://host.docker.internal:28310` | EU customer reporting |
| `http://host.docker.internal:28320` | US customer reporting |
| `http://host.docker.internal:28311` | Company operations API |
| `http://host.docker.internal:28330` | Company source service |
| `http://host.docker.internal:28390` | Metrics |
| `http://host.docker.internal:28391` | Logs |
| `http://host.docker.internal:28392` | Software/source catalog |

Company guest shells contain the installed application and operations tools. Use the normal company documentation and service contracts for their interfaces.
