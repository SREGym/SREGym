"""Auto-generated from https://issues.apache.org/jira/browse/ZOOKEEPER-2213

Title: Empty path in Set crashes server and prevents restart
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoZookeeper2213(GenericCustomBuildProblem):
    db_name   = "zookeeper"
    issue_url = "https://issues.apache.org/jira/browse/ZOOKEEPER-2213"
    root_cause_description = (
        "Empty path in Set crashes server and prevents restart. See https://github.com/samuel/go-zookeeper/issues/62 I've reproduced this on 3.4.5 with the code: c, _, _ := zk.Connect([]string{\"127.0.0.1\"}, time.Second) c.Set(\"\", []byte{}, 0) This crashes a local zookeeper 3.4.5 server: 2015-06-10 16:21:10,862 [myid:] - ERROR [SyncThread:0:SyncRequestProcessor@151] - Severe unrecoverable error, exiting java.lang.IllegalArgumentException: Invalid path at org.apache.zookeeper.common.PathTrie.findMaxPrefix(PathTrie.java:259) at org.apache.zookeeper.server.DataTree.getMaxPrefixWithQuota(DataTree.java:634) at org.apache.zookeeper.server.DataTree.setData(DataTree.java:616) at org.apache.zookeeper.server.DataTree.processTxn(DataTree.java:807) at org.apache.zookeeper.server.ZKDatabase.processTxn(ZKDat"
    )
