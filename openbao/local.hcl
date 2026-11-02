# Used by scripts/bao_server.sh when running the OpenBao binary without Docker.
storage "raft" {
  path    = ".bao/data"
  node_id = "bao-local"
}
listener "tcp" {
  address     = "127.0.0.1:8200"
  tls_disable = 1
}
api_addr     = "http://127.0.0.1:8200"
cluster_addr = "http://127.0.0.1:8201"
