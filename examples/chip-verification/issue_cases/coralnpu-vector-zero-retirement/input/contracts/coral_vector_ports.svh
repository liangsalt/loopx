// SPDX-License-Identifier: Apache-2.0
// Include inside module coral_vector_retirement_subsystem(...).
input  logic clk_i,
input  logic rst_ni,
input  logic flush_i,
input  logic cmd_valid_i,
output logic cmd_ready_o,
input  coral_vector_contract_pkg::vector_command_t cmd_i,
output logic mem_req_valid_o,
input  logic mem_req_ready_i,
output coral_vector_contract_pkg::vector_memory_request_t mem_req_o,
input  logic mem_rsp_valid_i,
output logic mem_rsp_ready_o,
input  coral_vector_contract_pkg::vector_memory_response_t mem_rsp_i,
output logic retire_valid_o,
input  logic retire_ready_i,
output coral_vector_contract_pkg::vector_retirement_t retire_o
