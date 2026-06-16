"""Auto-generated from https://github.com/etcd-io/etcd/issues/14631

Title: concurrency.NewSession hang after etcd server is killed with SIGSTOP(19)
"""
from sregym.conductor.problems.generic_custom_build import GenericCustomBuildProblem


class AutoEtcd14631(GenericCustomBuildProblem):
    db_name   = "etcd"
    issue_url = "https://github.com/etcd-io/etcd/issues/14631"
    root_cause_description = (
        "concurrency.NewSession hang after etcd server is killed with SIGSTOP(19). ### What happened? [`concurrency.NewSession`](https://github.com/etcd-io/etcd/blob/55500416335e959e347d368c7f8a7a0229db3f6a/client/v3/concurrency/session.go#L38) hang after etcd server is kill by SIGSTOP(19) ### What did you expect to happen? `NewSession` can return error after server is killed. ### How can we reproduce it (as minimally and precisely as possible)? 1. start three or more etcd server nodes. 2. run `main` with following codes. 3. kill -19 `pidof etcd leader` ``` package main import ( \"fmt\" \"time\" \"github.com/pingcap/log\" clientv3 \"go.etcd.io/etcd/client/v3\" \"go.etcd.io/etcd/client/v3/concurrency\" \"go.uber.org/zap\" ) func initEtcdClient() *clientv3.Client { var client *clientv3.Client var err error endpoints := []string{\"172.16.5"
    )
