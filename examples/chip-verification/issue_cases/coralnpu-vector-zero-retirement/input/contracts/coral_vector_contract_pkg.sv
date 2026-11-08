// SPDX-License-Identifier: Apache-2.0
// Authored interface contract only; no implementation or verification oracle.
package coral_vector_contract_pkg;
  localparam int XLEN = 32;
  localparam int VLEN = 128;
  localparam int COMMAND_SLOTS = 4;
  localparam int READ_SLOTS = 4;
  typedef enum logic [3:0] {
    OP_ADD = 4'h0, OP_SUB = 4'h1, OP_XOR = 4'h2,
    OP_LOAD = 4'h3, OP_STORE = 4'h4,
    OP_STRIDED_LOAD = 4'h5, OP_STRIDED_STORE = 4'h6,
    OP_WHOLE_LOAD = 4'h7, OP_WHOLE_STORE = 4'h8,
    OP_MOVE_X_S = 4'h9, OP_MOVE_S_X = 4'ha, OP_CONFIG = 4'hb
  } operation_e;
  typedef struct packed {
    logic [3:0] op;
    logic [7:0] tag;
    logic [31:0] pc;
    logic [4:0] vl;
    logic [4:0] vstart;
    logic [1:0] sew;
    logic vill;
    logic masked;
    logic [15:0] mask;
    logic [31:0] base;
    logic signed [31:0] stride;
    logic [31:0] scalar;
    logic [127:0] vs1;
    logic [127:0] vs2;
    logic [127:0] old_vd;
  } vector_command_t;
  typedef struct packed {
    logic [7:0] id;
    logic [7:0] tag;
    logic [4:0] element;
    logic write;
    logic [31:0] address;
    logic [1:0] size;
    logic [31:0] data;
  } vector_memory_request_t;
  typedef struct packed {
    logic [7:0] id;
    logic error;
    logic [31:0] data;
  } vector_memory_response_t;
  typedef struct packed {
    logic [7:0] tag;
    logic [31:0] pc;
    logic cancelled;
    logic trap;
    logic [4:0] cause;
    logic [4:0] vstart_next;
    logic [15:0] vector_we;
    logic [127:0] vector_data;
    logic scalar_we;
    logic [31:0] scalar_data;
    logic config_we;
    logic [4:0] vl_next;
    logic [1:0] sew_next;
    logic vill_next;
  } vector_retirement_t;
endpackage
