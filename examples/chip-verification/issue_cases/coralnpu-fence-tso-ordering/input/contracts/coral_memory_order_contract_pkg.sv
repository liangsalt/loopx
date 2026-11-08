// SPDX-License-Identifier: Apache-2.0
// Authored interface contract only; no implementation or verification oracle.
package coral_memory_order_contract_pkg;
  localparam int XLEN = 32;
  localparam int COMMAND_SLOTS = 4;
  localparam int READ_SLOTS = 4;
  localparam logic [31:0] FENCE_TSO = 32'h8330000f;
  typedef struct packed {
    logic [7:0] tag;
    logic [31:0] pc;
    logic [31:0] instruction;
    logic [31:0] rs1;
    logic [31:0] rs2;
  } memory_command_t;
  typedef struct packed {
    logic [7:0] id;
    logic [7:0] tag;
    logic write;
    logic [31:0] address;
    logic [1:0] size;
    logic [31:0] data;
  } memory_request_t;
  typedef struct packed {
    logic [7:0] id;
    logic error;
    logic [31:0] data;
  } memory_response_t;
  typedef struct packed {
    logic [7:0] tag;
    logic [31:0] pc;
    logic [31:0] instruction;
    logic cancelled;
    logic trap;
    logic [4:0] cause;
    logic [31:0] tval;
    logic [4:0] rd;
    logic rd_we;
    logic [31:0] rd_data;
  } memory_retirement_t;
endpackage
