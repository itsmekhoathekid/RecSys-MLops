check "compact_12vcpu_capacity_contract" {
  assert {
    condition = var.capacity_profile != "compact-12vcpu" || (
      var.cpu_machine_type == "n2-standard-8" &&
      var.cpu_min_nodes == 1 &&
      var.cpu_max_nodes == 1 &&
      var.ml_machine_type == "e2-standard-4" &&
      var.ml_min_nodes == 1 &&
      var.ml_max_nodes == 1 &&
      var.llm_node_pool_mode == "cpu-services-shared" &&
      !var.enable_gpu_pool
    )
    error_message = "compact-12vcpu must use exactly one n2-standard-8 CPU node, one e2-standard-4 ML node, shared LLM placement, and no GPU pool."
  }
}
