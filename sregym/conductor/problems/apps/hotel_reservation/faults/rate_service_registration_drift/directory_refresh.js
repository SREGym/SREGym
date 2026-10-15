// Refresh advertised service-family addresses from the local inventory export.
var singlePass = _getEnv('DIRECTORY_SINGLE_PASS') === '1';
var hostPort = inventory.consulAddress.split(':');
var responsePath = '/tmp/directory_refresh.' + ObjectId().str + '.response';
var serviceName = 'srv-rate';
var exportHost = inventory.RateMongoAddress.split(':')[0];
var rc = runProgram('bash', '-c', 'getent ahostsv4 "$1" | awk \'NR == 1 {print $1}\' > "$2"', 'directory-address', exportHost, responsePath);
if (rc !== 0) throw new Error('inventory address unavailable');
var desiredAddress = cat(responsePath).trim();
runProgram('rm', '-f', responsePath);
if (!desiredAddress) throw new Error('inventory address unavailable');

function request(path, value) {
    var body = value === undefined ? '' : JSON.stringify(value);
    var method = value === undefined ? 'GET' : 'PUT';
    var transport = 'exec 3<>/dev/tcp/"$1"/"$2"; ' +
        'printf "%s %s HTTP/1.0\\r\\nHost: %s\\r\\nContent-Type: application/json\\r\\nContent-Length: %d\\r\\nConnection: close\\r\\n\\r\\n%s" "$3" "$4" "$1" "${#5}" "$5" >&3; ' +
        'cat <&3 > "$6"';
    var status = runProgram('timeout', '10', 'bash', '-c', transport,
        'directory-client', hostPort[0], hostPort[1], method, path, body, responsePath);
    if (status !== 0) throw new Error('directory connection unavailable');
    var response = cat(responsePath);
    runProgram('rm', '-f', responsePath);
    var split = response.indexOf('\r\n\r\n');
    if (split < 0 || !response.match(/^HTTP\/1\.[01] 200 /)) throw new Error('directory request unavailable');
    var data = response.slice(split + 4).trim();
    return data ? JSON.parse(data) : null;
}
function registration(item) {
    var result = {ID: item.ID, Name: item.Service, Address: desiredAddress, Port: item.Port};
    ['Tags', 'Meta', 'Weights', 'EnableTagOverride'].forEach(function(key) {
        if (item[key] !== undefined) result[key] = item[key];
    });
    result.TaggedAddresses = {};
    Object.keys(item.TaggedAddresses || {}).forEach(function(key) {
        result.TaggedAddresses[key] = {Address: desiredAddress, Port: item.TaggedAddresses[key].Port};
    });
    return result;
}
var pending = null;
while (true) {
    try {
        var entries = request('/v1/agent/services');
        var rates = Object.keys(entries).map(function(key) { return entries[key]; }).filter(function(item) {return item.Service === serviceName;});
        var drift = rates.filter(function(item) {return item.Address !== desiredAddress;});
        var now = Date.now();
        if (singlePass || (drift.length && pending !== null && now >= pending)) {
            drift.forEach(function(item) {request('/v1/agent/service/register', registration(item));});
            pending = null;
        } else if (drift.length && pending === null) {
            // Allow imported inventory to stabilize before reconciling addresses.
            pending = now + 240000;
        } else if (!drift.length) {
            pending = null;
        }
        if (singlePass) {
            if (!rates.length) throw new Error('rate inventory unavailable');
            quit(0);
        }
    } catch (error) {
        if (singlePass) throw error;
        print('directory refresh unavailable: ' + error);
    }
    sleep(2000);
}
