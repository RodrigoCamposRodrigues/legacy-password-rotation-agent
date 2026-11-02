# Used by docker-compose. Integrated (Raft) storage persists across restarts, unlike -dev mode.
storage "raft" {
  path    = "/openbao/file"
  node_id = "bao-1"
}
listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = 1
}
api_addr     = "http://127.0.0.1:8200"
cluster_addr = "http://127.0.0.1:8201"
ui           = true
